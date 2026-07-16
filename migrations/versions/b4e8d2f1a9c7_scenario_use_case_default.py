"""scenario use_case default

Revision ID: b4e8d2f1a9c7
Revises: a3f1c2b4d5e6
Create Date: 2026-07-15 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e8d2f1a9c7'
down_revision: Union[str, Sequence[str], None] = 'a3f1c2b4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        'scenario_results',
        'use_case',
        existing_type=sa.String(length=128),
        server_default='default',
        existing_nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'scenario_results',
        'use_case',
        existing_type=sa.String(length=128),
        server_default=None,
        existing_nullable=False,
    )
