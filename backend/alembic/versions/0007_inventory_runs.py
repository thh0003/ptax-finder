"""runs: a run is a comparison (`change`) or a single-year structure inventory

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-23

An inventory run reads one imagery year, stored as `base_year_id`; its `target_year_id`
is NULL, and the checks tie the two together: exactly the inventory runs have no target
year, and exactly they use the `vision` detector (a vision-language model, whose name is
`model_name`). Every run before this revision was a comparison, so existing rows are
backfilled `change` through a temporary server default, which is then dropped as in 0005.

Downgrade fails while any inventory run exists (its NULL target year cannot be restored
to NOT NULL) rather than deleting runs: remove them first if the downgrade is wanted.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("kind", sa.Text(), nullable=False, server_default="change"))
    op.alter_column("runs", "kind", server_default=None)
    op.create_check_constraint("runs_kind_check", "runs", "kind IN ('change', 'inventory')")
    op.alter_column("runs", "target_year_id", nullable=True)
    op.drop_constraint("runs_detector_check", "runs", type_="check")
    op.create_check_constraint(
        "runs_detector_check", "runs", "detector IN ('classical', 'segmentation', 'vision')"
    )
    op.create_check_constraint(
        "runs_kind_target_check", "runs", "(kind = 'inventory') = (target_year_id IS NULL)"
    )
    op.create_check_constraint(
        "runs_kind_detector_check", "runs", "(kind = 'inventory') = (detector = 'vision')"
    )


def downgrade() -> None:
    op.drop_constraint("runs_kind_detector_check", "runs", type_="check")
    op.drop_constraint("runs_kind_target_check", "runs", type_="check")
    op.drop_constraint("runs_detector_check", "runs", type_="check")
    op.create_check_constraint(
        "runs_detector_check", "runs", "detector IN ('classical', 'segmentation')"
    )
    op.alter_column("runs", "target_year_id", nullable=False)
    op.drop_constraint("runs_kind_check", "runs", type_="check")
    op.drop_column("runs", "kind")
