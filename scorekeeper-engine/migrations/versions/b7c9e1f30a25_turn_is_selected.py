"""turn is_selected

Revision ID: b7c9e1f30a25
Revises: 80700622f3c6
Create Date: 2026-07-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c9e1f30a25'
down_revision: Union[str, Sequence[str], None] = '80700622f3c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'turns',
        sa.Column(
            'is_selected',
            sa.Boolean(),
            nullable=False,
            server_default='0',
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('turns', 'is_selected')
