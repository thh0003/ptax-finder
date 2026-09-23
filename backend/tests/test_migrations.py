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
    assert "run_parcels_queue_idx" in {ix["name"] for ix in inspector.get_indexes("run_parcels")}
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
