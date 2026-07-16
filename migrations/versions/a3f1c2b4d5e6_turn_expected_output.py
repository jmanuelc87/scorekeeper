"""turn expected output

Revision ID: a3f1c2b4d5e6
Revises: 71dc507e9508
Create Date: 2026-07-15 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f1c2b4d5e6'
down_revision: Union[str, Sequence[str], None] = '71dc507e9508'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('turns', sa.Column('expected_output', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('turns', 'expected_output')
