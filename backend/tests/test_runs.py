import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
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
    """Start a classical run (the only detector) unless the test says otherwise."""
    body = {"base_year_id": base, "target_year_id": target, "detector": "classical", **extra}
    return client.post("/api/runs", json=body, headers=headers)


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
    rows = (
        db.execute(select(RunParcel).where(RunParcel.run_id == uuid.UUID(run["id"])))
        .scalars()
        .all()
    )
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


# --- The run records where it found the change -----------------------------------------
#
# A score alone cannot be checked by a reviewer, and a markup recomputed later would be
# today's detector answering for a run scored by older code. The masks are therefore
# stored with the score, in the same committed batch.


def _geom_area_m2(db: Session, run_id: str, ref_suffix: str, column) -> float | None:
    """Area of a stored run-parcel geometry, measured in its local UTM zone."""
    row = db.execute(
        select(column, func.ST_Y(func.ST_Centroid(column)), func.ST_X(func.ST_Centroid(column)))
        .where(RunParcel.run_id == uuid.UUID(run_id))
        .where(RunParcel.parcel_ref.like(f"%{ref_suffix}"))
    ).first()
    if row is None or row[0] is None:
        return None
    # ST_Area over geography gives square metres without picking a zone by hand.
    return db.execute(
        select(func.ST_Area(func.Geography(column)))
        .where(RunParcel.run_id == uuid.UUID(run_id))
        .where(RunParcel.parcel_ref.like(f"%{ref_suffix}"))
    ).scalar_one()


def test_a_scored_parcel_stores_the_area_the_run_detected(
    client: TestClient, db: Session, tenant_with_admin, years
) -> None:
    headers = tenant_with_admin["admin_headers"]
    run = _start(
        client, headers, years[2021]["id"], years[2023]["id"], threshold=FIXTURE_THRESHOLD
    ).json()
    drain_queue(db)
    assert _get(client, headers, run["id"])["status"] == "succeeded"

    rows = {
        r.parcel_ref[-6:]: r
        for r in db.execute(select(RunParcel).where(RunParcel.run_id == uuid.UUID(run["id"])))
        .scalars()
        .all()
    }

    # 000003 is one of the three parcels that gained a planted roof.
    flagged = rows["000003"]
    assert flagged.structure_geom is not None, "a flagged parcel must record what it flagged"
    assert flagged.new_builtup_geom is not None

    # The stored polygon has to account for the score printed beside it.
    stored_m2 = _geom_area_m2(db, run["id"], "000003", RunParcel.structure_geom)
    assert stored_m2 == pytest.approx(flagged.indicators["structure_m2"], rel=0.05)


def test_a_parcel_with_nothing_detected_stores_no_geometry(
    client: TestClient, db: Session, tenant_with_admin, years
) -> None:
    """NULL, not an empty shape: most of a county detects nothing and pays this per row."""
    headers = tenant_with_admin["admin_headers"]
    run = _start(
        client, headers, years[2021]["id"], years[2023]["id"], threshold=FIXTURE_THRESHOLD
    ).json()
    drain_queue(db)

    quiet = (
        db.execute(
            select(RunParcel)
            .where(RunParcel.run_id == uuid.UUID(run["id"]), RunParcel.candidate.is_(False))
            .where(RunParcel.skipped_reason.is_(None))
        )
        .scalars()
        .all()
    )
    assert quiet, "expected at least one scored, unflagged parcel"
    unmarked = [r for r in quiet if r.structure_geom is None]
    assert unmarked, "a parcel that detected nothing must store NULL geometry"


def test_a_skipped_parcel_stores_no_geometry(
    client: TestClient, db: Session, tenant_with_admin, years
) -> None:
    headers = tenant_with_admin["admin_headers"]
    run = _start(
        client, headers, years[2023]["id"], years[2025]["id"], threshold=FIXTURE_THRESHOLD
    ).json()
    drain_queue(db)

    skipped = (
        db.execute(
            select(RunParcel).where(
                RunParcel.run_id == uuid.UUID(run["id"]), RunParcel.skipped_reason.is_not(None)
            )
        )
        .scalars()
        .all()
    )
    assert skipped
    for row in skipped:
        assert row.structure_geom is None and row.new_builtup_geom is None


