"""imagery years/assets, runs, run_parcels, parcel_layers.footprint

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21

"""

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

IMAGERY_SOURCES = ("naip", "upload")
IMAGERY_YEAR_STATUSES = ("queued", "processing", "ready", "failed")
IMAGERY_ASSET_STATUSES = ("pending", "ready", "failed")
RUN_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
CREATED_AT = sa.text("clock_timestamp()")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _uuid_fk(name: str, target: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), sa.ForeignKey(target), nullable=nullable)


def _polygon(name: str) -> sa.Column:
    return sa.Column(
        name,
        geoalchemy2.Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )


def upgrade() -> None:
    op.add_column(
        "parcel_layers",
        sa.Column(
            "footprint",
            geoalchemy2.Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
            nullable=True,
        ),
    )

    op.create_table(
        "imagery_years",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid_fk("tenant_id", "tenants.id"),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        _uuid_fk("created_by", "users.id"),
        sa.Column("band_count", sa.Integer(), nullable=True),
        sa.Column("resolution_m", sa.Float(), nullable=True),
        sa.Column("coverage_pct", sa.Float(), nullable=True),
        sa.Column("parcels_uncovered", sa.Integer(), nullable=True),
        _polygon("bounds"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=CREATED_AT, nullable=False
        ),
        sa.UniqueConstraint(
            "tenant_id", "year", "source", name="imagery_years_tenant_year_source_key"
        ),
        sa.CheckConstraint(
            f"source IN ({_in_list(IMAGERY_SOURCES)})", name="imagery_years_source_check"
        ),
        sa.CheckConstraint(
            f"status IN ({_in_list(IMAGERY_YEAR_STATUSES)})", name="imagery_years_status_check"
        ),
    )
    op.create_index("ix_imagery_years_tenant_id", "imagery_years", ["tenant_id"])

    op.create_table(
        "imagery_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid_fk("tenant_id", "tenants.id"),
        _uuid_fk("year_id", "imagery_years.id"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=True),
        _polygon("bounds"),
        sa.Column("epsg", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("band_count", sa.Integer(), nullable=True),
        sa.Column("resolution_m", sa.Float(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=CREATED_AT, nullable=False
        ),
        sa.CheckConstraint(
            f"status IN ({_in_list(IMAGERY_ASSET_STATUSES)})", name="imagery_assets_status_check"
        ),
    )
    op.create_index("ix_imagery_assets_year_id", "imagery_assets", ["year_id"])
    op.create_index(
        "imagery_assets_bounds_idx", "imagery_assets", ["bounds"], postgresql_using="gist"
    )

    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid_fk("tenant_id", "tenants.id"),
        _uuid_fk("layer_id", "parcel_layers.id"),
        _uuid_fk("base_year_id", "imagery_years.id"),
        _uuid_fk("target_year_id", "imagery_years.id"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("min_new_area_m2", sa.Float(), nullable=False),
        sa.Column("parcels_total", sa.Integer(), nullable=False),
        sa.Column("parcels_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parcels_skipped", sa.Integer(), nullable=False, server_default="0"),
        _uuid_fk("created_by", "users.id"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=CREATED_AT, nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"status IN ({_in_list(RUN_STATUSES)})", name="runs_status_check"),
    )
    op.create_index("runs_tenant_created_idx", "runs", ["tenant_id", "created_at"])

    op.create_table(
        "run_parcels",
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id"), primary_key=True
        ),
        sa.Column(
            "parcel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("parcels.id"),
            primary_key=True,
        ),
        sa.Column("parcel_ref", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("candidate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("skipped_reason", sa.Text(), nullable=True),
        sa.Column("indicators", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "run_parcels_queue_idx",
        "run_parcels",
        ["run_id", "candidate", sa.text("score DESC")],
    )


def downgrade() -> None:
    op.drop_index("run_parcels_queue_idx", table_name="run_parcels")
    op.drop_table("run_parcels")
    op.drop_index("runs_tenant_created_idx", table_name="runs")
    op.drop_table("runs")
    op.drop_index("imagery_assets_bounds_idx", table_name="imagery_assets")
    op.drop_index("ix_imagery_assets_year_id", table_name="imagery_assets")
    op.drop_table("imagery_assets")
    op.drop_index("ix_imagery_years_tenant_id", table_name="imagery_years")
    op.drop_table("imagery_years")
    op.drop_column("parcel_layers", "footprint")
