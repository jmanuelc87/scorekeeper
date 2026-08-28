"""judge call prompts

Persist one row per LLM call a judge makes while scoring a turn, in a new
``judge_calls`` table hanging off ``metric_scores`` (CASCADE). Until now the text
actually sent to the model was rendered inside the judge and discarded, leaving a
score impossible to audit or reproduce — and a single ``metric_scores`` row can
summarize dozens of calls (faithfulness verifies each claim, hallucination runs
per document), so the prompt has to live at call granularity, not score
granularity. No backfill: existing scores have no recoverable prompt, and their
``judge_calls`` stay empty until the turn is re-scored.

Revision ID: f2b6d4c8a013
Revises: e9c1a7d43b58
Create Date: 2026-08-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2b6d4c8a013'
down_revision: Union[str, Sequence[str], None] = 'e9c1a7d43b58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'judge_calls',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('metric_score_id', sa.Uuid(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('step', sa.String(length=16), nullable=True),
        sa.Column('model', sa.String(length=128), nullable=False),
        sa.Column('system_prompt', sa.Text(), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('input_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('output_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['metric_score_id'], ['metric_scores.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('metric_score_id', 'sequence', name='uq_judge_call_sequence'),
    )
    op.create_index(
        op.f('ix_judge_calls_metric_score_id'), 'judge_calls', ['metric_score_id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_judge_calls_metric_score_id'), table_name='judge_calls')
    op.drop_table('judge_calls')
