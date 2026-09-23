import io
import os
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ptax.config import Settings
from ptax.db.models import Parcel, Tenant
from ptax.storage import get_s3_client
from ptax.worker import run_once

FIXTURES = Path(__file__).parent / "fixtures"
GEOJSON = FIXTURES / "parcels_small.geojson"
SHAPEFILE_ZIP = FIXTURES / "parcels_small_26915.zip"


def _upload(settings: Settings, tenant_id: uuid.UUID, name: str, data: bytes) -> str:
    key = f"tenants/{tenant_id}/parcel_layer/{uuid.uuid4()}/{name}"
    get_s3_client(settings).put_object(Bucket=settings.s3_bucket, Key=key, Body=data)
    return key


def _drain(db: Session) -> None:
    while run_once(db) is not None:
        pass


def _create_layer(client: TestClient, headers: dict, key: str, filename: str) -> dict:
    response = client.post(
        "/api/parcel-layers",
        json={"upload_key": key, "original_filename": filename},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _get_layer(client: TestClient, headers: dict, layer_id: str) -> dict:
    response = client.get(f"/api/parcel-layers/{layer_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _ingest(client: TestClient, headers: dict, layer_id: str, field: str) -> None:
    response = client.post(
        f"/api/parcel-layers/{layer_id}/ingest",
        json={"parcel_id_field": field},
        headers=headers,
    )
    assert response.status_code == 202, response.text


def test_zip_shapefile_inspect_and_ingest(
    client: TestClient, db: Session, settings: Settings, tenant_with_admin
) -> None:
    tenant: Tenant = tenant_with_admin["tenant"]
    headers = tenant_with_admin["admin_headers"]
    key = _upload(settings, tenant.id, "parcels.zip", SHAPEFILE_ZIP.read_bytes())

    layer = _create_layer(client, headers, key, "parcels.zip")
    assert layer["status"] == "uploaded"
    _drain(db)

    layer = _get_layer(client, headers, layer["id"])
    assert layer["status"] == "awaiting_field", layer.get("error")
    assert layer["source_crs"] == "EPSG:26915"
    assert layer["feature_count"] == 25
    field_names = [f["name"] for f in layer["fields"]]
    assert "PIN" in field_names and "OWNER" in field_names
    pin_field = next(f for f in layer["fields"] if f["name"] == "PIN")
    assert pin_field["samples"][0] == "27-053-000001"

    _ingest(client, headers, layer["id"], "PIN")
    assert _get_layer(client, headers, layer["id"])["status"] == "ingesting"
    _drain(db)

    layer = _get_layer(client, headers, layer["id"])
    assert layer["status"] == "ready", layer.get("error")
    assert layer["skipped_count"] == 0
    assert layer["is_current"] is True
    db.refresh(tenant)
    assert str(tenant.current_parcel_layer_id) == layer["id"]

    # Centroids after reprojection must match the 4326 source within 1e-6 degrees.
    expected = gpd.read_file(GEOJSON).set_index("PIN").geometry.centroid
    rows = db.execute(
        select(
            Parcel.parcel_ref,
            func.ST_X(func.ST_Centroid(Parcel.geom)),
            func.ST_Y(func.ST_Centroid(Parcel.geom)),
        ).where(Parcel.layer_id == uuid.UUID(layer["id"]))
    ).all()
    assert len(rows) == 25
    for ref, x, y in rows:
        assert abs(x - expected[ref].x) < 1e-6 and abs(y - expected[ref].y) < 1e-6
    geom_types = (
        db.execute(
            text("SELECT DISTINCT ST_GeometryType(geom) FROM parcels WHERE layer_id = :id"),
            {"id": layer["id"]},
        )
        .scalars()
        .all()
    )
    assert geom_types == ["ST_MultiPolygon"]


def test_zip_without_dataset_fails_with_reason(
    client: TestClient, db: Session, settings: Settings, tenant_with_admin
) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "nothing to see here")
    key = _upload(settings, tenant_with_admin["tenant"].id, "junk.zip", buf.getvalue())
    headers = tenant_with_admin["admin_headers"]

    layer = _create_layer(client, headers, key, "junk.zip")
    _drain(db)

    layer = _get_layer(client, headers, layer["id"])
    assert layer["status"] == "failed"
    assert "no shapefile or GeoJSON found" in layer["error"]


def test_geojson_with_bad_rows_counts_skips(
    client: TestClient, db: Session, settings: Settings, tenant_with_admin
) -> None:
    gdf = gpd.read_file(GEOJSON)
    # Two rows with no PIN, one row that is a LineString: all three must be skipped.
    gdf.loc[0, "PIN"] = None
    gdf.loc[1, "PIN"] = ""
    line = gpd.GeoDataFrame(
        [{"PIN": "27-053-LINE", "OWNER": "Road"}],
        geometry=gpd.GeoSeries.from_wkt(["LINESTRING(-93.7 45.05, -93.69 45.06)"]),
        crs="EPSG:4326",
    )
    gdf = gpd.GeoDataFrame(gpd.pd.concat([gdf, line], ignore_index=True), crs="EPSG:4326")
    key = _upload(settings, tenant_with_admin["tenant"].id, "mixed.geojson", gdf.to_json().encode())
    headers = tenant_with_admin["admin_headers"]

    layer = _create_layer(client, headers, key, "mixed.geojson")
    _drain(db)
    _ingest(client, headers, layer["id"], "PIN")
    _drain(db)

    layer = _get_layer(client, headers, layer["id"])
    assert layer["status"] == "ready", layer.get("error")
    assert layer["skipped_count"] == 3
    count = db.execute(
        select(func.count()).select_from(Parcel).where(Parcel.layer_id == uuid.UUID(layer["id"]))
    ).scalar_one()
    assert count == 23


def test_reupload_keeps_old_parcels_and_switches_current(
    client: TestClient, db: Session, settings: Settings, tenant_with_admin
) -> None:
    tenant: Tenant = tenant_with_admin["tenant"]
    headers = tenant_with_admin["admin_headers"]

    first = _create_layer(
        client, headers, _upload(settings, tenant.id, "a.geojson", GEOJSON.read_bytes()), "a"
    )
    _drain(db)
    _ingest(client, headers, first["id"], "PIN")
    _drain(db)

    # Second layer: same parcels but only the first 10, to make the difference observable.
    subset = gpd.read_file(GEOJSON).head(10)
    second = _create_layer(
        client,
        headers,
        _upload(settings, tenant.id, "b.geojson", subset.to_json().encode()),
        "b",
    )
    _drain(db)
    _ingest(client, headers, second["id"], "PIN")
    _drain(db)

    history = client.get("/api/parcel-layers", headers=headers).json()
    assert [layer["id"] for layer in history] == [second["id"], first["id"]]
    assert [layer["is_current"] for layer in history] == [True, False]
    total = db.execute(
        select(func.count()).select_from(Parcel).where(Parcel.tenant_id == tenant.id)
    ).scalar_one()
    assert total == 35  # 25 + 10: the old layer's parcels are retained

    bbox = "-93.71,45.04,-93.60,45.10"
    features = client.get(f"/api/parcels?bbox={bbox}", headers=headers).json()["features"]
    assert len(features) == 10
    assert all(f["properties"]["layer_id"] == second["id"] for f in features)

    one = client.get("/api/parcels/by-ref/27-053-000001", headers=headers)
    assert one.status_code == 200
    assert one.json()["properties"]["parcel_ref"] == "27-053-000001"
    assert client.get("/api/parcels/by-ref/27-053-000020", headers=headers).status_code == 404


def test_parcels_are_tenant_scoped(
    client: TestClient, db: Session, settings: Settings, tenant_with_admin, make_token
) -> None:
    from ptax.db.models import User, UserRole

    tenant: Tenant = tenant_with_admin["tenant"]
    headers = tenant_with_admin["admin_headers"]
    layer = _create_layer(
        client, headers, _upload(settings, tenant.id, "a.geojson", GEOJSON.read_bytes()), "a"
    )
    _drain(db)
    _ingest(client, headers, layer["id"], "PIN")
    _drain(db)

    other = Tenant(name="Other County", state="MN", fips="27001")
    db.add(other)
    db.flush()
    other_sub = str(uuid.uuid4())
    db.add(
        User(
            tenant_id=other.id, cognito_sub=other_sub, email="admin@other.test", role=UserRole.admin
        )
    )
    db.flush()
    other_headers = {"Authorization": f"Bearer {make_token(other_sub)}"}

    assert client.get("/api/parcel-layers", headers=other_headers).json() == []
    assert client.get(f"/api/parcel-layers/{layer['id']}", headers=other_headers).status_code == 404
    bbox = "-93.71,45.04,-93.60,45.10"
    assert client.get(f"/api/parcels?bbox={bbox}", headers=other_headers).json()["features"] == []
    # A key under another tenant's prefix cannot be registered as one's own layer.
    response = client.post(
        "/api/parcel-layers",
        json={"upload_key": layer["s3_key"], "original_filename": "stolen.geojson"},
        headers=other_headers,
    )
    assert response.status_code == 422


def test_ingest_state_and_field_validation(
    client: TestClient, db: Session, settings: Settings, tenant_with_admin
) -> None:
    tenant: Tenant = tenant_with_admin["tenant"]
    headers = tenant_with_admin["admin_headers"]
    layer = _create_layer(
        client, headers, _upload(settings, tenant.id, "a.geojson", GEOJSON.read_bytes()), "a"
    )
    # Not inspected yet -> 409.
    response = client.post(
        f"/api/parcel-layers/{layer['id']}/ingest",
        json={"parcel_id_field": "PIN"},
        headers=headers,
    )
    assert response.status_code == 409
    _drain(db)
    # Unknown field -> 422.
    response = client.post(
        f"/api/parcel-layers/{layer['id']}/ingest",
        json={"parcel_id_field": "NOPE"},
        headers=headers,
    )
    assert response.status_code == 422
    # Reviewer cannot create layers.
    response = client.post(
        "/api/parcel-layers",
        json={"upload_key": "tenants/x/parcel_layer/y/z.zip", "original_filename": "z.zip"},
        headers=tenant_with_admin["reviewer_headers"],
    )
    assert response.status_code == 403


def test_bbox_over_limit_is_413(
    client: TestClient, db: Session, settings: Settings, tenant_with_admin
) -> None:
    tenant: Tenant = tenant_with_admin["tenant"]
    headers = tenant_with_admin["admin_headers"]
    layer = _create_layer(
        client, headers, _upload(settings, tenant.id, "a.geojson", GEOJSON.read_bytes()), "a"
    )
    _drain(db)
    _ingest(client, headers, layer["id"], "PIN")
    _drain(db)
    response = client.get("/api/parcels?bbox=-93.71,45.04,-93.60,45.10&limit=10", headers=headers)
    assert response.status_code == 413
    assert client.get("/api/parcels?bbox=not-a-bbox", headers=headers).status_code == 422


@pytest.mark.skipif(os.environ.get("PTAX_PERF") != "1", reason="set PTAX_PERF=1 to run")
def test_fifty_thousand_features_ingest_under_60s(
    client: TestClient, db: Session, settings: Settings, tenant_with_admin, tmp_path: Path
) -> None:
    subprocess.run(
        [
            sys.executable,
            str(FIXTURES / "make_fixtures.py"),
            "--count",
            "50000",
            "--out",
            str(tmp_path),
            "--stem",
            "perf",
        ],
        check=True,
    )
    tenant: Tenant = tenant_with_admin["tenant"]
    headers = tenant_with_admin["admin_headers"]
    key = _upload(settings, tenant.id, "perf.zip", (tmp_path / "perf_26915.zip").read_bytes())
    layer = _create_layer(client, headers, key, "perf.zip")
    _drain(db)
    _ingest(client, headers, layer["id"], "PIN")

    started = time.monotonic()
    _drain(db)
    elapsed = time.monotonic() - started

    layer = _get_layer(client, headers, layer["id"])
    assert layer["status"] == "ready", layer.get("error")
    assert layer["feature_count"] == 50000 and layer["skipped_count"] == 0
    assert elapsed < 60, f"ingest took {elapsed:.1f}s"
