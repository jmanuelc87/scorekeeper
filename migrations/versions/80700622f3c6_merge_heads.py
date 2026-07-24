"""merge heads

Revision ID: 80700622f3c6
Revises: c62ade2857e5, f4a2c9d1e7b8
Create Date: 2026-07-24 07:40:06.030835

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '80700622f3c6'
down_revision: Union[str, Sequence[str], None] = ('c62ade2857e5', 'f4a2c9d1e7b8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
