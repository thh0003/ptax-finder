import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ptax.config import Settings
from ptax.db.models import Tenant
from ptax.db.session import get_engine
from ptax.db.tenancy import TenantScopeError, tenant_scope
from ptax.pipeline.models import PIPELINE_TABLES, PipelineRun

TABLES = {
    "tenant_configs",
    "runs",
    "parcels",
    "detections",
    "detection_matches",
    "parcel_changes",
    "reviews",
    "tile_qc",
}


def _tenant(db: Session, fips: str) -> Tenant:
    tenant = Tenant(name=f"County {fips}", state="IL", fips=fips)
    db.add(tenant)
    db.flush()
    return tenant


def _run(tenant: Tenant) -> PipelineRun:
    return PipelineRun(tenant_id=tenant.id, year_a=2015, year_b=2019, status="queued")


def _runs(db: Session) -> set[uuid.UUID]:
    return set(db.execute(select(PipelineRun.tenant_id)).scalars())


@pytest.fixture
def two_tenants(db: Session) -> tuple[Tenant, Tenant]:
    a, b = _tenant(db, "90001"), _tenant(db, "90002")
    with tenant_scope(db, a.id):
        db.add(_run(a))
        db.flush()
    with tenant_scope(db, b.id):
        db.add(_run(b))
        db.flush()
    return a, b


def test_every_pipeline_table_is_under_row_level_security(db: Session) -> None:
    rows = db.execute(
        text(
            "SELECT c.relname, c.relrowsecurity FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'pipeline' AND c.relkind = 'r'"
        )
    ).all()
    assert {name for name, _ in rows} == TABLES == {t.name for t in PIPELINE_TABLES}
    assert all(secured for _, secured in rows)


def test_a_tenant_sees_and_writes_only_its_own_rows(
    db: Session, two_tenants: tuple[Tenant, Tenant]
) -> None:
    a, b = two_tenants
    with tenant_scope(db, a.id):
        assert _runs(db) == {a.id}
        with pytest.raises(DBAPIError, match="row-level security"), db.begin_nested():
            db.add(_run(b))
            db.flush()
    with tenant_scope(db, b.id):
        assert _runs(db) == {b.id}


def test_the_tenant_role_with_no_tenant_set_sees_nothing(
    db: Session, two_tenants: tuple[Tenant, Tenant]
) -> None:
    with db.begin_nested():
        db.execute(text("SET LOCAL ROLE ptax_tenant"))
        for table in TABLES:
            count = db.execute(text(f"SELECT count(*) FROM pipeline.{table}")).scalar_one()
            assert count == 0, table
        db.execute(text("RESET ROLE"))
    # The owner connection, outside any scope, still sees every tenant (migrations need it).
    a, b = two_tenants
    assert {a.id, b.id} <= _runs(db)


def test_leaving_a_scope_restores_full_access_even_without_a_real_commit(
    db: Session, two_tenants: tuple[Tenant, Tenant]
) -> None:
    """The test session's commit is only a savepoint release, which does not undo
    `SET LOCAL`; the scope must reset itself on exit rather than rely on a commit."""
    a, b = two_tenants
    with tenant_scope(db, a.id):
        db.add(_run(a))
        db.flush()
    db.commit()
    assert db.execute(text("SELECT current_user")).scalar_one() != "ptax_tenant"
    assert {a.id, b.id} <= _runs(db)

    with pytest.raises(RuntimeError, match="boom"), tenant_scope(db, a.id):
        raise RuntimeError("boom")
    assert db.execute(text("SELECT current_user")).scalar_one() != "ptax_tenant"
    assert {a.id, b.id} <= _runs(db)


def test_nesting_another_tenants_scope_is_refused(
    db: Session, two_tenants: tuple[Tenant, Tenant]
) -> None:
    a, b = two_tenants
    with tenant_scope(db, a.id):
        with tenant_scope(db, a.id):  # the same tenant nests harmlessly
            assert _runs(db) == {a.id}
        assert _runs(db) == {a.id}, "the inner scope must not end the outer one"
        with pytest.raises(TenantScopeError):
            with tenant_scope(db, b.id):
                pass


def test_a_real_commit_inside_a_scope_keeps_the_scope(settings: Settings) -> None:
    """A commit ends `SET LOCAL`; the scope must re-apply itself to the next transaction."""
    engine = get_engine(settings.database_url)
    fips = f"9{uuid.uuid4().int % 10000:04d}"
    with Session(engine) as setup:
        a, b = _tenant(setup, fips), _tenant(setup, str(int(fips) + 1).zfill(5)[-5:])
        ids = (a.id, b.id)
        setup.commit()
    try:
        with Session(engine) as db:
            with tenant_scope(db, ids[1]):
                db.add(PipelineRun(tenant_id=ids[1], year_a=2015, year_b=2019, status="queued"))
                db.commit()
            with tenant_scope(db, ids[0]):
                db.add(PipelineRun(tenant_id=ids[0], year_a=2015, year_b=2019, status="queued"))
                db.commit()
                visible = set(
                    db.execute(
                        select(PipelineRun.tenant_id).where(PipelineRun.tenant_id.in_(ids))
                    ).scalars()
                )
                assert visible == {ids[0]}
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(
                text("DELETE FROM pipeline.runs WHERE tenant_id = ANY(:ids)"), {"ids": list(ids)}
            )
            cleanup.execute(text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": list(ids)})
            cleanup.commit()
