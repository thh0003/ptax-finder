"""pipeline: the parcel improvement detection schema, isolated per tenant by row-level security

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-24

The detection pipeline's tables live in their own `pipeline` schema, beside (not in place
of) the app's `runs` and `parcels`, which mean something else. Every table carries
`tenant_id` and one RLS policy: a row is visible and writable only when `tenant_id`
equals the transaction's `app.tenant_id` setting.

The app connects as the tables' owner (a superuser locally), and owners and superusers
bypass RLS. Tenant work therefore runs as `ptax_tenant`, a NOLOGIN role the app's user is
granted and switches to per transaction (`ptax.db.tenancy.tenant_scope`). RLS is not
FORCEd, so the owner itself -- migrations and cross-tenant operator maintenance -- keeps
full access. Roles are cluster-wide, so the role is created only if absent, and a
downgrade drops it only when no other database still grants it anything.

Downgrade drops the schema and everything in it: pipeline data exists nowhere else.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TABLES = (
    "tenant_configs",
    "runs",
    "parcels",
    "detections",
    "detection_matches",
    "parcel_changes",
    "reviews",
    "tile_qc",
)

_TENANT = "tenant_id uuid NOT NULL REFERENCES public.tenants(id)"
_RUN = "run_id uuid NOT NULL REFERENCES pipeline.runs(run_id) ON DELETE CASCADE"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ptax_tenant') THEN
                CREATE ROLE ptax_tenant NOLOGIN;
            END IF;
        END $$;
        """
    )
    op.execute("GRANT ptax_tenant TO CURRENT_USER")
    op.execute("CREATE SCHEMA pipeline")

    op.execute(
        f"""
        CREATE TABLE pipeline.tenant_configs (
            {_TENANT} PRIMARY KEY,
            config jsonb NOT NULL,
            version integer NOT NULL DEFAULT 1,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE pipeline.runs (
            run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            {_TENANT},
            year_a integer NOT NULL,
            year_b integer NOT NULL,
            model_version text,
            status text NOT NULL
                CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
            started_at timestamptz,
            finished_at timestamptz,
            config_snapshot jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (year_b > year_a)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE pipeline.parcels (
            {_RUN},
            {_TENANT},
            pin text NOT NULL,
            geom geometry(MultiPolygon, 4326) NOT NULL,
            attrs jsonb,
            PRIMARY KEY (run_id, pin)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE pipeline.detections (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            {_RUN},
            {_TENANT},
            pin text NOT NULL,
            year integer NOT NULL,
            class text NOT NULL,
            score double precision NOT NULL,
            area_sqft double precision NOT NULL,
            change_type text CHECK (change_type IN
                ('new', 'expanded', 'unchanged', 'removed', 'uncertain')),
            already_assessed boolean NOT NULL DEFAULT false,
            geom geometry(MultiPolygon, 4326) NOT NULL
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE pipeline.detection_matches (
            {_RUN},
            {_TENANT},
            a_id uuid NOT NULL REFERENCES pipeline.detections(id) ON DELETE CASCADE,
            b_id uuid NOT NULL REFERENCES pipeline.detections(id) ON DELETE CASCADE,
            iou double precision NOT NULL,
            area_delta_sqft double precision NOT NULL,
            PRIMARY KEY (run_id, a_id, b_id)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE pipeline.parcel_changes (
            {_RUN},
            {_TENANT},
            pin text NOT NULL,
            status text NOT NULL
                CHECK (status IN ('high_confidence', 'needs_review', 'no_change')),
            new_sqft_est double precision NOT NULL DEFAULT 0,
            classes_added text[] NOT NULL DEFAULT '{{}}',
            change_model_agrees boolean,
            note text,
            geom geometry(MultiPolygon, 4326),
            PRIMARY KEY (run_id, pin)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE pipeline.reviews (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            {_RUN},
            {_TENANT},
            pin text NOT NULL,
            review_status text NOT NULL
                CHECK (review_status IN ('pending', 'accepted', 'rejected')),
            reviewer text,
            comment text,
            synced_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE pipeline.tile_qc (
            {_RUN},
            {_TENANT},
            tile_id text NOT NULL,
            reg_shift_ft double precision NOT NULL,
            flagged boolean NOT NULL,
            PRIMARY KEY (run_id, tile_id)
        )
        """
    )

    for table in ("parcels", "detections", "parcel_changes"):
        op.execute(f"CREATE INDEX {table}_geom_idx ON pipeline.{table} USING gist (geom)")
    op.execute("CREATE INDEX detections_run_pin_idx ON pipeline.detections (run_id, pin)")
    op.execute("CREATE INDEX reviews_run_pin_idx ON pipeline.reviews (run_id, pin)")

    op.execute("GRANT USAGE ON SCHEMA pipeline TO ptax_tenant")
    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON pipeline.{table} TO ptax_tenant")
        op.execute(f"ALTER TABLE pipeline.{table} ENABLE ROW LEVEL SECURITY")
        # NULLIF: an unset or cleared setting matches no row instead of failing the cast.
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON pipeline.{table}
                USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            """
        )


def downgrade() -> None:
    op.execute("DROP SCHEMA pipeline CASCADE")
    op.execute(
        """
        DO $$
        BEGIN
            DROP ROLE IF EXISTS ptax_tenant;
        EXCEPTION WHEN dependent_objects_still_exist THEN
            -- Another database on this cluster still grants it privileges; leave it.
            NULL;
        END $$;
        """
    )