def test_stored_geometry_stays_small_enough_for_county_scale(
    client: TestClient, db: Session, tenant_with_admin, years
) -> None:
    """Under 2 KB per non-NULL row.

    A polygonised raster carries a vertex per pixel step, so without simplification a
    single parcel's markup can run to thousands of vertices. At a few hundred thousand
    parcels per run that is the difference between megabytes and gigabytes.
    """
    headers = tenant_with_admin["admin_headers"]
    run = _start(
        client, headers, years[2021]["id"], years[2023]["id"], threshold=FIXTURE_THRESHOLD
    ).json()
    drain_queue(db)

    average = db.execute(
        select(
            func.avg(
                func.pg_column_size(RunParcel.new_builtup_geom)
                + func.pg_column_size(RunParcel.structure_geom)
            )
        ).where(
            RunParcel.run_id == uuid.UUID(run["id"]),
            RunParcel.new_builtup_geom.is_not(None),
        )
    ).scalar_one()
    assert average is not None, "expected at least one row with stored geometry"
    assert float(average) < 2048, f"stored geometry averages {average} bytes per row"


def _other_tenant_sub(db: Session) -> str:
    """A second tenant with an admin, for proving cross-tenant reads are refused."""
    from ptax.db.models import Tenant, User, UserRole

    other = Tenant(name="Other County", state="MN", fips="27001")
    db.add(other)
    db.flush()
    sub = str(uuid.uuid4())
    db.add(User(tenant_id=other.id, cognito_sub=sub, email="a@other.test", role=UserRole.admin))
    # flush, never commit: the session fixture rolls back, and a commit here leaks this
    # tenant into every later test (it collided with the seed and NAIP ingest tests).
    db.flush()
    return sub


# --- Reading a run's per-parcel results -------------------------------------------------


