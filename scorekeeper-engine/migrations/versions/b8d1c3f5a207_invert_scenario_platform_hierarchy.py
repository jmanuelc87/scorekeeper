"""invert the scenario/platform hierarchy

Turns the run tree inside out so a scenario becomes a like-for-like comparison
across platforms instead of a single captured conversation.

Before: ``benchmark_runs -> platform_executions -> scenario_results -> turns``.
After:  ``benchmark_runs -> scenario_results -> platform_executions -> turns``.

``ScenarioResult`` keeps only what identifies the *question* — which scenario, under
which use case — and a ``status``. Everything describing one captured conversation moves
down onto ``PlatformExecution``, which now owns the turns: ``model_name``,
``source_ref``, ``screenshot_path``, ``raw_conversation``, ``status`` and
``average_score``. A scenario may hold several executions (one per platform), so the FK
is a plain indexed column, not a unique one.

``scenario_results.average_score`` is dropped outright rather than moved: a mean across
platforms blends the systems being compared into one number. The scores that mean
something are the per-platform ones.

**This migration destroys benchmark data.** Every foreign key in the tree moves, so
there is no row-preserving path that does not invent a parent for existing rows;
``benchmark_runs`` is emptied (cascading through the whole tree) and runs are
re-ingested from their source ``.xlsx`` files or captures. Nothing outside the run
tree is touched — use cases, metrics, the prompt catalog, auth providers and the
document cache all survive.

Like the rest of the chain this targets PostgreSQL; the test suite builds its schema
from the models with ``create_all`` rather than by migrating.

Revision ID: b8d1c3f5a207
Revises: a4c8e0b53f19
Create Date: 2026-08-18 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d1c3f5a207'
down_revision: Union[str, Sequence[str], None] = 'a4c8e0b53f19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Columns describing one captured conversation. They live on whichever table owns the
# turns, so they travel with them in both directions.
_CAPTURE_COLUMNS = (
    ('model_name', sa.String(length=128)),
    ('source_ref', sa.Text()),
    ('screenshot_path', sa.String(length=512)),
    ('status', sa.String(length=32)),
    ('average_score', sa.Float()),
    ('raw_conversation', sa.JSON().with_variant(sa.dialects.postgresql.JSONB, 'postgresql')),
)


def _clear_runs() -> None:
    """Empty the run tree. Every table below it hangs off an ``ON DELETE CASCADE`` FK."""
    op.execute(sa.text('DELETE FROM benchmark_runs'))


def upgrade() -> None:
    """Upgrade schema."""
    _clear_runs()

    # 1. scenario_results: re-parent onto the run and shed the conversation columns.
    op.drop_index('ix_scenario_results_platform_execution_id', 'scenario_results')
    op.drop_column('scenario_results', 'platform_execution_id')
    for name, _type in _CAPTURE_COLUMNS:
        # The scenario keeps only its ``status``, now rolled up across its executions
        # rather than over its own turns.
        if name != 'status':
            op.drop_column('scenario_results', name)
    op.add_column('scenario_results', sa.Column('run_id', sa.Uuid(), nullable=False))
    op.create_index('ix_scenario_results_run_id', 'scenario_results', ['run_id'])
    op.create_foreign_key(
        'fk_scenario_results_run_id',
        'scenario_results',
        'benchmark_runs',
        ['run_id'],
        ['id'],
        ondelete='CASCADE',
    )

    # 2. platform_executions: re-parent onto the scenario and take the columns over.
    op.drop_index('ix_platform_executions_run_id', 'platform_executions')
    op.drop_column('platform_executions', 'run_id')
    op.add_column(
        'platform_executions', sa.Column('scenario_result_id', sa.Uuid(), nullable=False)
    )
    for name, type_ in _CAPTURE_COLUMNS:
        if name == 'average_score':
            continue  # platform_executions already carries one
        nullable = name != 'status'
        op.add_column(
            'platform_executions',
            sa.Column(name, type_, nullable=nullable, server_default=None if nullable else 'pending'),
        )
    op.create_index(
        'ix_platform_executions_scenario_result_id',
        'platform_executions',
        ['scenario_result_id'],
    )
    op.create_foreign_key(
        'fk_platform_executions_scenario_result_id',
        'platform_executions',
        'scenario_results',
        ['scenario_result_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.alter_column('platform_executions', 'status', server_default=None)

    # 3. turns: hang off the execution that produced them.
    op.drop_index('ix_turns_scenario_result_id', 'turns')
    op.drop_column('turns', 'scenario_result_id')
    op.add_column('turns', sa.Column('platform_execution_id', sa.Uuid(), nullable=False))
    op.create_index('ix_turns_platform_execution_id', 'turns', ['platform_execution_id'])
    op.create_foreign_key(
        'fk_turns_platform_execution_id',
        'turns',
        'platform_executions',
        ['platform_execution_id'],
        ['id'],
        ondelete='CASCADE',
    )


def downgrade() -> None:
    """Downgrade schema."""
    _clear_runs()

    op.drop_constraint('fk_turns_platform_execution_id', 'turns', type_='foreignkey')
    op.drop_index('ix_turns_platform_execution_id', 'turns')
    op.drop_column('turns', 'platform_execution_id')
    op.add_column('turns', sa.Column('scenario_result_id', sa.Uuid(), nullable=False))
    op.create_index('ix_turns_scenario_result_id', 'turns', ['scenario_result_id'])
    op.create_foreign_key(
        'fk_turns_scenario_result_id',
        'turns',
        'scenario_results',
        ['scenario_result_id'],
        ['id'],
        ondelete='CASCADE',
    )

    op.drop_constraint(
        'fk_platform_executions_scenario_result_id', 'platform_executions',
        type_='foreignkey',
    )
    op.drop_index('ix_platform_executions_scenario_result_id', 'platform_executions')
    op.drop_column('platform_executions', 'scenario_result_id')
    for name, _type in _CAPTURE_COLUMNS:
        if name != 'average_score':
            op.drop_column('platform_executions', name)
    op.add_column('platform_executions', sa.Column('run_id', sa.Uuid(), nullable=False))
    op.create_index('ix_platform_executions_run_id', 'platform_executions', ['run_id'])
    op.create_foreign_key(
        'fk_platform_executions_run_id',
        'platform_executions',
        'benchmark_runs',
        ['run_id'],
        ['id'],
        ondelete='CASCADE',
    )

    op.drop_constraint('fk_scenario_results_run_id', 'scenario_results', type_='foreignkey')
    op.drop_index('ix_scenario_results_run_id', 'scenario_results')
    op.drop_column('scenario_results', 'run_id')
    for name, type_ in _CAPTURE_COLUMNS:
        # ``status`` never left the scenario; everything else comes back.
        if name != 'status':
            op.add_column('scenario_results', sa.Column(name, type_, nullable=True))
    op.add_column(
        'scenario_results', sa.Column('platform_execution_id', sa.Uuid(), nullable=False)
    )
    op.create_index(
        'ix_scenario_results_platform_execution_id', 'scenario_results',
        ['platform_execution_id'],
    )
    op.create_foreign_key(
        'fk_scenario_results_platform_execution_id',
        'scenario_results',
        'platform_executions',
        ['platform_execution_id'],
        ['id'],
        ondelete='CASCADE',
    )
