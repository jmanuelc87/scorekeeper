"""turn token usage

Add a ``turn_token_usage`` table (1:1 with ``turns``) holding the input/output LLM
token counts consumed while scoring each turn. New table; no backfill.

Revision ID: f4a2c9d1e7b8
Revises: d1f3a7c2e9b4
Create Date: 2026-07-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a2c9d1e7b8'
down_revision: Union[str, Sequence[str], None] = 'd1f3a7c2e9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'turn_token_usage',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('turn_id', sa.Uuid(), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=False),
        sa.Column('output_tokens', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['turn_id'], ['turns.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('turn_id'),
    )
    op.create_index(
        op.f('ix_turn_token_usage_turn_id'), 'turn_token_usage', ['turn_id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_turn_token_usage_turn_id'), table_name='turn_token_usage')
    op.drop_table('turn_token_usage')
