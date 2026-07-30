"""turn attempts

Revision ID: e2d7b4a13c95
Revises: c1a5e7d3f0b6
Create Date: 2026-07-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2d7b4a13c95'
down_revision: Union[str, Sequence[str], None] = 'c1a5e7d3f0b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'turns',
        sa.Column(
            'attempts',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('turns', 'attempts')
