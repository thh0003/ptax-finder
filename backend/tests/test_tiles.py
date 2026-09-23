import io
import uuid

import morecantile
import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from geoalchemy2.shape import to_shape
from pyproj import Transformer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ptax.config import Settings
from ptax.db.models import Parcel
from ptax.imagery.gdal import gdal_env, s3_uri
from ptax.imagery.reader import assets_intersecting, read_parcel, read_parcel_uris
from tests.conftest import FIXTURES_DIR, ingest_naip_year, ingest_upload_year

IMAGERY_DIR = FIXTURES_DIR / "imagery"
TMS = morecantile.tms.get("WebMercatorQuad")


def _png_array(content: bytes) -> np.ndarray:
    with rasterio.open(io.BytesIO(content)) as src:
        return src.read()


@pytest.fixture
def years(client, db, settings, tenant_with_admin, ingested_layer) -> dict:
    headers = tenant_with_admin["admin_headers"]
    y2023 = ingest_naip_year(client, db, headers, 2023)
    y2025 = ingest_upload_year(
        client,
        db,
        settings,
        tenant_with_admin["tenant"],
        headers,
        2025,
        {"ortho_2025_partial.tif": (IMAGERY_DIR / "ortho_2025_partial.tif").read_bytes()},
    )
    assert y2025["status"] == "ready", y2025.get("error")
    return {2023: y2023, 2025: y2025}


def _parcel(db: Session, layer_id: str, ref: str) -> Parcel:
    return db.execute(
        select(Parcel).where(Parcel.layer_id == uuid.UUID(layer_id), Parcel.parcel_ref == ref)
    ).scalar_one()