def _parcels(client: TestClient, headers: dict, run_id: str, **params) -> dict:
    response = client.get(f"/api/runs/{run_id}/parcels", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def scored(client: TestClient, db: Session, tenant_with_admin, years) -> dict:
    headers = tenant_with_admin["admin_headers"]
    run = _start(
        client, headers, years[2021]["id"], years[2023]["id"], threshold=FIXTURE_THRESHOLD
    ).json()
    drain_queue(db)
    return {"run_id": run["id"], "headers": headers}


def test_the_parcel_list_is_ordered_by_score_with_skipped_rows_last(
    client: TestClient, db: Session, tenant_with_admin, years
) -> None:
    headers = tenant_with_admin["admin_headers"]
    # 2023 -> 2025 leaves ten parcels unscored, so this run has both kinds of row.
    run = _start(
        client, headers, years[2023]["id"], years[2025]["id"], threshold=FIXTURE_THRESHOLD
    ).json()
    drain_queue(db)

    items = _parcels(client, headers, run["id"], limit=100)["items"]
    assert len(items) == 25
    scores = [i["score"] for i in items]
    scored_part = [s for s in scores if s is not None]
    assert scored_part == sorted(scored_part, reverse=True)
    # Unscored rows sort last, not first: a NULL is not the best result in the run.
    assert all(s is None for s in scores[len(scored_part) :])


def test_paging_returns_every_parcel_exactly_once(client: TestClient, db: Session, scored) -> None:
    seen: list[str] = []
    offset = 0
    while True:
        page = _parcels(client, scored["headers"], scored["run_id"], limit=7, offset=offset)
        seen.extend(i["parcel_id"] for i in page["items"])
        if not page["items"] or len(seen) >= page["total"]:
            break
        offset += 7

    assert len(seen) == 25
    assert len(set(seen)) == 25, "a parcel appeared on two pages"


def test_the_detail_carries_the_stored_result_and_whether_markup_exists(
    client: TestClient, db: Session, scored
) -> None:
    headers, run_id = scored["headers"], scored["run_id"]
    flagged = (
        db.execute(
            select(RunParcel).where(
                RunParcel.run_id == uuid.UUID(run_id), RunParcel.structure_geom.is_not(None)
            )
        )
        .scalars()
        .first()
    )
    assert flagged is not None

    body = client.get(f"/api/runs/{run_id}/parcels/{flagged.parcel_id}", headers=headers).json()

    assert body["score"] == pytest.approx(flagged.score)
    assert body["candidate"] is flagged.candidate
    assert body["skipped_reason"] is None
    assert body["parcel_ref"] == flagged.parcel_ref
    assert body["indicators"]["structure_m2"] == flagged.indicators["structure_m2"]
    assert body["has_markup"] is True


def test_a_parcel_with_no_recorded_markup_says_so(client: TestClient, db: Session, scored) -> None:
    """The viewer needs this to render two panes rather than a silently-identical third."""
    headers, run_id = scored["headers"], scored["run_id"]
    plain = (
        db.execute(
            select(RunParcel).where(
                RunParcel.run_id == uuid.UUID(run_id),
                RunParcel.structure_geom.is_(None),
                RunParcel.new_builtup_geom.is_(None),
            )
        )
        .scalars()
        .first()
    )
    assert plain is not None

    body = client.get(f"/api/runs/{run_id}/parcels/{plain.parcel_id}", headers=headers).json()
    assert body["has_markup"] is False


def test_skipped_parcels_are_listed_and_readable(
    client: TestClient, db: Session, years, tenant_with_admin
) -> None:
    """A reviewer has to be able to open one and see why it was skipped."""
    headers = tenant_with_admin["admin_headers"]
    run = _start(
        client, headers, years[2023]["id"], years[2025]["id"], threshold=FIXTURE_THRESHOLD
    ).json()
    drain_queue(db)

    skipped = (
        db.execute(
            select(RunParcel).where(
                RunParcel.run_id == uuid.UUID(run["id"]), RunParcel.skipped_reason.is_not(None)
            )
        )
        .scalars()
        .first()
    )
    body = client.get(f"/api/runs/{run['id']}/parcels/{skipped.parcel_id}", headers=headers).json()

    assert body["skipped_reason"] == "no_coverage_target"
    assert body["score"] is None and body["has_markup"] is False


def test_the_parcel_routes_are_scoped_to_the_tenant_and_need_a_token(
    client: TestClient, db: Session, scored, make_token
) -> None:
    headers, run_id = scored["headers"], scored["run_id"]
    assert client.get(f"/api/runs/{run_id}/parcels").status_code == 401
    assert client.get(f"/api/runs/{uuid.uuid4()}/parcels", headers=headers).status_code == 404
    assert (
        client.get(f"/api/runs/{run_id}/parcels/{uuid.uuid4()}", headers=headers).status_code == 404
    )

    # A real second tenant against a run that genuinely exists. Random UUIDs alone would
    # pass even if `_get_run`'s tenant check were dropped, leaving only "does it exist".
    parcel_id = (
        db.execute(select(RunParcel.parcel_id).where(RunParcel.run_id == uuid.UUID(run_id)))
        .scalars()
        .first()
    )
    other_headers = {"Authorization": f"Bearer {make_token(_other_tenant_sub(db))}"}
    assert client.get(f"/api/runs/{run_id}/parcels", headers=other_headers).status_code == 404
    assert (
        client.get(f"/api/runs/{run_id}/parcels/{parcel_id}", headers=other_headers).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/runs/{run_id}/parcels/{parcel_id}/overlay.png", headers=other_headers
        ).status_code
        == 404
    )


def test_the_score_ordered_list_uses_an_index_rather_than_sorting_the_run(
    client: TestClient, db: Session, scored
) -> None:
    """At county scale the default list must not sort the whole run on every page.

    `run_parcels_queue_idx` is `(run_id, candidate, score DESC)`; with no predicate on
    `candidate` PostgreSQL cannot merge its two groups into one ordered stream, so before
    `run_parcels_score_idx` this query planned a full sort. Seeded to 50k rows so the
    planner prefers a real scan over the sequential read it would pick on 25 rows.
    """
    run_id = uuid.UUID(scored["run_id"])
    run = db.get(Run, run_id)
    # `run_parcels.parcel_id` is a real foreign key, so the seed needs parcels behind it.
    db.execute(
        text(
            "INSERT INTO parcels (id, tenant_id, layer_id, parcel_ref, geom, attributes) "
            "SELECT gen_random_uuid(), :tenant_id, :layer_id, 'seed-' || g, "
            "  ST_Multi(ST_Buffer(ST_SetSRID(ST_MakePoint(-93.5 + g * 1e-6, 45.1), 4326), 1e-5)), "
            "  '{}'::jsonb "
            "FROM generate_series(1, 50000) g"
        ),
        {"tenant_id": run.tenant_id, "layer_id": run.layer_id},
    )
    db.execute(
        text(
            "INSERT INTO run_parcels (run_id, parcel_id, parcel_ref, score, candidate) "
            "SELECT :run_id, p.id, p.parcel_ref, random(), false FROM parcels p "
            "WHERE p.parcel_ref LIKE 'seed-%'"
        ),
        {"run_id": run_id},
    )
    db.execute(text("ANALYZE run_parcels"))

    query = text(
        "EXPLAIN SELECT * FROM run_parcels WHERE run_id = :run_id "
        "ORDER BY score DESC NULLS LAST, parcel_ref LIMIT 50"
    )

    def plan() -> str:
        return "\n".join(r[0] for r in db.execute(query, {"run_id": run_id}))

    with_index = plan()
    assert "run_parcels_score_idx" in with_index, with_index
    assert "Sort" not in with_index, f"the list still sorts the run:\n{with_index}"

    # Drop it and the sort comes back -- so the index is demonstrably what removed it.
    db.execute(text("DROP INDEX run_parcels_score_idx"))
    without_index = plan()
    assert "Sort" in without_index, f"expected a sort without the index:\n{without_index}"
    db.rollback()


