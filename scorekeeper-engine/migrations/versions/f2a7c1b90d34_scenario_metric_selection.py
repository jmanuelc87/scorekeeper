"""scenario metric selection

Revision ID: f2a7c1b90d34
Revises: 811d10323fdd
Create Date: 2026-07-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f2a7c1b90d34'
down_revision: Union[str, Sequence[str], None] = '811d10323fdd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'scenario_metrics',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('use_case', sa.String(length=128), nullable=False),
        sa.Column('metric_name', sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('use_case', 'metric_name', name='uq_scenario_metric'),
    )
    op.create_index(
        op.f('ix_scenario_metrics_use_case'), 'scenario_metrics', ['use_case'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_scenario_metrics_use_case'), table_name='scenario_metrics')
    op.drop_table('scenario_metrics')