def test_tile_endpoint(client: TestClient, tenant_with_admin, years, make_token, db) -> None:
    headers = tenant_with_admin["admin_headers"]
    year = years[2023]
    minx, miny, maxx, maxy = year["bounds"]
    tile = TMS.tile((minx + maxx) / 2, (miny + maxy) / 2, 17)

    response = client.get(f"/api/tiles/{year['id']}/17/{tile.x}/{tile.y}.png", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert "private" in response.headers["cache-control"]
    data = _png_array(response.content)
    assert data.shape[1:] == (256, 256)
    assert len(np.unique(data.reshape(data.shape[0], -1), axis=1).T) > 1

    pacific = TMS.tile(-150.0, 20.0, 17)
    response = client.get(
        f"/api/tiles/{year['id']}/17/{pacific.x}/{pacific.y}.png", headers=headers
    )
    assert response.status_code == 204

    assert client.get(f"/api/tiles/{year['id']}/3/0/0.png", headers=headers).status_code == 422
    assert (
        client.get(
            f"/api/tiles/{year['id']}/17/{tile.x}/{tile.y}.png",
            headers=tenant_with_admin["reviewer_headers"],
        ).status_code
        == 200
    )

    from ptax.db.models import Tenant, User, UserRole

    other = Tenant(name="Other County", state="MN", fips="27001")
    db.add(other)
    db.flush()
    other_sub = str(uuid.uuid4())
    db.add(
        User(tenant_id=other.id, cognito_sub=other_sub, email="a@other.test", role=UserRole.admin)
    )
    db.flush()
    other_headers = {"Authorization": f"Bearer {make_token(other_sub)}"}
    response = client.get(
        f"/api/tiles/{year['id']}/17/{tile.x}/{tile.y}.png", headers=other_headers
    )
    assert response.status_code == 404

    # Zoom hints for MapLibre: 1 m/px imagery tops out around zoom 17.
    assert year["min_zoom"] == 8
    assert 16 <= year["max_zoom"] <= 18
    assert years[2025]["max_zoom"] == year["max_zoom"] + 1


def test_read_parcel_masks_and_coverage(
    db: Session, settings: Settings, tenant_with_admin, ingested_layer, years
) -> None:
    tenant = tenant_with_admin["tenant"]
    parcel = _parcel(db, ingested_layer["id"], "27-053-000003")
    geom = to_shape(parcel.geom)

    assets = assets_intersecting(db, tenant.id, uuid.UUID(years[2023]["id"]), geom.bounds)
    assert len(assets) == 1
    raster = read_parcel(settings, assets, geom, resolution_m=1.0)
    assert raster is not None
    assert raster.data.shape[0] == 4
    assert raster.data.dtype == np.uint8
    assert raster.resolution_m == 1.0
    assert raster.mask.all()
    # The parcel mask area is within 5% of the parcel's true area in m2.
    to_utm = Transformer.from_crs(4326, 32615, always_xy=True).transform
    from shapely.ops import transform

    true_area = transform(to_utm, geom).area
    assert abs(raster.parcel_mask.sum() * raster.resolution_m**2 - true_area) / true_area < 0.05
    # Roof pixels are brighter/greyer than the field around them.
    roof = raster.data[:3, raster.parcel_mask].mean(axis=0)
    assert roof.max() > 140 and roof.min() < 130

    # Parcel 000005 (column 4) lies outside the partial 2025 ortho: nothing to read.
    outside = to_shape(_parcel(db, ingested_layer["id"], "27-053-000005").geom)
    assets = assets_intersecting(db, tenant.id, uuid.UUID(years[2025]["id"]), outside.bounds)
    assert assets == []
    assert read_parcel(settings, assets, outside, resolution_m=1.0) is None

    # Parcel 000003 (column 2) is covered by the 3-band ortho; a buffer that reaches into
    # column 3 shows invalid pixels in the mask.
    assets = assets_intersecting(db, tenant.id, uuid.UUID(years[2025]["id"]), geom.bounds)
    raster = read_parcel(settings, assets, geom, resolution_m=0.5, buffer_m=60)
    assert raster is not None
    assert raster.data.shape[0] == 3
    assert raster.mask[raster.parcel_mask].all()
    assert not raster.mask.all()


def test_read_parcel_uris_is_the_same_warp_as_read_parcel(
    db: Session, settings: Settings, tenant_with_admin, ingested_layer, years
) -> None:
    """The evaluation harness reads by URI; production reads by asset row.

    Both must land on the same grid through the same warp, because the detector defect the
    harness measures is a property of how two years are resampled onto one comparison
    grid. A harness with its own reader would be measuring a different program.
    """
    tenant = tenant_with_admin["tenant"]
    geom = to_shape(_parcel(db, ingested_layer["id"], "27-053-000003").geom)
    assets = assets_intersecting(db, tenant.id, uuid.UUID(years[2023]["id"]), geom.bounds)

    by_asset = read_parcel(settings, assets, geom, resolution_m=1.0)
    by_uri = read_parcel_uris(
        gdal_env(settings),
        [s3_uri(settings, a.s3_key) for a in assets],
        geom,
        resolution_m=1.0,
    )

    assert by_asset is not None and by_uri is not None
    assert np.array_equal(by_uri.data, by_asset.data)
    assert np.array_equal(by_uri.mask, by_asset.mask)
    assert np.array_equal(by_uri.parcel_mask, by_asset.parcel_mask)
    assert by_uri.transform == by_asset.transform
    assert by_uri.crs == by_asset.crs
    assert by_uri.bounds == by_asset.bounds

    assert read_parcel_uris(gdal_env(settings), [], geom, resolution_m=1.0) is None


def test_thumbnail_and_parcel_preview(
    client: TestClient, db: Session, tenant_with_admin, ingested_layer, years
) -> None:
    headers = tenant_with_admin["admin_headers"]
    year = years[2023]

    response = client.get(f"/api/imagery/years/{year['id']}/thumbnail.png", headers=headers)
    assert response.status_code == 200, response.text
    data = _png_array(response.content)
    assert max(data.shape[1:]) <= 256

    parcel = _parcel(db, ingested_layer["id"], "27-053-000003")
    response = client.get(
        f"/api/imagery/years/{year['id']}/parcels/{parcel.id}/preview.png?size=256&outline=1",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = _png_array(response.content)
    assert max(data.shape[1:]) == 256
    rgb = data[:3]
    yellow = (rgb[0] > 200) & (rgb[1] > 200) & (rgb[2] < 60)
    assert yellow.sum() > 100
    bounds = [float(v) for v in response.headers["x-bounds"].split(",")]
    geom = to_shape(parcel.geom)
    assert bounds[0] < geom.bounds[0] and bounds[2] > geom.bounds[2]
    assert bounds[1] < geom.bounds[1] and bounds[3] > geom.bounds[3]

    # No outline -> no yellow; other tenant's parcel -> 404; no imagery -> 204.
    response = client.get(
        f"/api/imagery/years/{year['id']}/parcels/{parcel.id}/preview.png", headers=headers
    )
    rgb = _png_array(response.content)[:3]
    assert ((rgb[0] > 200) & (rgb[1] > 200) & (rgb[2] < 60)).sum() == 0
    assert (
        client.get(
            f"/api/imagery/years/{year['id']}/parcels/{uuid.uuid4()}/preview.png", headers=headers
        ).status_code
        == 404
    )
    uncovered = _parcel(db, ingested_layer["id"], "27-053-000005")
    response = client.get(
        f"/api/imagery/years/{years[2025]['id']}/parcels/{uncovered.id}/preview.png",
        headers=headers,
    )
    assert response.status_code == 204