# --- The detector a run uses -------------------------------------------------------------


def test_a_run_defaults_to_the_classical_detector_and_carries_no_model(
    client: TestClient, tenant_with_admin, years
) -> None:
    headers = tenant_with_admin["admin_headers"]
    response = client.post(
        "/api/runs",
        json={"base_year_id": years[2021]["id"], "target_year_id": years[2023]["id"]},
        headers=headers,
    )

    assert response.status_code == 201, response.text
    assert (response.json()["detector"], response.json()["model_name"]) == ("classical", None)
    # The segmentation detector was removed: asking for it is refused like any unknown name.
    for gone in ("segmentation", "magic"):
        refused = _start(client, headers, years[2021]["id"], years[2023]["id"], detector=gone)
        assert refused.status_code == 422, gone


def test_a_run_recorded_with_the_removed_segmenter_fails_before_scoring(
    client: TestClient, db: Session, tenant_with_admin, years
) -> None:
    """A segmenter run still queued from before the removal must not be quietly scored by
    the classical detector while its row says otherwise."""
    headers = tenant_with_admin["admin_headers"]
    created = _start(client, headers, years[2021]["id"], years[2023]["id"]).json()
    run = db.get_one(Run, uuid.UUID(created["id"]))
    run.detector, run.model_name, run.model_sha256 = "segmentation", "segmenter-v1", "0" * 64
    db.flush()

    drain_queue(db)

    run_out = _get(client, headers, created["id"])
    assert run_out["status"] == "failed"
    assert "no longer exists" in run_out["error"]
    assert (
        db.execute(
            select(func.count()).select_from(RunParcel).where(RunParcel.run_id == run.id)
        ).scalar_one()
        == 0
    )


def test_the_operator_command_queues_a_run(
    db: Session,
    settings: Settings,
    tenant_with_admin,
    years,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    from typer.testing import CliRunner

    from ptax import cli

    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_session", lambda: nullcontext(db))
    tenant = tenant_with_admin["tenant"]

    result = CliRunner().invoke(
        cli.app,
        ["start-run", "--tenant-fips", tenant.fips, "--base", "2021", "--target", "2023"],
    )

    assert result.exit_code == 0, result.output
    run = (
        db.execute(select(Run).where(Run.tenant_id == tenant.id).order_by(Run.created_at.desc()))
        .scalars()
        .first()
    )
    assert run is not None and run.status == "queued"
    assert (run.detector, run.model_name) == ("classical", None)
    assert (
        db.execute(
            select(func.count()).select_from(Job).where(Job.payload["run_id"].astext == str(run.id))
        ).scalar_one()
        == 1
    )

    refused = CliRunner().invoke(
        cli.app,
        ["start-run", "--tenant-fips", tenant.fips, "--base", "2023", "--target", "2021"],
    )
    assert refused.exit_code == 1
    assert "later than base" in refused.output


def test_peak_memory_is_reported_in_mebibytes_on_this_platform() -> None:
    """`ru_maxrss` is KiB on Linux and bytes on macOS; a test process is tens to hundreds
    of MiB, never hundreds of thousands."""
    assert 10 < run_module._peak_rss_mb() < 16_384
