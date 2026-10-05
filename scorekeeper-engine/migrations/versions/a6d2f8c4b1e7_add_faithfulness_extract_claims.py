"""add faithfulness extract_claims prompt

``faithfulness_ragas`` and ``faithfulness_deepeval`` split the answer into claims with an LLM
extraction step again (it was a deterministic sentence split before), so both gain an
``extract_claims`` prompt slot. The Spanish text is seeded here: the metric classes carry none,
and scoring refuses to start for a slot with no active published version.

Inserts are conditional on the slot not already being present, like ``d7f2b6c1a840``: this
writes into tables that already hold rows, and a slot an operator already edited is left alone.

``downgrade`` removes the two prompts and their versions, leaving the ``metrics`` rows.

Revision ID: a6d2f8c4b1e7
Revises: f2b6d4c8a013
Create Date: 2026-10-05 00:00:00.000000

"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a6d2f8c4b1e7'
down_revision: Union[str, Sequence[str], None] = 'f2b6d4c8a013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# JSONB on PostgreSQL, plain JSON elsewhere — the migration-side mirror of models.JsonColumn.
_JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

# Lightweight table shims for the seed DML — the migration is a snapshot and must not
# import the ORM models, which drift.
_metrics = sa.table('metrics', sa.column('id', sa.Uuid()), sa.column('name', sa.String()))
_prompts = sa.table(
    'prompts',
    sa.column('id', sa.Uuid()),
    sa.column('metric_id', sa.Uuid()),
    sa.column('slug', sa.String()),
    sa.column('required_variables', _JSON_TYPE),
    sa.column('description', sa.Text()),
    sa.column('created_at', sa.DateTime(timezone=True)),
)
_prompt_versions = sa.table(
    'prompt_versions',
    sa.column('id', sa.Uuid()),
    sa.column('prompt_id', sa.Uuid()),
    sa.column('version', sa.Integer()),
    sa.column('template', sa.Text()),
    sa.column('status', sa.String()),
    sa.column('is_active', sa.Boolean()),
    sa.column('supersedes_id', sa.Uuid()),
    sa.column('changelog', sa.Text()),
    sa.column('created_by', sa.String()),
    sa.column('created_at', sa.DateTime(timezone=True)),
    sa.column('published_by', sa.String()),
    sa.column('published_at', sa.DateTime(timezone=True)),
)


EXTRACT_CLAIMS = '''\
Crea una o más afirmaciones a partir de cada oración de la respuesta del asistente. Cada \
afirmación debe ser un enunciado atómico, verificable e independiente. Devuelve también un \
breve resumen en español.
Pregunta: {prompt}
Respuesta: {response}'''

_DESCRIPTION = (
    'Extracción de afirmaciones atómicas y verificables a partir de la respuesta del asistente.'
)

SEEDED_PROMPTS = (
    {
        'metric': 'faithfulness_ragas',
        'slug': 'extract_claims',
        'required_variables': [],
        'description': _DESCRIPTION,
        'template': EXTRACT_CLAIMS,
    },
    {
        'metric': 'faithfulness_deepeval',
        'slug': 'extract_claims',
        'required_variables': [],
        'description': _DESCRIPTION,
        'template': EXTRACT_CLAIMS,
    },
)


def upgrade() -> None:
    """Upgrade schema."""
    _seed_prompts()


def _seed_prompts(bind=None) -> None:
    """Insert the missing ``metrics`` rows and one published, active v1 per slot.

    Conditional on what is already stored, so it is safe on a database whose app has
    already run ``sync_metrics``: a metric name that exists is reused, and a prompt slot
    that exists is left alone (its text may have been edited already).

    **Requires online mode** (``alembic upgrade head``, what the compose ``migrate``
    service runs) — it cannot be rendered with ``--sql``: the ``SELECT``s below have
    nothing to return offline, and SQLAlchemy has no literal renderer for the JSON value
    ``required_variables`` needs.

    ``bind`` defaults to the migration's connection, and is a parameter so the test suite —
    which never runs Alembic — can execute the real insert against its own SQLite
    connection (see ``tests/seeded_prompts.py``).
    """
    bind = bind if bind is not None else op.get_bind()
    now = datetime.now(timezone.utc)

    metric_ids = {
        name: key for name, key in bind.execute(sa.select(_metrics.c.name, _metrics.c.id))
    }
    new_metrics = [
        {'id': uuid.uuid4(), 'name': seed['metric']}
        for seed in SEEDED_PROMPTS
        if seed['metric'] not in metric_ids
    ]
    if new_metrics:
        bind.execute(sa.insert(_metrics), new_metrics)
        metric_ids.update({row['name']: row['id'] for row in new_metrics})

    existing_slots = {
        (metric_id, slug)
        for metric_id, slug in bind.execute(
            sa.select(_prompts.c.metric_id, _prompts.c.slug)
        )
    }

    prompt_rows = []
    version_rows = []
    for seed in SEEDED_PROMPTS:
        metric_id = metric_ids[seed['metric']]
        if (metric_id, seed['slug']) in existing_slots:
            continue
        # Minted here, not at insert, so the version row can reference its prompt.
        prompt_id = uuid.uuid4()
        prompt_rows.append(
            {
                'id': prompt_id,
                'metric_id': metric_id,
                'slug': seed['slug'],
                'required_variables': seed['required_variables'],
                'description': seed['description'],
                'created_at': now,
            }
        )
        version_rows.append(
            {
                'id': uuid.uuid4(),
                'prompt_id': prompt_id,
                'version': 1,
                'template': seed['template'],
                'status': 'published',
                'is_active': True,
                'supersedes_id': None,
                'changelog': 'Texto inicial de la extracción de afirmaciones.',
                'created_by': 'system',
                'created_at': now,
                'published_by': 'system',
                'published_at': now,
            }
        )

    if prompt_rows:
        bind.execute(sa.insert(_prompts), prompt_rows)
        bind.execute(sa.insert(_prompt_versions), version_rows)


def downgrade() -> None:
    """Remove the seeded claim-extraction prompts, leaving the ``metrics`` rows in place."""
    bind = op.get_bind()
    names = sorted({seed['metric'] for seed in SEEDED_PROMPTS})
    prompt_ids = [
        row[0]
        for row in bind.execute(
            sa.select(_prompts.c.id)
            .select_from(_prompts.join(_metrics, _prompts.c.metric_id == _metrics.c.id))
            .where(_metrics.c.name.in_(names), _prompts.c.slug == 'extract_claims')
        )
    ]
    if not prompt_ids:
        return
    # Versions first: run_prompt_bindings has no ON DELETE, so a version a run was
    # scored under stops the downgrade rather than orphaning that run's record.
    bind.execute(sa.delete(_prompt_versions).where(_prompt_versions.c.prompt_id.in_(prompt_ids)))
    bind.execute(sa.delete(_prompts).where(_prompts.c.id.in_(prompt_ids)))
