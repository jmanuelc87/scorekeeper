"""metric score scoring key

Revision ID: e2b4f6a8c135
Revises: c1a5e7d3f0b6
Create Date: 2026-07-30 00:00:00.000000

Adds the scoring fingerprint that makes scoring resumable at metric granularity
(see scorekeeper.core.metrics.fingerprint). Nullable with no server default and
no backfill on purpose: NULL means "written before this column existed, provenance
unknown", which never matches a computed key, so those rows re-score exactly once.
Inventing a backfill value would claim knowledge the migration does not have.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2b4f6a8c135'
down_revision: Union[str, Sequence[str], None] = 'c1a5e7d3f0b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'metric_scores',
        sa.Column('scoring_key', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('metric_scores') as batch_op:
        batch_op.drop_column('scoring_key')
