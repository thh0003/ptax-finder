"""run_parcels: where the run found the change

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-22

Both columns are nullable with no default and no backfill, so this applies to a populated
`run_parcels` without rewriting rows. Runs scored before this migration keep their scores
and carry NULL, which the viewer renders as "this run recorded no markup" -- deliberately,
rather than re-deriving a markup with today's detector that would disagree with the score
stored beside it.
"""

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_COLUMNS = ("new_builtup_geom", "structure_geom")


def _geometry() -> geoalchemy2.Geometry:
    # spatial_index=False: these are fetched by (run_id, parcel_id) primary key from the
    # parcel viewer and never searched spatially, so an index would be write cost for
    # nothing on a table that takes hundreds of thousands of inserts per run.
    return geoalchemy2.Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("run_parcels", sa.Column(name, _geometry(), nullable=True))


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("run_parcels", name)
