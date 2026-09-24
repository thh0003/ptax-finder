import json
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from ptax.config import Settings
from ptax.db.models import ImageryYear, ParcelLayer, Tenant, User, UserRole
from ptax.db.session import get_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {"tenants", "users", "parcel_layers", "parcels", "jobs"}
PLAN_B_TABLES = {"imagery_years", "imagery_assets", "runs", "run_parcels"}


def _alembic_config(settings: Settings) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def _seed_parcel_row(conn, parcel_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Minimal tenant/user/layer/parcel chain, for tests that run raw SQL mid-migration."""
    tenant_id, user_id, layer_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn.execute(
        text("INSERT INTO tenants (id, name, state, fips) VALUES (:id, 'M', 'MN', '27997')"),
        {"id": tenant_id},
    )
    conn.execute(
        text(
            "INSERT INTO users (id, tenant_id, cognito_sub, email, role) "
            "VALUES (:id, :tenant, :sub, 'm@t.test', 'admin')"
        ),
        {"id": user_id, "tenant": tenant_id, "sub": str(uuid.uuid4())},
    )
    conn.execute(
        text(
            "INSERT INTO parcel_layers (id, tenant_id, uploaded_by, s3_key, original_filename,"
            " status) VALUES (:id, :tenant, :user, 'k', 'f.zip', 'ready')"
        ),
        {"id": layer_id, "tenant": tenant_id, "user": user_id},
    )
    conn.execute(
        text(
            "INSERT INTO parcels (id, tenant_id, layer_id, parcel_ref, geom, attributes) VALUES"
            " (:id, :tenant, :layer, 'ref-1',"
            "  ST_Multi(ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))', 4326)), '{}'::jsonb)"
        ),
        {"id": parcel_id, "tenant": tenant_id, "layer": layer_id},
    )
    return tenant_id, layer_id, user_id


def _seed_year(conn, tenant_id: uuid.UUID, user_id: uuid.UUID, year: int) -> uuid.UUID:
    year_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO imagery_years (id, tenant_id, year, source, status, created_by) "
            "VALUES (:id, :tenant, :year, 'naip', 'ready', :user)"
        ),
        {"id": year_id, "tenant": tenant_id, "year": year, "user": user_id},
    )
    return year_id


def test_upgrade_and_downgrade_round_trip(settings: Settings) -> None:
    cfg = _alembic_config(settings)
    engine = get_engine(settings.database_url)

    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    assert EXPECTED_TABLES | PLAN_B_TABLES <= set(inspector.get_table_names())
    index_names = {ix["name"] for ix in inspector.get_indexes("parcels")}
    assert "parcels_geom_idx" in index_names
    assert "footprint" in {c["name"] for c in inspector.get_columns("parcel_layers")}
    assert "imagery_assets_bounds_idx" in {
        ix["name"] for ix in inspector.get_indexes("imagery_assets")
    }
    run_parcel_indexes = {ix["name"] for ix in inspector.get_indexes("run_parcels")}
    assert "run_parcels_queue_idx" in run_parcel_indexes
    # 0004. The queue index cannot serve the unfiltered score-ordered parcel list --
    # `candidate` sits between the equality and sort columns -- so the list has its own.
    assert "run_parcels_score_idx" in run_parcel_indexes
    # 0003. Where a run found the change, nullable so older runs keep their scores with
    # no markup rather than one re-derived by a detector that has since changed.
    run_parcel_columns = {c["name"]: c for c in inspector.get_columns("run_parcels")}
    for column in ("new_builtup_geom", "structure_geom"):
        assert column in run_parcel_columns, f"{column} missing from run_parcels"
        assert run_parcel_columns[column]["nullable"] is True
    with engine.connect() as conn:
        enum_values = (
            conn.execute(text("SELECT unnest(enum_range(NULL::user_role))::text")).scalars().all()
        )
    assert set(enum_values) == {"admin", "reviewer"}

    # Plan B's migration alone is reversible on top of Plan A's schema.
    command.downgrade(cfg, "0001")
    inspector = inspect(engine)
    assert not (PLAN_B_TABLES & set(inspector.get_table_names()))
    assert "footprint" not in {c["name"] for c in inspector.get_columns("parcel_layers")}

    command.downgrade(cfg, "base")
    inspector = inspect(engine)
    assert not (EXPECTED_TABLES & set(inspector.get_table_names()))
    with engine.connect() as conn:
        enum_exists = conn.execute(
            text("SELECT 1 FROM pg_type WHERE typname = 'user_role'")
        ).first()
    assert enum_exists is None

    # Leave the database migrated for the rest of the suite.
    command.upgrade(cfg, "head")


def test_parcel_geometry_must_be_multipolygon(db: Session) -> None:
    tenant = Tenant(name="T", state="MN", fips="27999")
    db.add(tenant)
    db.flush()
    user = User(
        tenant_id=tenant.id, cognito_sub=str(uuid.uuid4()), email="t@t.test", role=UserRole.admin
    )
    db.add(user)
    db.flush()
    layer = ParcelLayer(
        tenant_id=tenant.id,
        uploaded_by=user.id,
        s3_key="k",
        original_filename="f.zip",
        status="uploaded",
    )
    db.add(layer)
    db.flush()

    insert = text(
        "INSERT INTO parcels (id, tenant_id, layer_id, parcel_ref, geom, attributes) "
        "VALUES (:id, :tenant_id, :layer_id, :ref, ST_GeomFromText(:wkt, 4326), '{}'::jsonb)"
    )
    base = {"tenant_id": tenant.id, "layer_id": layer.id}

    # PostGIS 3.x promotes a Polygon to MultiPolygon on the typmod cast; the stored value
    # must always be a MultiPolygon.
    db.execute(
        insert,
        {**base, "id": uuid.uuid4(), "ref": "p1", "wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))"},
    )
    stored_type = db.execute(
        text("SELECT ST_GeometryType(geom) FROM parcels WHERE layer_id = :layer_id"),
        {"layer_id": layer.id},
    ).scalar_one()
    assert stored_type == "ST_MultiPolygon"

    # Anything that cannot be a MultiPolygon is rejected by the column type.
    with pytest.raises(DBAPIError):
        db.execute(insert, {**base, "id": uuid.uuid4(), "ref": "p2", "wkt": "LINESTRING(0 0,1 1)"})


def test_imagery_year_unique_and_status_check(db: Session) -> None:
    tenant = Tenant(name="T", state="MN", fips="27998")
    db.add(tenant)
    db.flush()
    user = User(
        tenant_id=tenant.id, cognito_sub=str(uuid.uuid4()), email="i@t.test", role=UserRole.admin
    )
    db.add(user)
    db.flush()

    def add_year(status: str) -> ImageryYear:
        year = ImageryYear(
            tenant_id=tenant.id, year=2021, source="naip", status=status, created_by=user.id
        )
        db.add(year)
        db.flush()
        return year

    add_year("queued")
    with pytest.raises(IntegrityError):
        add_year("queued")  # same (tenant, year, source)
    db.rollback()
    with pytest.raises(IntegrityError):
        add_year("bogus")  # status outside the CHECK constraint
    db.rollback()


def test_0003_leaves_existing_run_parcel_results_untouched(settings: Settings) -> None:
    """Migrating a populated `run_parcels` must not disturb the scores already in it.

    The markup columns are nullable with no default and no backfill precisely so this
    holds: an older run keeps its recorded score and indicators, and simply carries no
    markup. If 0003 ever rewrote rows, a reassessment's stored evidence would change
    underneath it.
    """
    cfg = _alembic_config(settings)
    engine = get_engine(settings.database_url)

    command.downgrade(cfg, "base")
    command.upgrade(cfg, "0002")

    run_id = uuid.uuid4()
    parcel_id = uuid.uuid4()
    indicators = {"structure_m2": 412.5, "new_builtup_m2": 980.0}
    with engine.begin() as conn:
        tenant_id, layer_id, user_id = _seed_parcel_row(conn, parcel_id)
        base_year = _seed_year(conn, tenant_id, user_id, 2021)
        target_year = _seed_year(conn, tenant_id, user_id, 2023)
        conn.execute(
            text(
                "INSERT INTO runs (id, tenant_id, layer_id, base_year_id, target_year_id,"
                " status, threshold, min_new_area_m2, parcels_total, created_by) VALUES"
                " (:id, :tenant, :layer, :base, :target, 'succeeded', 0.3, 37.2, 1, :user)"
            ),
            {
                "id": run_id,
                "tenant": tenant_id,
                "layer": layer_id,
                "base": base_year,
                "target": target_year,
                "user": user_id,
            },
        )
        conn.execute(
            text(
                "INSERT INTO run_parcels (run_id, parcel_id, parcel_ref, score, candidate,"
                " indicators) VALUES (:run, :parcel, 'ref-1', 0.6125, true, :ind)"
            ),
            {"run": run_id, "parcel": parcel_id, "ind": json.dumps(indicators)},
        )

    command.upgrade(cfg, "0003")

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT score, candidate, indicators, new_builtup_geom, structure_geom"
                " FROM run_parcels WHERE run_id = :run"
            ),
            {"run": run_id},
        ).one()
    assert float(row[0]) == pytest.approx(0.6125)
    assert row[1] is True
    assert row[2] == indicators
    # The new columns exist and are empty -- no markup invented for an older run.
    assert row[3] is None and row[4] is None

    command.upgrade(cfg, "head")

    # This test writes through its own engine, so its rows are genuinely committed and
    # outlive the session fixture's rollback. Delete them explicitly. Tearing the schema
    # down instead would be simpler but destroys state the rest of the suite is using --
    # doing so broke test_cli and test_imagery_ingest when they ran after this file.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM run_parcels WHERE run_id = :run"), {"run": run_id})
        conn.execute(text("DELETE FROM runs WHERE id = :run"), {"run": run_id})
        conn.execute(
            text("DELETE FROM imagery_years WHERE tenant_id = :tenant"), {"tenant": tenant_id}
        )
        conn.execute(text("DELETE FROM parcels WHERE tenant_id = :tenant"), {"tenant": tenant_id})
        conn.execute(
            text("DELETE FROM parcel_layers WHERE tenant_id = :tenant"), {"tenant": tenant_id}
        )
        conn.execute(text("DELETE FROM users WHERE tenant_id = :tenant"), {"tenant": tenant_id})
        conn.execute(text("DELETE FROM tenants WHERE id = :tenant"), {"tenant": tenant_id})


def test_0005_reads_existing_runs_as_classical_and_new_runs_must_say(
    settings: Settings,
) -> None:
    """Every run existing before 0005 was scored by the classical detector, so that is what
    it must read back as. After 0005 there is no default: a run states its detector, and a
    segmenter run carries the hash of the exact model it used."""
    cfg = _alembic_config(settings)
    engine = get_engine(settings.database_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "0004")

    run_id = uuid.uuid4()
    insert_run = (
        "INSERT INTO runs (id, tenant_id, layer_id, base_year_id, target_year_id,"
        " status, threshold, min_new_area_m2, parcels_total, created_by{extra}) VALUES"
        " (:id, :tenant, :layer, :base, :target, 'succeeded', 0.3, 37.2, 1, :user{values})"
    )
    with engine.begin() as conn:
        tenant_id, layer_id, user_id = _seed_parcel_row(conn, uuid.uuid4())
        params = {
            "tenant": tenant_id,
            "layer": layer_id,
            "base": _seed_year(conn, tenant_id, user_id, 2021),
            "target": _seed_year(conn, tenant_id, user_id, 2023),
            "user": user_id,
        }
        conn.execute(text(insert_run.format(extra="", values="")), {**params, "id": run_id})

    try:
        command.upgrade(cfg, "0005")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT detector, model_name, model_sha256 FROM runs WHERE id = :id"),
                {"id": run_id},
            ).one()
        assert tuple(row) == ("classical", None, None)

        rejected = [
            ("", ""),  # no detector: there is no default any more
            (", detector", ", 'segmentation'"),  # segmenter run with no model hash
            (", detector, model_sha256", ", 'classical', 'abc'"),  # hash on a classical run
            (", detector", ", 'magic'"),
        ]
        for extra, values in rejected:
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(
                    text(insert_run.format(extra=extra, values=values)),
                    {**params, "id": uuid.uuid4()},
                )
        with engine.begin() as conn:
            conn.execute(
                text(
                    insert_run.format(
                        extra=", detector, model_name, model_sha256",
                        values=", 'segmentation', 'segmenter-v1', 'abc'",
                    )
                ),
                {**params, "id": uuid.uuid4()},
            )

        command.downgrade(cfg, "0004")
        assert "detector" not in {c["name"] for c in inspect(engine).get_columns("runs")}
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM imagery_years WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM parcels WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM parcel_layers WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
