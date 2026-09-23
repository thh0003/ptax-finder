"""run_parcels: an index the score-ordered parcel list can actually use

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-22

`run_parcels_queue_idx` is `(run_id, candidate, score DESC)`. `candidate` sits *between*
the equality column and the sort column, so a B-tree holds `candidate=false` rows and
`candidate=true` rows as two separately-ordered groups. PostgreSQL has no loose/skip index
scan to merge them, so the default parcel list -- which filters on `run_id` alone and
orders by score -- falls back to sorting the whole run. At a few hundred thousand parcels
that is a full sort on every page of every scroll.

This index matches that query exactly. The existing one stays: it still serves the
candidate-filtered form, where the equality predicate makes its leading columns usable.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

INDEX = "run_parcels_score_idx"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "run_parcels",
        ["run_id", sa.text("score DESC NULLS LAST"), "parcel_ref"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="run_parcels")
