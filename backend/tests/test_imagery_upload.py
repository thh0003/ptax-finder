import uuid
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin
from sqlalchemy import select
from sqlalchemy.orm import Session

from ptax.config import Settings
from ptax.db.models import ImageryAsset
from ptax.storage import get_s3_client
from tests.conftest import (
    FIXTURES_DIR,
    PARCELS_GEOJSON,
    drain_queue,
    ingest_parcels,
    upload_object,
)

IMAGERY_DIR = FIXTURES_DIR / "imagery"


def _create_year(client: TestClient, headers: dict, year: int, provider: str | None = None):
    body = {"year": year, "provider": provider}
    return client.post("/api/imagery/years", json=body, headers=headers)


def _register(client: TestClient, headers: dict, year_id: str, key: str, name: str):
    return client.post(
        f"/api/imagery/years/{year_id}/assets",
        json={"upload_key": key, "original_filename": name},
        headers=headers,
    )


def _finalize(client: TestClient, headers: dict, year_id: str):
    return client.post(f"/api/imagery/years/{year_id}/finalize", headers=headers)


def _get(client: TestClient, headers: dict, year_id: str) -> dict:
    response = client.get(f"/api/imagery/years/{year_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _upload_key(settings: Settings, tenant_id: uuid.UUID, name: str, data: bytes) -> str:
    return upload_object(settings, f"tenants/{tenant_id}/imagery/{uuid.uuid4()}/{name}", data)


def _write_tif(path: Path, data: np.ndarray, *, crs: str | None = "EPSG:26915") -> bytes:
    bands, height, width = data.shape
    profile = {
        "driver": "GTiff",
        "dtype": data.dtype.name,
        "count": bands,
        "height": height,
        "width": width,
        "transform": from_origin(440000, 4990000, 0.5, 0.5),
    }
    if crs:
        profile["crs"] = crs
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
    return path.read_bytes()


def test_partial_ortho_upload_reports_coverage_gap(
    client: TestClient, db: Session, settings: Settings, tenant_with_admin, ingested_layer
) -> None:
    headers = tenant_with_admin["admin_headers"]
    tenant = tenant_with_admin["tenant"]

    response = _create_year(client, headers, 2025, "Nearmap")
    assert response.status_code == 201, response.text
    year = response.json()
    assert year["status"] == "queued" and year["source"] == "upload"
    assert year["provider"] == "Nearmap"
    assert _create_year(client, headers, 2025).status_code == 409

    key = _upload_key(
        settings, tenant.id, "ortho.tif", (IMAGERY_DIR / "ortho_2025_partial.tif").read_bytes()
    )
    response = _register(client, headers, year["id"], key, "ortho_2025_partial.tif")
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"
    # Finalize before any asset is registered on another year -> 409; here it is fine.
    response = _finalize(client, headers, year["id"])
    assert response.status_code == 202, response.text
    assert _finalize(client, headers, year["id"]).status_code == 409  # already processing
    drain_queue(db)

    year = _get(client, headers, year["id"])
    assert year["status"] == "ready", year.get("error")
    assert year["band_count"] == 3
    assert year["resolution_m"] == 0.5
    assert year["coverage_pct"] == 60
    assert year["parcels_uncovered"] == 10
    assert [a["status"] for a in year["assets"]] == ["ready"]
    assert year["assets"][0]["original_filename"] == "ortho_2025_partial.tif"
    asset = db.execute(
        select(ImageryAsset).where(ImageryAsset.year_id == uuid.UUID(year["id"]))
    ).scalar_one()
    assert asset.epsg == 3857 and asset.band_count == 3 and asset.resolution_m == 0.5
    assert asset.s3_key.startswith(f"tenants/{tenant.id}/imagery/{year['id']}/")
    # The original upload object is gone; the stored COG exists.
    s3 = get_s3_client(settings)
    assert "Contents" not in s3.list_objects_v2(Bucket=settings.s3_bucket, Prefix=key)
    s3.head_object(Bucket=settings.s3_bucket, Key=asset.s3_key)

    # A re-ingested parcel layer with only the first 10 parcels (rows 0-1) recomputes the
    # gap report: 6 of those are in the covered columns 0-2.
    subset = gpd.read_file(PARCELS_GEOJSON).head(10)
    ingest_parcels(client, db, settings, tenant, headers, subset.to_json().encode())
    drain_queue(db)
    year = _get(client, headers, year["id"])
    assert year["parcels_uncovered"] == 4
    assert year["coverage_pct"] == 60


def test_invalid_files_fail_per_asset_and_year_recovers(
    client: TestClient,
    db: Session,
    settings: Settings,
    tenant_with_admin,
    ingested_layer,
    tmp_path: Path,
) -> None:
    headers = tenant_with_admin["admin_headers"]
    tenant = tenant_with_admin["tenant"]
    year_id = _create_year(client, headers, 2026).json()["id"]

    bogus = _upload_key(settings, tenant.id, "bogus.tif", b"this is not a tiff")
    _register(client, headers, year_id, bogus, "bogus.tif")
    _finalize(client, headers, year_id)
    drain_queue(db)
    year = _get(client, headers, year_id)
    assert year["status"] == "failed"
    assert "no valid imagery files" in year["error"]
    assert year["assets"][0]["status"] == "failed"
    assert "bogus.tif" in year["assets"][0]["error"]
    assert "not a readable GeoTIFF" in year["assets"][0]["error"]

    # A one-band file, a float file, a file without CRS, and a too-coarse file all fail
    # with their own reasons; a uint16 file is stretched to 8-bit and succeeds.
    one_band = _write_tif(tmp_path / "one.tif", np.full((1, 32, 32), 100, np.uint8))
    floats = _write_tif(tmp_path / "f.tif", np.full((3, 32, 32), 0.5, np.float32))
    no_crs = _write_tif(tmp_path / "nocrs.tif", np.full((3, 32, 32), 100, np.uint8), crs=None)
    sixteen = np.zeros((3, 64, 64), np.uint16)
    sixteen[:] = np.linspace(1000, 20000, 64, dtype=np.uint16)[None, None, :]
    sixteen_bytes = _write_tif(tmp_path / "u16.tif", sixteen)
    coarse_path = tmp_path / "coarse.tif"
    with rasterio.open(
        coarse_path,
        "w",
        driver="GTiff",
        dtype="uint8",
        count=3,
        height=16,
        width=16,
        crs="EPSG:26915",
        transform=from_origin(440000, 4990000, 2.0, 2.0),
    ) as dst:
        dst.write(np.full((3, 16, 16), 100, np.uint8))

    for name, data in [
        ("one.tif", one_band),
        ("f.tif", floats),
        ("nocrs.tif", no_crs),
        ("coarse.tif", coarse_path.read_bytes()),
        ("u16.tif", sixteen_bytes),
    ]:
        key = _upload_key(settings, tenant.id, name, data)
        assert _register(client, headers, year_id, key, name).status_code == 201
    assert _finalize(client, headers, year_id).status_code == 202
    drain_queue(db)

    year = _get(client, headers, year_id)
    assert year["status"] == "ready", year.get("error")
    by_name = {a["original_filename"]: a for a in year["assets"]}
    assert by_name["bogus.tif"]["status"] == "failed"  # still failed from the first pass
    assert "1 band(s); 3 or 4 required" in by_name["one.tif"]["error"]
    assert "float32 pixels are not supported" in by_name["f.tif"]["error"]
    assert "no coordinate reference system" in by_name["nocrs.tif"]["error"]
    assert "coarser than 1 m" in by_name["coarse.tif"]["error"]
    assert by_name["u16.tif"]["status"] == "ready"

    asset = db.execute(
        select(ImageryAsset).where(
            ImageryAsset.year_id == uuid.UUID(year_id), ImageryAsset.status == "ready"
        )
    ).scalar_one()
    body = get_s3_client(settings).get_object(Bucket=settings.s3_bucket, Key=asset.s3_key)["Body"]
    local = tmp_path / "u16_out.tif"
    local.write_bytes(body.read())
    with rasterio.open(local) as src:
        assert src.dtypes[0] == "uint8"
        assert np.percentile(src.read(1), 98) >= 250


def test_upload_year_permissions_and_validation(
    client: TestClient,
    db: Session,
    settings: Settings,
    tenant_with_admin,
    ingested_layer,
    make_token,
) -> None:
    headers = tenant_with_admin["admin_headers"]
    tenant = tenant_with_admin["tenant"]
    reviewer = tenant_with_admin["reviewer_headers"]

    assert _create_year(client, reviewer, 2025).status_code == 403
    assert _create_year(client, headers, 1800).status_code == 422
    year_id = _create_year(client, headers, 2025).json()["id"]

    # Presigned upload for imagery lands under the tenant's imagery prefix.
    response = client.post(
        "/api/uploads",
        json={"filename": "a.tif", "content_type": "image/tiff", "purpose": "imagery"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["key"].startswith(f"tenants/{tenant.id}/imagery/")

    # Keys outside the tenant's imagery prefix are refused; reviewers cannot register.
    other_key = f"tenants/{uuid.uuid4()}/imagery/{uuid.uuid4()}/x.tif"
    assert _register(client, headers, year_id, other_key, "x.tif").status_code == 422
    good_key = _upload_key(settings, tenant.id, "a.tif", b"x")
    assert _register(client, reviewer, year_id, good_key, "a.tif").status_code == 403
    # Finalize with nothing pending -> 409.
    assert _finalize(client, headers, year_id).status_code == 409

    # Other tenant sees nothing.
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
    assert _register(client, other_headers, year_id, good_key, "a.tif").status_code == 404
    assert _finalize(client, other_headers, year_id).status_code == 404
    # No parcel layer yet for the other tenant -> upload years cannot be created.
    assert _create_year(client, other_headers, 2025).status_code == 409
