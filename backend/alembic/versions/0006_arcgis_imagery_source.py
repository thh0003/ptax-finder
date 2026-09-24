"""imagery_years: allow the `arcgis` source, a county's cached ArcGIS orthophoto

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-23

A county ArcGIS year is ingested by `ptax-admin county-imagery` from the tile cache named
in its county profile; `provider` records the service URL.

Downgrade restores the narrower check. It fails while any `arcgis` year exists rather
than deleting imagery: remove those years first if the downgrade is really wanted.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("imagery_years_source_check", "imagery_years", type_="check")
    op.create_check_constraint(
        "imagery_years_source_check", "imagery_years", "source IN ('naip', 'upload', 'arcgis')"
    )


def downgrade() -> None:
    op.drop_constraint("imagery_years_source_check", "imagery_years", type_="check")
    op.create_check_constraint(
        "imagery_years_source_check", "imagery_years", "source IN ('naip', 'upload')"
    )
