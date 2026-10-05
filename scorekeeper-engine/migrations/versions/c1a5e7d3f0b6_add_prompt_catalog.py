"""add prompt catalog

Add ``prompts``, ``prompt_versions`` and ``run_prompt_bindings``: the tables that make the
Spanish judge prompts editable at runtime and versioned, so a rubric can be tuned without a
deploy while every past benchmark keeps a record of the exact text that produced it.

Ownership splits the same way ``metrics`` / ``use_cases`` do, one level down. A metric declares
its prompt *slots* in code — the slug and the placeholders it fills itself — and nothing more.
``prompts`` mirrors those declarations and ``prompt_versions`` holds the text.
``run_prompt_bindings`` records which version a run scored under.

``prompt_versions`` is append-only: ``template`` is never updated, only ``status`` and
``is_active`` transition. Two partial unique indexes enforce "at most one live version" and "at
most one open draft" per prompt, and a check constraint keeps ``is_active`` false unless the row
is published.

**This migration seeds the prompt text, and is the only place it exists.** That is a stronger
claim than the metric names in ``a9c4e7b21d38``, which are "only a head start" re-derived by
``sync_metrics``: nothing re-derives a prompt template, because there is no code copy to derive it
from. The metric classes render whatever ``selection.resolve`` injects, so an operator editing
``prompt_versions`` changes what the judge receives on the next run.

Seeding is a plain insert rather than an upsert because this is the same revision that creates the
tables — they are empty by construction. ``sync_prompts`` afterwards only reconciles the code-owned
metadata (slug, required variables, description) and never writes a version.

The consequence worth stating: the test suite builds its schema with ``Base.metadata.create_all``
and never runs Alembic, so it does not get these rows for free. ``SEEDED_PROMPTS`` is exposed as a
module-level constant precisely so the suite can import it — to seed a test database, and to assert
every code-declared slot has exactly one seed satisfying its contract.

Revision ID: c1a5e7d3f0b6
Revises: a9c4e7b21d38
Create Date: 2026-07-28 00:00:00.000000

"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c1a5e7d3f0b6'
down_revision: Union[str, Sequence[str], None] = 'a9c4e7b21d38'
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


# --- Seed data ----------------------------------------------------------------
# The Spanish prompt text, spelled out literally: a migration must not import
# application code. Unlike the metric names in a9c4e7b21d38 this is not a head
# start — it is the source of truth. The metric classes declare only the slug and
# the variables they fill; selection.resolve injects whichever version is active.

GENERATE_QUESTION = '''\
Genera una única pregunta en español que la siguiente respuesta estaría respondiendo. Devuelve\
 solo la pregunta, sin explicaciones ni comentarios.
respuesta: {response}
'''

VERDICT_PROMPT = '''\
Eres un evaluador de recuperación (retrieval) para un sistema RAG. Debes decidir si un NODO \
recuperado es relevante para poder construir la RESPUESTA ESPERADA a la pregunta del usuario.

Un nodo es RELEVANTE si aporta información que ayuda directamente a llegar a la respuesta \
esperada (un hecho, dato, definición o paso que aparece o se usa en ella). Es NO RELEVANTE si \
trata de otro tema, es genérico o no contribuye a la respuesta esperada, aunque esté \
relacionado por encima.

Juzga únicamente la utilidad del nodo respecto a la respuesta esperada; no evalúes la \
respuesta del asistente ni la redacción del nodo.

RESPUESTA ESPERADA (verdad de referencia):
{expected_output}

NODO RECUPERADO:
{node}

Devuelve tu veredicto: relevant=true si el nodo es relevante, relevant=false si no lo es, \
junto con una justificación breve en español.
'''

GENERATE_TRUTHS = '''\
Extrae las verdades o hechos presentes en el contexto recuperado. Cada verdad debe ser un \
enunciado atómico y verificable tomado únicamente del contexto. Devuelve también un breve \
resumen en español.
Contexto: {context}'''

VERIFY_RAGAS = '''\
¿Puede inferirse la siguiente afirmación a partir del contexto recuperado?
Devuelve:
- entailed: true si la afirmación se deduce del contexto, false si no se deduce o lo \
contradice.
- justification: una justificación breve en español.
Contexto recuperado:
{context}
Afirmación: {claim}'''

VERIFY_DEEPEVAL = '''\
¿La afirmación concuerda con las verdades o no se menciona en ellas?
Responde:
- true si la afirmación concuerda con las verdades o no se menciona (no verificable pero no contradice).
- false solo si las verdades la contradicen directamente.
Justifica brevemente en español.
Verdades:
{truths}
Afirmación: {claim}'''

NLI_PROMPT = '''\
Eres un juez estricto de inferencia de lenguaje natural (NLI). Se te
proporciona una PREMISA y una HIPÓTESIS. Determina la relación lógica de la
HIPÓTESIS con la PREMISA y devuelve exactamente una etiqueta.

ETIQUETAS
- "entailment":    Una persona que leyera únicamente la PREMISA concluiría
                   que la HIPÓTESIS es definitivamente verdadera.
- "contradiction": Una persona que leyera únicamente la PREMISA concluiría
                   que la HIPÓTESIS es definitivamente falsa.
- "neutral":       La HIPÓTESIS podría ser verdadera o falsa — la PREMISA no
                   aporta información suficiente para decidir en ningún
                   sentido.

REGLAS DE JUICIO
1. Juzga ÚNICAMENTE con base en la PREMISA. No uses conocimiento del mundo
   externo, suposiciones ni hechos que no estén enunciados en la PREMISA.
2. Trata la PREMISA como la descripción de una única escena/evento concreto.
   Las dos oraciones pueden describir la misma escena.
3. La falta de un detalle NO es contradicción. Si la HIPÓTESIS añade
   información que la PREMISA ni confirma ni niega, la etiqueta es "neutral".
4. Un conflicto directo en cualquier atributo, acción, cantidad o actor
   enunciado (p. ej. color, ubicación, quién hace qué) es "contradiction".
5. No te dejes influir por la fluidez ni la verosimilitud — solo por la
   relación lógica.
6. Sé decisivo. Elige la única etiqueta que mejor se ajuste.

SALIDA
Devuelve ÚNICAMENTE un objeto JSON, sin markdown, sin texto adicional:
{{
  "label": "entailment" | "neutral" | "contradiction",
  "reason": "<una oración que cite el detalle específico de la premisa que lo decidió>"
}}

EJEMPLOS

PREMISA: Un hombre de cabello rubio y camisa marrón está bebiendo de una
fuente de agua pública.
HIPÓTESIS: Una persona rubia está bebiendo agua en público.
{{"label": "entailment", "reason": "La premisa indica que un hombre rubio bebe de una fuente \
pública, lo cual la hipótesis reformula de manera más general."}}

PREMISA: Un hombre de cabello rubio y camisa marrón está bebiendo de una
fuente de agua pública.
HIPÓTESIS: El hombre lleva una camisa roja.
{{"label": "contradiction", "reason": "La premisa especifica una camisa marrón, lo cual entra \
en conflicto con la camisa roja de la hipótesis."}}

PREMISA: Un hombre de cabello rubio y camisa marrón está bebiendo de una
fuente de agua pública.
HIPÓTESIS: El hombre tiene sed después de una larga carrera.
{{"label": "neutral", "reason": "La premisa menciona que bebe, pero no dice nada sobre correr \
ni sobre la causa, por lo que no puede confirmarse ni negarse."}}

AHORA JUZGA

PREMISA (documento de contexto recuperado):
{documento}

HIPÓTESIS (respuesta del asistente):
{response}
'''

SEEDED_PROMPTS = (
    {
        'metric': 'answer_relevance',
        'slug': 'generate_question',
        'required_variables': [],
        'description': 'Generación inversa: a partir de la respuesta sola, produce la pregunta que estaría respondiendo. La pregunta original no se expone, para no sesgar la generación.',
        'template': GENERATE_QUESTION,
    },
    {
        'metric': 'contextual_precision',
        'slug': 'verdict',
        'required_variables': ['expected_output', 'node'],
        'description': 'Veredicto binario de relevancia de un nodo recuperado frente a la respuesta esperada, no frente a la respuesta del asistente.',
        'template': VERDICT_PROMPT,
    },
    {
        'metric': 'faithfulness_deepeval',
        'slug': 'generate_truths',
        'required_variables': [],
        'description': 'Extracción de verdades atómicas del contexto recuperado, contra las que se verifica cada afirmación de la respuesta.',
        'template': GENERATE_TRUTHS,
    },
    {
        'metric': 'faithfulness_deepeval',
        'slug': 'verify',
        'required_variables': ['truths', 'claim'],
        'description': 'Veredicto por afirmación: 0 solo si las verdades la contradicen directamente, 1 si concuerda o no se menciona.',
        'template': VERIFY_DEEPEVAL,
    },
    {
        'metric': 'faithfulness_ragas',
        'slug': 'verify',
        'required_variables': ['claim'],
        'description': 'Veredicto de entailment de una afirmación frente al contexto recuperado, con confianza para la cascada Haiku→Opus.',
        'template': VERIFY_RAGAS,
    },
    {
        'metric': 'hallucination',
        'slug': 'nli',
        'required_variables': ['documento', 'response'],
        'description': 'Clasificación NLI de la respuesta (hipótesis) frente a un documento recuperado (premisa): entailment, contradiction o neutral.',
        'template': NLI_PROMPT,
    },
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "prompts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("metric_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("required_variables", _JSON_TYPE, nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # No ON DELETE: NO ACTION is what stops a metric a prompt belongs to from being
        # deleted out from under it.
        sa.ForeignKeyConstraint(["metric_id"], ["metrics.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("metric_id", "slug", name="uq_prompt_metric_slug"),
    )
    op.create_index(op.f("ix_prompts_metric_id"), "prompts", ["metric_id"])

    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("prompt_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_by", sa.String(length=128), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["prompt_id"], ["prompts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["prompt_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prompt_id", "version", name="uq_prompt_version"),
        sa.CheckConstraint(
            "NOT is_active OR status = 'published'",
            name="ck_prompt_version_active_published",
        ),
    )
    op.create_index(op.f("ix_prompt_versions_prompt_id"), "prompt_versions", ["prompt_id"])
    # At most one live version per prompt, and at most one open draft.
    op.create_index(
        "uq_prompt_version_active",
        "prompt_versions",
        ["prompt_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published' AND is_active"),
    )
    op.create_index(
        "uq_prompt_version_draft",
        "prompt_versions",
        ["prompt_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )

    op.create_table(
        "run_prompt_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("prompt_version_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["benchmark_runs.id"], ondelete="CASCADE"),
        # No ON DELETE: a version a run was scored under is not deletable.
        sa.ForeignKeyConstraint(["prompt_version_id"], ["prompt_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "prompt_version_id", name="uq_run_prompt_binding"),
    )
    op.create_index(
        op.f("ix_run_prompt_bindings_run_id"), "run_prompt_bindings", ["run_id"]
    )
    op.create_index(
        op.f("ix_run_prompt_bindings_prompt_version_id"),
        "run_prompt_bindings",
        ["prompt_version_id"],
    )

    _seed_prompts()


def _seed_prompts(bind=None) -> None:
    """Insert one prompt row per slot with its published, active v1.

    A plain insert, not an upsert: the tables were created moments ago in the same
    revision, so they are empty by construction.

    ``metric_id`` is looked up by name from ``metrics``, whose rows ``a9c4e7b21d38``
    seeded; a name missing there means the registry drifted from that revision, so we
    fail loudly rather than silently skipping a prompt the scoring runner will later
    demand.

    **This revision requires online mode** (``alembic upgrade head``, what the compose
    ``migrate`` service runs) — it cannot be rendered with ``--sql``. Two things stop
    it: the ``SELECT`` below has nothing to return offline, and SQLAlchemy has no
    literal renderer for a JSON value, which ``required_variables`` needs.

    ``bind`` defaults to the migration's connection. It is a parameter so the test
    suite — which never runs Alembic — can execute this against its own SQLite
    connection and assert the seed really lands, rather than only checking the literals.
    """
    bind = bind if bind is not None else op.get_bind()
    now = datetime.now(timezone.utc)

    metric_ids = {
        name: key for name, key in bind.execute(sa.select(_metrics.c.name, _metrics.c.id))
    }
    unknown = sorted({seed['metric'] for seed in SEEDED_PROMPTS} - set(metric_ids))
    if unknown:
        raise RuntimeError('No existe fila en "metrics" para: ' + ', '.join(unknown) + '.')

    prompt_rows = []
    version_rows = []
    for seed in SEEDED_PROMPTS:
        # Minted here, not at insert, so the version row can reference its prompt.
        prompt_id = uuid.uuid4()
        prompt_rows.append(
            {
                'id': prompt_id,
                'metric_id': metric_ids[seed['metric']],
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
                'changelog': 'Texto inicial migrado desde el código.',
                'created_by': 'system',
                'created_at': now,
                'published_by': 'system',
                'published_at': now,
            }
        )

    bind.execute(sa.insert(_prompts), prompt_rows)
    bind.execute(sa.insert(_prompt_versions), version_rows)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_run_prompt_bindings_prompt_version_id"), table_name="run_prompt_bindings"
    )
    op.drop_index(op.f("ix_run_prompt_bindings_run_id"), table_name="run_prompt_bindings")
    op.drop_table("run_prompt_bindings")

    op.drop_index("uq_prompt_version_draft", table_name="prompt_versions")
    op.drop_index("uq_prompt_version_active", table_name="prompt_versions")
    op.drop_index(op.f("ix_prompt_versions_prompt_id"), table_name="prompt_versions")
    op.drop_table("prompt_versions")

    op.drop_index(op.f("ix_prompts_metric_id"), table_name="prompts")
    op.drop_table("prompts")
