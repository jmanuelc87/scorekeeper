"""turn retrieved context

Revision ID: 71dc507e9508
Revises: f2a7c1b90d34
Create Date: 2026-07-15 15:42:45.777509

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '71dc507e9508'
down_revision: Union[str, Sequence[str], None] = 'f2a7c1b90d34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('turns', sa.Column('retrieved_context', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('turns', 'retrieved_context')
