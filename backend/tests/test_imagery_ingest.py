import uuid
from dataclasses import replace
from pathlib import Path

import pytest
import rasterio
from fastapi.testclient import TestClient
from geoalchemy2.shape import to_shape
from rio_cogeo.cogeo import cog_validate
from shapely.geometry.base import BaseGeometry
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ptax import worker
from ptax.config import Settings
from ptax.db.models import ImageryAsset, ImageryYear, Job, Parcel
from ptax.imagery import ingest
from ptax.imagery.fixture import FixtureNaipSource
from ptax.imagery.sources import NaipItem, NaipYear
from ptax.storage import get_s3_client
from ptax.worker import run_once
from tests.conftest import FIXTURES_DIR, drain_queue

IMAGERY_DIR = FIXTURES_DIR / "imagery"


def _ingest(client: TestClient, headers: dict, year: int) -> object:
    return client.post("/api/imagery/naip/ingest", json={"year": year}, headers=headers)


def _year(client: TestClient, headers: dict, year_id: str) -> dict:
    response = client.get(f"/api/imagery/years/{year_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_naip_ingest_copies_clipped_cog_and_computes_coverage(
    client: TestClient,
    db: Session,
    settings: Settings,
    tenant_with_admin,
    ingested_layer,
    tmp_path: Path,
) -> None:
    headers = tenant_with_admin["admin_headers"]
    tenant = tenant_with_admin["tenant"]

    response = _ingest(client, headers, 2021)
    assert response.status_code == 201, response.text
    year = response.json()
    assert year["status"] == "queued" and year["source"] == "naip" and year["year"] == 2021
    drain_queue(db)

    year = _year(client, headers, year["id"])
    assert year["status"] == "ready", year.get("error")
    assert year["band_count"] == 4
    assert year["resolution_m"] == 1.0
    assert year["coverage_pct"] == 100
    assert year["parcels_uncovered"] == 0

    assets = (
        db.execute(select(ImageryAsset).where(ImageryAsset.year_id == uuid.UUID(year["id"])))
        .scalars()
        .all()
    )
    assert len(assets) == 1
    asset = assets[0]
    assert asset.status == "ready"
    assert asset.s3_key.startswith(f"tenants/{tenant.id}/imagery/{year['id']}/")
    assert asset.source_ref == "naip_2021"
    assert asset.epsg == 26915 and asset.band_count == 4 and asset.resolution_m == 1.0
    assert asset.size_bytes and asset.size_bytes > 0

    # The stored object is a valid 4-band COG whose bounds contain every parcel.
    body = get_s3_client(settings).get_object(Bucket=settings.s3_bucket, Key=asset.s3_key)["Body"]
    local = tmp_path / "downloaded.tif"
    local.write_bytes(body.read())
    assert cog_validate(str(local))[0]
    with rasterio.open(local) as src:
        assert src.count == 4
        assert "alpha" not in [c.name for c in src.colorinterp]
    bounds = to_shape(asset.bounds)
    parcels_outside = db.execute(
        select(func.count())
        .select_from(Parcel)
        .where(
            Parcel.layer_id == uuid.UUID(ingested_layer["id"]),
            ~func.ST_Covers(func.ST_GeomFromText(bounds.wkt, 4326), Parcel.geom),
        )
    ).scalar_one()
    assert parcels_outside == 0

    # Listing shows the year, and NAIP availability now reports it as existing.
    listed = client.get("/api/imagery/years", headers=headers).json()
    assert [y["id"] for y in listed] == [year["id"]]
    available = client.get("/api/imagery/naip/available", headers=headers).json()
    assert available[0]["existing"] == {"id": year["id"], "status": "ready"}


