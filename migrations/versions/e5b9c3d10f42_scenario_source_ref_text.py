"""widen scenario source_ref to text

Revision ID: e5b9c3d10f42
Revises: d1f3a7c2e9b4
Create Date: 2026-07-23 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b9c3d10f42'
down_revision: Union[str, Sequence[str], None] = 'd1f3a7c2e9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Live browser captures store the full chat URL here, which overruns the old
    # VARCHAR(256) — Copilot thread URLs alone carry request ids and origin params.
    op.alter_column(
        'scenario_results',
        'source_ref',
        existing_type=sa.String(length=256),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Rows captured live may exceed 256 chars; truncate so the narrower type fits.
    op.execute(
        "UPDATE scenario_results SET source_ref = LEFT(source_ref, 256) "
        "WHERE source_ref IS NOT NULL AND char_length(source_ref) > 256"
    )
    op.alter_column(
        'scenario_results',
        'source_ref',
        existing_type=sa.Text(),
        type_=sa.String(length=256),
        existing_nullable=True,
    )
