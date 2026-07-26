"""scenario model_name

Revision ID: c3e7b1a45d09
Revises: b7c9e1f30a25
Create Date: 2026-07-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3e7b1a45d09'
down_revision: Union[str, Sequence[str], None] = 'b7c9e1f30a25'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable with no server default: every pre-existing scenario was ingested
    # without a model, and unknown is the honest value for those rows.
    op.add_column(
        'scenario_results',
        sa.Column('model_name', sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('scenario_results', 'model_name')
