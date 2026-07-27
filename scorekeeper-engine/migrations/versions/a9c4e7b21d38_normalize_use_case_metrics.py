"""normalize use case metrics

Replaces the flat ``scenario_metrics(use_case, metric_name)`` table — which repeated
both strings on every row — with ``use_cases`` + ``metrics`` and the
``use_case_metrics`` join between them, and turns ``scenario_results.use_case`` into a
foreign key.

``default`` is the only use case seeded, and it scores **every** metric: it is the FK
target every existing ``scenario_results`` row is backfilled to, and the use case an
upload that names none lands on. The same-named sets that ``@register(scenarios=…)`` used
to derive are deliberately *not* recreated — metric sets are user data now, composed
through ``POST /use-cases``.

The metric names are spelled out literally: a migration is a snapshot and must not import
application code, which drifts. That is only a head start, not the source of truth —
``selection.sync_metrics`` re-derives the same invariant from the code registry on every
ingest, so a metric added later joins ``default`` without another migration.

No ``batch_alter_table``: the ``migrate`` service targets PostgreSQL only (the test suite
builds its schema with ``Base.metadata.create_all``), and batch mode would force a full
table copy of ``scenario_results`` for no benefit.

Revision ID: a9c4e7b21d38
Revises: c3e7b1a45d09
Create Date: 2026-07-26 00:00:00.000000

"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a9c4e7b21d38'
down_revision: Union[str, Sequence[str], None] = 'c3e7b1a45d09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The one use case this migration creates, and the catalog it scores — the registered
# metrics as of this revision. ``selection.sync_metrics`` keeps both current afterwards.
DEFAULT_USE_CASE = 'default'
SEEDED_METRICS = (
    'answer_relevance',
    'contextual_precision',
    'faithfulness_deepeval',
    'faithfulness_ragas',
    'hallucination',
)

_use_cases = sa.table('use_cases', sa.column('id', sa.Uuid()), sa.column('name', sa.String()))
_metrics = sa.table('metrics', sa.column('id', sa.Uuid()), sa.column('name', sa.String()))
_use_case_metrics = sa.table(
    'use_case_metrics',
    sa.column('id', sa.Uuid()),
    sa.column('use_case_id', sa.Uuid()),
    sa.column('metric_id', sa.Uuid()),
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'use_cases',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_use_case_name'),
    )
    op.create_table(
        'metrics',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_metric_name'),
    )
    op.create_table(
        'use_case_metrics',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('use_case_id', sa.Uuid(), nullable=False),
        sa.Column('metric_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['use_case_id'], ['use_cases.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['metric_id'], ['metrics.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('use_case_id', 'metric_id', name='uq_use_case_metric'),
    )
    op.create_index(
        op.f('ix_use_case_metrics_use_case_id'), 'use_case_metrics', ['use_case_id'], unique=False
    )
    op.create_index(
        op.f('ix_use_case_metrics_metric_id'), 'use_case_metrics', ['metric_id'], unique=False
    )

    bind = op.get_bind()

    # The only seeded use case — "default", scoring every metric — and the FK target the
    # backfill below points every existing scenario at.
    default_id = uuid.uuid4()
    metric_ids = {name: uuid.uuid4() for name in SEEDED_METRICS}
    bind.execute(sa.insert(_use_cases), [{'id': default_id, 'name': DEFAULT_USE_CASE}])
    bind.execute(
        sa.insert(_metrics), [{'id': key, 'name': name} for name, key in metric_ids.items()]
    )
    bind.execute(
        sa.insert(_use_case_metrics),
        [
            {'id': uuid.uuid4(), 'use_case_id': default_id, 'metric_id': metric_ids[name]}
            for name in SEEDED_METRICS
        ],
    )

    # Nullable first so existing rows can be backfilled before the constraint lands.
    op.add_column('scenario_results', sa.Column('use_case_id', sa.Uuid(), nullable=True))
    scenario_results = sa.table('scenario_results', sa.column('use_case_id', sa.Uuid()))
    bind.execute(sa.update(scenario_results).values(use_case_id=default_id))
    op.alter_column('scenario_results', 'use_case_id', existing_type=sa.Uuid(), nullable=False)
    op.create_index(
        op.f('ix_scenario_results_use_case_id'), 'scenario_results', ['use_case_id'], unique=False
    )
    # No ondelete: NO ACTION is what stops a use case a scored run points at from being
    # deleted out from under it.
    op.create_foreign_key(
        'fk_scenario_results_use_case_id', 'scenario_results', 'use_cases', ['use_case_id'], ['id']
    )
    op.drop_column('scenario_results', 'use_case')

    op.drop_index(op.f('ix_scenario_metrics_use_case'), table_name='scenario_metrics')
    op.drop_table('scenario_metrics')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        'scenario_metrics',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('use_case', sa.String(length=128), nullable=False),
        sa.Column('metric_name', sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('use_case', 'metric_name', name='uq_scenario_metric'),
    )
    op.create_index(
        op.f('ix_scenario_metrics_use_case'), 'scenario_metrics', ['use_case'], unique=False
    )

    bind = op.get_bind()
    # Flatten the join back into the repeated-string pairs it replaced.
    pairs = bind.execute(
        sa.select(_use_cases.c.name, _metrics.c.name)
        .select_from(
            _use_case_metrics.join(_use_cases, _use_cases.c.id == _use_case_metrics.c.use_case_id)
            .join(_metrics, _metrics.c.id == _use_case_metrics.c.metric_id)
        )
    ).all()
    if pairs:
        scenario_metrics = sa.table(
            'scenario_metrics',
            sa.column('id', sa.Uuid()),
            sa.column('use_case', sa.String()),
            sa.column('metric_name', sa.String()),
        )
        bind.execute(
            sa.insert(scenario_metrics),
            [
                {'id': uuid.uuid4(), 'use_case': use_case, 'metric_name': metric}
                for use_case, metric in pairs
            ],
        )

    op.add_column(
        'scenario_results',
        sa.Column('use_case', sa.String(length=128), nullable=True, server_default='default'),
    )
    bind.execute(
        sa.text(
            'UPDATE scenario_results SET use_case = use_cases.name '
            'FROM use_cases WHERE use_cases.id = scenario_results.use_case_id'
        )
    )
    op.alter_column(
        'scenario_results', 'use_case', existing_type=sa.String(length=128), nullable=False
    )
    op.drop_constraint('fk_scenario_results_use_case_id', 'scenario_results', type_='foreignkey')
    op.drop_index(op.f('ix_scenario_results_use_case_id'), table_name='scenario_results')
    op.drop_column('scenario_results', 'use_case_id')

    op.drop_index(op.f('ix_use_case_metrics_metric_id'), table_name='use_case_metrics')
    op.drop_index(op.f('ix_use_case_metrics_use_case_id'), table_name='use_case_metrics')
    op.drop_table('use_case_metrics')
    op.drop_table('metrics')
    op.drop_table('use_cases')
