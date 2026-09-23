import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ptax import worker
from ptax.api.runs import DEFAULT_MIN_NEW_AREA_M2
from ptax.config import Settings
from ptax.db.models import Job, Run, RunParcel
from ptax.detection import run as run_module
from ptax.worker import run_once
from tests.conftest import FIXTURES_DIR, drain_queue, ingest_naip_year, ingest_upload_year
from tests.test_detector import FIXTURE_THRESHOLD

IMAGERY_DIR = FIXTURES_DIR / "imagery"


@pytest.fixture
def years(client, db, settings, tenant_with_admin, ingested_layer) -> dict:
    headers = tenant_with_admin["admin_headers"]
    return {
        2021: ingest_naip_year(client, db, headers, 2021),
        2023: ingest_naip_year(client, db, headers, 2023),
        2025: ingest_upload_year(
            client,
            db,
            settings,
            tenant_with_admin["tenant"],
            headers,
            2025,
            {"ortho_2025_partial.tif": (IMAGERY_DIR / "ortho_2025_partial.tif").read_bytes()},
        ),
    }


def _start(client: TestClient, headers: dict, base: str, target: str, **extra):
    return client.post(
        "/api/runs", json={"base_year_id": base, "target_year_id": target, **extra}, headers=headers
    )


def _get(client: TestClient, headers: dict, run_id: str) -> dict:
    response = client.get(f"/api/runs/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _refs(db: Session, run_id: str, *, candidate: bool | None = None) -> set[str]:
    stmt = select(RunParcel.parcel_ref).where(RunParcel.run_id == uuid.UUID(run_id))
    if candidate is not None:
        stmt = stmt.where(RunParcel.candidate.is_(candidate))
    return {ref[-6:] for ref in db.execute(stmt).scalars()}


def test_run_scores_every_parcel_and_summarises(
    client: TestClient, db: Session, tenant_with_admin, years
) -> None:
    headers = tenant_with_admin["admin_headers"]
    response = _start(
        client, headers, years[2021]["id"], years[2023]["id"], threshold=FIXTURE_THRESHOLD
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "queued"
    assert run["parcels_total"] == 25
    assert run["threshold"] == FIXTURE_THRESHOLD
    assert run["min_new_area_m2"] == DEFAULT_MIN_NEW_AREA_M2  # 400 sq ft
    assert run["base_year"]["year"] == 2021 and run["target_year"]["source"] == "naip"
    drain_queue(db)

    run = _get(client, headers, run["id"])
    assert run["status"] == "succeeded", run.get("error")
    assert run["parcels_processed"] == 25
    assert run["candidates"] == 3
    assert run["parcels_skipped"] == 0
    assert run["started_at"] and run["finished_at"]
    assert _refs(db, run["id"], candidate=True) == {"000003", "000007", "000012"}
    # `.all()`, not the bare ScalarResult: it is a one-shot iterator, and the loop below
    # would otherwise exhaust it before the radiometric assertion could see any row.
    rows = db.execute(
        select(RunParcel).where(RunParcel.run_id == uuid.UUID(run["id"]))
    ).scalars().all()
    for row in rows:
        assert row.score is not None and row.skipped_reason is None
        assert "new_builtup_m2" in row.indicators
    # The run job fits one radiometric correction across a sample of parcels before
    # scoring and passes it into every comparison. Only the job builds that fit, so
    # without this the production path is covered solely by the candidate set happening
    # to come out right. The 2023 fixture carries a deliberate RECAPTURE_GAIN so there is
    # a real inter-capture difference for the fit to find.
    fitted = [r for r in rows if (r.indicators or {}).get("radiometric_gain") is not None]
    assert fitted, "no scored parcel carries the run-level radiometric fit"
    assert fitted[0].indicators["radiometric_sample_parcels"] > 0

    # 2023 -> 2025: the ten parcels outside the partial ortho are skipped, not scored.
    run2 = _start(
        client, headers, years[2023]["id"], years[2025]["id"], threshold=FIXTURE_THRESHOLD
    ).json()
    drain_queue(db)
    run2 = _get(client, headers, run2["id"])
    assert run2["status"] == "succeeded", run2.get("error")
    assert run2["parcels_processed"] == 25
    assert run2["candidates"] == 1
    assert run2["parcels_skipped"] == 10
    assert _refs(db, run2["id"], candidate=True) == {"000001"}
    skipped = db.execute(
        select(RunParcel.skipped_reason).where(
            RunParcel.run_id == uuid.UUID(run2["id"]), RunParcel.skipped_reason.is_not(None)
        )
    ).scalars()
    assert set(skipped) == {"no_coverage_target"}

    listed = client.get("/api/runs", headers=headers).json()
    assert [r["id"] for r in listed] == [run2["id"], run["id"]]


def test_run_validation_permissions_and_tenancy(
    client: TestClient, db: Session, tenant_with_admin, years, make_token
) -> None:
    headers = tenant_with_admin["admin_headers"]
    # Target must be later than base; same year twice is also refused.
    response = _start(client, headers, years[2023]["id"], years[2021]["id"])
    assert response.status_code == 422
    assert response.json()["detail"] == "Target year must be later than base year"
    assert _start(client, headers, years[2021]["id"], years[2021]["id"]).status_code == 422
    assert _start(client, headers, str(uuid.uuid4()), years[2021]["id"]).status_code == 404
    assert (
        _start(
            client, tenant_with_admin["reviewer_headers"], years[2021]["id"], years[2023]["id"]
        ).status_code
        == 403
    )
    assert (
        _start(client, headers, years[2021]["id"], years[2023]["id"], threshold=2).status_code
        == 422
    )

    run_id = _start(client, headers, years[2021]["id"], years[2023]["id"]).json()["id"]

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
    assert client.get("/api/runs", headers=other_headers).json() == []
    assert client.get(f"/api/runs/{run_id}", headers=other_headers).status_code == 404
    assert client.post(f"/api/runs/{run_id}/cancel", headers=other_headers).status_code == 404
    # No layer / no ready years for the other tenant -> 409 when starting.
    response = _start(client, other_headers, years[2021]["id"], years[2023]["id"])
    assert response.status_code in (404, 409)
    # Reviewers can read runs.
    assert client.get("/api/runs", headers=tenant_with_admin["reviewer_headers"]).status_code == 200


def test_run_can_be_cancelled_between_batches(
    client: TestClient, db: Session, tenant_with_admin, years, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = tenant_with_admin["admin_headers"]
    monkeypatch.setattr(run_module, "BATCH_SIZE", 5)
    run_id = _start(client, headers, years[2021]["id"], years[2023]["id"]).json()["id"]

    original = run_module.flush_batch

    def cancel_after_first_batch(db_: Session, run: Run, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        original(db_, run, *args, **kwargs)
        client.post(f"/api/runs/{run_id}/cancel", headers=headers)

    monkeypatch.setattr(run_module, "flush_batch", cancel_after_first_batch)
    drain_queue(db)

    run = _get(client, headers, run_id)
    assert run["status"] == "cancelled"
    assert 0 < run["parcels_processed"] < 25
    assert client.post(f"/api/runs/{run_id}/cancel", headers=headers).status_code == 409
    # The job itself succeeded (the cancel is the run's state, not a failure).
    job = db.execute(select(Job).where(Job.type == "run.execute")).scalar_one()
    assert job.status == "succeeded"


def test_run_resumes_after_worker_stop_and_fails_cleanly(
    client: TestClient, db: Session, tenant_with_admin, years, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = tenant_with_admin["admin_headers"]
    monkeypatch.setattr(run_module, "BATCH_SIZE", 5)
    run_id = _start(
        client, headers, years[2021]["id"], years[2023]["id"], threshold=FIXTURE_THRESHOLD
    ).json()["id"]

    original = run_module.flush_batch

    def stop_after_first_batch(db_: Session, run: Run, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        original(db_, run, *args, **kwargs)
        monkeypatch.setattr(worker, "_stop", True)

    monkeypatch.setattr(run_module, "flush_batch", stop_after_first_batch)
    while run_once(db) is not None and not worker.stop_requested():
        pass
    monkeypatch.setattr(worker, "_stop", False)
    monkeypatch.setattr(run_module, "flush_batch", original)

    job = db.execute(select(Job).where(Job.type == "run.execute")).scalar_one()
    assert job.status == "queued" and job.attempts == 0
    run = _get(client, headers, run_id)
    assert run["status"] == "running"
    assert run["parcels_processed"] == 5
    assert (
        db.execute(
            select(func.count()).select_from(RunParcel).where(RunParcel.run_id == uuid.UUID(run_id))
        ).scalar_one()
        == 5
    )

    drain_queue(db)
    run = _get(client, headers, run_id)
    assert run["status"] == "succeeded", run.get("error")
    assert run["parcels_processed"] == 25 and run["candidates"] == 3
    assert (
        db.execute(
            select(func.count()).select_from(RunParcel).where(RunParcel.run_id == uuid.UUID(run_id))
        ).scalar_one()
        == 25
    )

    # A detector blowing up on the second batch marks the run failed with the reason.
    run_id = _start(client, headers, years[2023]["id"], years[2025]["id"]).json()["id"]
    calls = {"n": 0}

    def explode(db_: Session, run: Run, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("detector exploded")
        original(db_, run, *args, **kwargs)

    monkeypatch.setattr(run_module, "flush_batch", explode)
    drain_queue(db)
    run = _get(client, headers, run_id)
    assert run["status"] == "failed"
    assert "detector exploded" in run["error"]
    job = (
        db.execute(select(Job).where(Job.type == "run.execute").order_by(Job.created_at.desc()))
        .scalars()
        .first()
    )
    assert job.status == "failed"


def test_run_needs_ready_years(
    client: TestClient, db: Session, settings: Settings, tenant_with_admin, ingested_layer
) -> None:
    headers = tenant_with_admin["admin_headers"]
    y2021 = ingest_naip_year(client, db, headers, 2021)
    response = client.post("/api/imagery/years", json={"year": 2026}, headers=headers)
    queued_id = response.json()["id"]
    response = _start(client, headers, y2021["id"], queued_id)
    assert response.status_code == 422
    assert "not ready" in response.json()["detail"]
