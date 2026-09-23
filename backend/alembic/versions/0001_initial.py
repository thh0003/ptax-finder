"""initial schema: postgis, tenants, users, parcel_layers, parcels, jobs

Revision ID: 0001
Revises:
Create Date: 2026-09-21

"""

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

PARCEL_LAYER_STATUSES = ("uploaded", "inspecting", "awaiting_field", "ingesting", "ready", "failed")
JOB_STATUSES = ("queued", "running", "succeeded", "failed")
# clock_timestamp() (not now()) so rows created in one transaction keep insertion order.
CREATED_AT = sa.text("clock_timestamp()")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    user_role = postgresql.ENUM("admin", "reviewer", name="user_role", create_type=False)
    user_role.create(op.get_bind(), checkfirst=True)

    # tenants first, without its FK to parcel_layers (added below, after parcel_layers exists).
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("state", sa.String(2), nullable=False),
        sa.Column("fips", sa.String(5), nullable=False, unique=True),
        sa.Column("current_parcel_layer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=CREATED_AT, nullable=False
        ),
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column("cognito_sub", sa.Text(), nullable=False, unique=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("role", user_role, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=CREATED_AT, nullable=False
        ),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "parcel_layers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column(
            "uploaded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("parcel_id_field", sa.Text(), nullable=True),
        sa.Column("fields", postgresql.JSONB(), nullable=True),
        sa.Column("source_crs", sa.Text(), nullable=True),
        sa.Column("feature_count", sa.Integer(), nullable=True),
        sa.Column("skipped_count", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=CREATED_AT, nullable=False
        ),
        sa.CheckConstraint(
            f"status IN ({_in_list(PARCEL_LAYER_STATUSES)})", name="parcel_layers_status_check"
        ),
    )
    op.create_index("ix_parcel_layers_tenant_id", "parcel_layers", ["tenant_id"])

    op.create_foreign_key(
        "tenants_current_parcel_layer_fk",
        "tenants",
        "parcel_layers",
        ["current_parcel_layer_id"],
        ["id"],
    )

    op.create_table(
        "parcels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column(
            "layer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("parcel_layers.id"),
            nullable=False,
        ),
        sa.Column("parcel_ref", sa.Text(), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("attributes", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("layer_id", "parcel_ref", name="parcels_layer_ref_key"),
    )
    op.create_index("ix_parcels_tenant_id", "parcels", ["tenant_id"])
    op.create_index("ix_parcels_layer_id", "parcels", ["layer_id"])
    op.create_index("parcels_geom_idx", "parcels", ["geom"], postgresql_using="gist")

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=CREATED_AT, nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"status IN ({_in_list(JOB_STATUSES)})", name="jobs_status_check"),
    )
    op.create_index("jobs_status_created_idx", "jobs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_index("parcels_geom_idx", table_name="parcels")
    op.drop_table("parcels")
    op.drop_constraint("tenants_current_parcel_layer_fk", "tenants", type_="foreignkey")
    op.drop_table("parcel_layers")
    op.drop_table("users")
    op.drop_table("tenants")
    postgresql.ENUM(name="user_role").drop(op.get_bind(), checkfirst=True)
    # The postgis extension is left installed; it is shared infrastructure, not app schema.
