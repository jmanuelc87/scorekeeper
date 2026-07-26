"""merge retrieved-context pipeline and metric-trace branches

Revision ID: c62ade2857e5
Revises: a7f3c1d5e9b2, e5b9c3d10f42
Create Date: 2026-07-23 10:37:42.304652

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c62ade2857e5'
down_revision: Union[str, Sequence[str], None] = ('a7f3c1d5e9b2', 'e5b9c3d10f42')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
