"""metric score structured trace

Replace the flattened Spanish ``justification`` text on ``metric_scores`` with a
structured trace held in its own ``metric_traces`` table (1:1 with
``metric_scores``, ``steps`` stored as JSON). Destructive: the old justification
strings are dropped, not backfilled.

Revision ID: d1f3a7c2e9b4
Revises: b4e8d2f1a9c7
Create Date: 2026-07-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd1f3a7c2e9b4'
down_revision: Union[str, Sequence[str], None] = 'b4e8d2f1a9c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_STEPS_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'metric_traces',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('metric_score_id', sa.Uuid(), nullable=False),
        sa.Column('steps', _STEPS_TYPE, nullable=True),
        sa.ForeignKeyConstraint(['metric_score_id'], ['metric_scores.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('metric_score_id'),
    )
    op.create_index(
        op.f('ix_metric_traces_metric_score_id'), 'metric_traces', ['metric_score_id'],
    )
    with op.batch_alter_table('metric_scores') as batch_op:
        batch_op.drop_column('justification')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('metric_scores') as batch_op:
        batch_op.add_column(sa.Column('justification', sa.Text(), nullable=True))
    op.drop_index(op.f('ix_metric_traces_metric_score_id'), table_name='metric_traces')
    op.drop_table('metric_traces')