def test_naip_ingest_conflicts_cap_and_retry(
    app,
    client: TestClient,
    db: Session,
    settings: Settings,
    tenant_with_admin,
    ingested_layer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = tenant_with_admin["admin_headers"]

    # Over the cap: refused before any row is written.
    app.state.settings = settings.model_copy(update={"naip_max_ingest_gb": 0.0001})
    response = _ingest(client, headers, 2021)
    assert response.status_code == 422
    assert "exceeds NAIP_MAX_INGEST_GB" in response.json()["detail"]
    assert db.execute(select(func.count()).select_from(ImageryYear)).scalar_one() == 0
    app.state.settings = settings

    # Unknown year -> 404; reviewer -> 403.
    assert _ingest(client, headers, 1999).status_code == 404
    assert _ingest(client, tenant_with_admin["reviewer_headers"], 2021).status_code == 403

    # A source whose item cannot be opened leaves the year failed, naming the item.
    class BrokenSource(FixtureNaipSource):
        def items_for(self, year: int, footprint: BaseGeometry) -> list[NaipItem]:
            return [
                replace(i, href=str(IMAGERY_DIR / "missing.tif"))
                for i in super().items_for(year, footprint)
            ]

    monkeypatch.setattr(ingest, "get_naip_source", lambda s: BrokenSource(IMAGERY_DIR))
    response = _ingest(client, headers, 2023)
    assert response.status_code == 201
    year_id = response.json()["id"]
    assert _ingest(client, headers, 2023).status_code == 409  # already queued
    drain_queue(db)
    year = _year(client, headers, year_id)
    assert year["status"] == "failed"
    assert "naip_2023" in year["error"]

    # Fixing the source and retrying resets the failed year and succeeds.
    monkeypatch.setattr(ingest, "get_naip_source", lambda s: FixtureNaipSource(IMAGERY_DIR))
    response = _ingest(client, headers, 2023)
    assert response.status_code == 201
    assert response.json()["id"] == year_id
    assert response.json()["status"] == "queued"
    drain_queue(db)
    year = _year(client, headers, year_id)
    assert year["status"] == "ready", year.get("error")
    assert year["error"] is None
    assets = (
        db.execute(select(ImageryAsset.status).where(ImageryAsset.year_id == uuid.UUID(year_id)))
        .scalars()
        .all()
    )
    assert assets == ["ready"]
    assert _ingest(client, headers, 2023).status_code == 409  # ready is final


def test_naip_ingest_resumes_after_worker_stop(
    client: TestClient,
    db: Session,
    settings: Settings,
    tenant_with_admin,
    ingested_layer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = tenant_with_admin["admin_headers"]

    class TwoItemSource(FixtureNaipSource):
        def items_for(self, year: int, footprint: BaseGeometry) -> list[NaipItem]:
            (item,) = super().items_for(year, footprint)
            return [item, replace(item, id=item.id + "_b")]

        def list_years(self, footprint: BaseGeometry) -> list[NaipYear]:
            return super().list_years(footprint)

    monkeypatch.setattr(ingest, "get_naip_source", lambda s: TwoItemSource(IMAGERY_DIR))
    year_id = _ingest(client, headers, 2021).json()["id"]

    # Stop is requested after the first item has been committed.
    registered: list[int] = []
    original = ingest.register_asset

    def register_then_stop(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        asset = original(*args, **kwargs)
        registered.append(1)
        monkeypatch.setattr(worker, "_stop", True)
        return asset

    monkeypatch.setattr(ingest, "register_asset", register_then_stop)
    # Like the worker loop: run jobs until a stop is requested (drain_queue would keep
    # re-claiming the re-queued job while the flag is set).
    while run_once(db) is not None and not worker.stop_requested():
        pass
    monkeypatch.setattr(worker, "_stop", False)

    job = db.execute(select(Job).where(Job.type == "imagery.ingest_naip")).scalar_one()
    assert job.status == "queued" and job.attempts == 0
    year = _year(client, headers, year_id)
    assert year["status"] == "processing"
    assert len(registered) == 1

    monkeypatch.setattr(ingest, "register_asset", original)
    drain_queue(db)
    year = _year(client, headers, year_id)
    assert year["status"] == "ready", year.get("error")
    refs = (
        db.execute(
            select(ImageryAsset.source_ref).where(ImageryAsset.year_id == uuid.UUID(year_id))
        )
        .scalars()
        .all()
    )
    assert sorted(refs) == ["naip_2021", "naip_2021_b"]


def test_naip_ingest_is_tenant_scoped(
    client: TestClient, db: Session, tenant_with_admin, ingested_layer, make_token
) -> None:
    from ptax.db.models import Tenant, User, UserRole

    headers = tenant_with_admin["admin_headers"]
    year_id = _ingest(client, headers, 2021).json()["id"]
    other = Tenant(name="Other County", state="MN", fips="27001")
    db.add(other)
    db.flush()
    other_sub = str(uuid.uuid4())
    db.add(
        User(tenant_id=other.id, cognito_sub=other_sub, email="a@other.test", role=UserRole.admin)
    )
    db.flush()
    other_headers = {"Authorization": f"Bearer {make_token(other_sub)}"}
    assert client.get("/api/imagery/years", headers=other_headers).json() == []
    assert client.get(f"/api/imagery/years/{year_id}", headers=other_headers).status_code == 404
    assert client.get("/api/imagery/naip/available", headers=other_headers).status_code == 409
