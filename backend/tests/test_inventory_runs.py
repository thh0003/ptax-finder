import uuid

import pytest
from fastapi.testclient import TestClient
from geoalchemy2.shape import to_shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import select
from sqlalchemy.orm import Session

from ptax.config import Settings
from ptax.db.models import Job, Parcel, Run, RunParcel
from ptax.detection import run as run_module
from ptax.detection.detector import ParcelRaster
from ptax.vision.client import VisionClient, VisionUnavailable
from ptax.vision.inventory import Inventory, InventoryError, Structure
from ptax.worker import run_once
from tests.conftest import drain_queue, ingest_naip_year


@pytest.fixture
def vision_key(app, settings: Settings) -> Settings:
    """The API sees a configured model; the worker's client is stubbed per test."""
    app.state.settings = settings.model_copy(update={"vision_api_key": "test-key"})
    return app.state.settings


@pytest.fixture
def year(client, db, tenant_with_admin, ingested_layer) -> dict:
    return ingest_naip_year(client, db, tenant_with_admin["admin_headers"], 2021)


class StubModel:
    """Stands in for `inventory_parcel`: a house on every parcel, with failures on cue."""

    def __init__(self, fail_on: dict[int, Exception] | None = None) -> None:
        self.calls = 0
        self.fail_on = fail_on or {}
        self.unavailable = False

    def __call__(
        self, client: VisionClient, raster: ParcelRaster, parcel: BaseGeometry
    ) -> Inventory:
        self.calls += 1
        if self.unavailable:
            raise VisionUnavailable("connection refused")
        if self.calls in self.fail_on:
            raise self.fail_on[self.calls]
        house = parcel.centroid.buffer(3)
        return Inventory(
            structures=[Structure("house", 0.8, (400, 400, 600, 600), house)],
            summary="A house on a lawn.",
            raw="{}",
        )


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> StubModel:
    model = StubModel(fail_on={5: InventoryError("invalid reply after one retry", "garbled")})
    monkeypatch.setattr(run_module, "inventory_parcel", model)
    monkeypatch.setattr(run_module, "vision_client", lambda settings: None)
    monkeypatch.setattr(run_module, "VISION_RETRY_DELAYS", ())
    return model


def _start(client: TestClient, headers: dict, year_id: str):  # noqa: ANN202
    return client.post("/api/runs/inventory", json={"year_id": year_id}, headers=headers)


def _rows(db: Session, run_id: str) -> list[RunParcel]:
    return list(
        db.execute(select(RunParcel).where(RunParcel.run_id == uuid.UUID(run_id))).scalars()
    )


def test_an_inventory_stores_each_parcels_structures(
    client: TestClient, db: Session, tenant_with_admin, year, vision_key, stub
) -> None:
    headers = tenant_with_admin["admin_headers"]
    response = _start(client, headers, year["id"])
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["kind"] == "inventory" and run["target_year"] is None
    assert run["base_year"]["year"] == 2021
    assert (run["detector"], run["model_name"]) == ("vision", "qwen3-vl")
    assert run["parcels_total"] == 25

    drain_queue(db)
    run = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    assert run["status"] == "succeeded", run["error"]
    assert (run["parcels_processed"], run["parcels_skipped"], run["candidates"]) == (25, 1, 0)

    rows = _rows(db, run["id"])
    (failed,) = [r for r in rows if r.skipped_reason is not None]
    assert failed.skipped_reason == "model_error"
    assert failed.indicators["raw"] == "garbled" and failed.structure_geom is None
    for row in (r for r in rows if r.skipped_reason is None):
        assert row.score == 0.8 and row.candidate is False
        indicators = row.indicators
        assert indicators["kinds"] == ["house"] and indicators["counts"] == {"house": 1}
        assert indicators["summary"] == "A house on a lawn."
        assert indicators["model"] == "qwen3-vl" and indicators["resolution_m"] > 0
        (structure,) = indicators["structures"]
        assert structure == {"kind": "house", "confidence": 0.8, "box": [400, 400, 600, 600]}
        # The markup is the structure, in EPSG:4326, on the parcel.
        markup = to_shape(row.structure_geom)
        parcel = db.get_one(Parcel, row.parcel_id)
        assert markup.within(to_shape(parcel.geom).buffer(1e-6))


