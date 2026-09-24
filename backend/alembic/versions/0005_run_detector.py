"""runs: which detector scored the run, and for the segmenter the exact model

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-23

Every run before this revision was scored by the classical detector, so existing rows are
backfilled `classical` through a temporary server default. The default is then dropped:
from here on a run states its detector, and the API -- not the database -- decides the
default, so a missing value is an error rather than a silent guess.

A segmenter run records the model's name and weights sha256 at creation; the worker
refuses to score with weights whose hash differs. The check constraint ties the two
together: a hash exactly when the detector is `segmentation`.

Downgrade drops the three columns. It loses which runs used the segmenter, which is
acceptable only because a downgrade also removes the code that could read it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("detector", sa.Text(), nullable=False, server_default="classical"),
    )
    op.alter_column("runs", "detector", server_default=None)
    op.add_column("runs", sa.Column("model_name", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("model_sha256", sa.Text(), nullable=True))
    op.create_check_constraint(
        "runs_detector_check", "runs", "detector IN ('classical', 'segmentation')"
    )
    op.create_check_constraint(
        "runs_model_check",
        "runs",
        "(detector = 'segmentation') = (model_sha256 IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("runs_model_check", "runs", type_="check")
    op.drop_constraint("runs_detector_check", "runs", type_="check")
    op.drop_column("runs", "model_sha256")
    op.drop_column("runs", "model_name")
    op.drop_column("runs", "detector")