def test_the_parcel_list_filters_by_structure_kind_and_the_detail_has_the_county_record(
    client: TestClient, db: Session, tenant_with_admin, year, vision_key, stub
) -> None:
    headers = tenant_with_admin["admin_headers"]
    run_id = _start(client, headers, year["id"]).json()["id"]
    drain_queue(db)

    def listed(**params) -> dict:  # noqa: ANN003
        response = client.get(f"/api/runs/{run_id}/parcels", params=params, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    houses = listed(structure="house")
    assert houses["total"] == 24
    # The list carries each parcel's summary and kinds, so it reads without opening each.
    assert houses["items"][0]["summary"] == "A house on a lawn."
    assert houses["items"][0]["kinds"] == ["house"]
    assert listed(structure="pool")["total"] == 0
    assert listed()["total"] == 25
    bad = client.get(f"/api/runs/{run_id}/parcels", params={"structure": "barn"}, headers=headers)
    assert bad.status_code == 422

    parcel_id = listed(structure="house")["items"][0]["parcel_id"]
    detail = client.get(f"/api/runs/{run_id}/parcels/{parcel_id}", headers=headers).json()
    assert detail["has_markup"] is True
    assert detail["parcel_attributes"]["PIN"].startswith("27-053-")
    overlay = client.get(f"/api/runs/{run_id}/parcels/{parcel_id}/overlay.png", headers=headers)
    assert overlay.status_code == 200 and overlay.headers["content-type"] == "image/png"


def test_change_and_inventory_runs_list_side_by_side(
    client: TestClient, db: Session, tenant_with_admin, year, vision_key, stub
) -> None:
    headers = tenant_with_admin["admin_headers"]
    later = ingest_naip_year(client, db, headers, 2023)
    change = client.post(
        "/api/runs",
        json={"base_year_id": year["id"], "target_year_id": later["id"], "detector": "classical"},
        headers=headers,
    ).json()
    inventory = _start(client, headers, year["id"]).json()
    drain_queue(db)

    runs = {r["id"]: r for r in client.get("/api/runs", headers=headers).json()}
    assert runs[change["id"]]["kind"] == "change"
    assert runs[change["id"]]["target_year"]["year"] == 2023
    assert runs[inventory["id"]]["kind"] == "inventory"
    assert runs[inventory["id"]]["target_year"] is None


def test_an_inventory_is_refused_without_a_model_key(
    client: TestClient, db: Session, app, settings: Settings, tenant_with_admin, year
) -> None:
    app.state.settings = settings.model_copy(update={"vision_api_key": None})
    response = _start(client, tenant_with_admin["admin_headers"], year["id"])
    assert response.status_code == 409
    assert response.json()["detail"] == "vision model is not configured (VISION_API_KEY)"
    assert db.execute(select(Run)).scalars().first() is None


def test_an_inventory_needs_a_ready_year_of_this_tenant(
    client: TestClient, tenant_with_admin, ingested_layer, vision_key
) -> None:
    response = _start(client, tenant_with_admin["admin_headers"], str(uuid.uuid4()))
    assert response.status_code == 404
    reviewer = client.post(
        "/api/runs/inventory",
        json={"year_id": str(uuid.uuid4())},
        headers=tenant_with_admin["reviewer_headers"],
    )
    assert reviewer.status_code == 403


def test_a_model_outage_leaves_the_run_running_and_it_resumes(
    client: TestClient,
    db: Session,
    tenant_with_admin,
    year,
    vision_key,
    stub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = tenant_with_admin["admin_headers"]
    monkeypatch.setattr(run_module, "INVENTORY_BATCH_SIZE", 5)
    run_id = _start(client, headers, year["id"]).json()["id"]
    stub.fail_on = {}

    original = stub.__call__

    def go_down_after_ten(*args):  # noqa: ANN002, ANN202
        if stub.calls >= 10:
            stub.unavailable = True
        return original(*args)

    monkeypatch.setattr(run_module, "inventory_parcel", go_down_after_ten)
    assert run_once(db) is not None

    job = db.execute(select(Job).where(Job.type == "run.execute")).scalar_one()
    assert job.status == "queued", job.error  # re-queued for another attempt
    run = client.get(f"/api/runs/{run_id}", headers=headers).json()
    assert run["status"] == "running" and run["parcels_processed"] == 10

    stub.unavailable = False
    monkeypatch.setattr(run_module, "inventory_parcel", stub)
    calls_before = stub.calls
    drain_queue(db)
    run = client.get(f"/api/runs/{run_id}", headers=headers).json()
    assert run["status"] == "succeeded", run["error"]
    assert run["parcels_processed"] == 25 and len(_rows(db, run_id)) == 25
    assert stub.calls - calls_before == 15  # only the parcels still to do


def test_a_model_down_for_every_attempt_fails_the_run(
    client: TestClient, db: Session, tenant_with_admin, year, vision_key, stub
) -> None:
    headers = tenant_with_admin["admin_headers"]
    run_id = _start(client, headers, year["id"]).json()["id"]
    stub.unavailable = True
    drain_queue(db)
    run = client.get(f"/api/runs/{run_id}", headers=headers).json()
    assert run["status"] == "failed"
    assert "connection refused" in run["error"]
    job = db.execute(select(Job).where(Job.type == "run.execute")).scalar_one()
    assert job.status == "failed"


def test_an_inventory_can_be_cancelled_between_batches(
    client: TestClient,
    db: Session,
    tenant_with_admin,
    year,
    vision_key,
    stub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = tenant_with_admin["admin_headers"]
    monkeypatch.setattr(run_module, "INVENTORY_BATCH_SIZE", 5)
    run_id = _start(client, headers, year["id"]).json()["id"]
    original = run_module.flush_batch

    def cancel_after_first_batch(db_: Session, run: Run, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        original(db_, run, *args, **kwargs)
        client.post(f"/api/runs/{run_id}/cancel", headers=headers)

    monkeypatch.setattr(run_module, "flush_batch", cancel_after_first_batch)
    drain_queue(db)
    run = client.get(f"/api/runs/{run_id}", headers=headers).json()
    assert run["status"] == "cancelled" and run["parcels_processed"] == 5
