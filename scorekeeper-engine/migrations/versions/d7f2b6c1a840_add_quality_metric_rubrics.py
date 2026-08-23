"""add quality metric rubrics

Seed the ten single-rubric quality metrics — relevancia, precisión, completitud, claridad,
razonamiento lógico, contextualización, accionabilidad, estructura, profundidad analítica y
coherencia multi-turno — each scored on a 0.0-1.0 rubric by one judge call.

Two kinds of row, for the same reason the earlier revisions split them:

* ``metrics`` — a head start only. ``selection.sync_metrics`` re-derives these names from the
  registry on every startup and links them to the ``default`` use case; they are inserted here
  because ``prompts.metric_id`` is a foreign key and the prompt seed below runs at migrate time,
  before the app has ever started.
* ``prompts`` / ``prompt_versions`` — the source of truth. A ``SingleRubricMetric`` declares only
  the ``rubric`` slot; the Spanish text exists nowhere in the code, so an operator editing
  ``prompt_versions`` changes what the judge receives on the next run.

Inserts are conditional on the name/slug not already being present, unlike ``c1a5e7d3f0b6``:
that revision created the tables it seeded, this one writes into tables that already hold rows,
and a deployment whose app booted between the two revisions will already have the ``metrics``
rows from ``sync_metrics``.

The rubrics are deliberately domain-neutral: the scenario's own context reaches the judge with
the turn (``judges.base`` appends prompt, response, history and retrieved context to every
call), so pinning the sector into the rubric text would only duplicate it.

``downgrade`` removes the prompts and their versions but leaves the ``metrics`` rows: nothing in
the application deletes a metric, and by then ``use_case_metrics`` and historical
``metric_scores`` may reference them.

Revision ID: d7f2b6c1a840
Revises: e2b4f6a8c135
Create Date: 2026-08-18 00:00:00.000000

"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd7f2b6c1a840'
down_revision: Union[str, Sequence[str], None] = 'e2b4f6a8c135'
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
# One rubric per metric, spelled out literally. Every template is instruction + scale;
# it references no placeholder because the judge appends the whole turn after it.

RELEVANCIA = '''\
Evalúa la RELEVANCIA de la respuesta del asistente: el grado en que aborda directamente la \
consulta del usuario.

Escala 0.0-1.0:
- 0.90-1.00: La respuesta aborda completamente la consulta con información altamente pertinente
- 0.70-0.89: La respuesta es mayormente relevante pero contiene información tangencial
- 0.50-0.69: La respuesta contiene elementos relevantes mezclados con información menos pertinente
- 0.30-0.49: La respuesta tiene cierta relevancia pero deja sin responder aspectos clave
- 0.00-0.29: La respuesta es poco relevante o no aborda la consulta

Devuelve la puntuación y una justificación breve en español.
'''

PRECISION = '''\
Evalúa la PRECISIÓN de la respuesta del asistente: la exactitud y corrección de los hechos, \
datos e información proporcionada.

Escala 0.0-1.0:
- 0.90-1.00: Información completamente precisa y verificable, sin errores factuales
- 0.70-0.89: Información mayormente precisa con errores menores o información ligeramente \
desactualizada
- 0.50-0.69: Mezcla de información precisa e imprecisa; algunos errores factuales presentes
- 0.30-0.49: Múltiples errores factuales; información principalmente imprecisa
- 0.00-0.29: Información fundamentalmente incorrecta o no verificable

Devuelve la puntuación y una justificación breve en español.
'''

COMPLETITUD = '''\
Evalúa la COMPLETITUD de la respuesta del asistente: el grado en que cubre todos los aspectos \
relevantes de la pregunta.

Escala 0.0-1.0:
- 0.90-1.00: Respuesta exhaustiva que cubre todos los aspectos principales y secundarios relevantes
- 0.70-0.89: Cubre los aspectos principales; faltan algunos detalles secundarios
- 0.50-0.69: Cubre aproximadamente la mitad de los aspectos relevantes
- 0.30-0.49: Cubre solo algunos aspectos; hay grandes vacíos de información
- 0.00-0.29: Respuesta incompleta que cubre muy poco del tema

Devuelve la puntuación y una justificación breve en español.
'''

CLARIDAD = '''\
Evalúa la CLARIDAD de la respuesta del asistente: la facilidad de comprensión y la calidad de \
la comunicación y del texto.

Escala 0.0-1.0:
- 0.90-1.00: Extremadamente clara, bien estructurada, con lenguaje preciso y fácil de seguir
- 0.70-0.89: Mayormente clara, estructura coherente, con lenguaje generalmente preciso
- 0.50-0.69: Moderadamente clara; algunas partes son confusas o desorganizadas
- 0.30-0.49: Frecuentemente confusa; estructura pobre; lenguaje impreciso
- 0.00-0.29: Muy confusa, desorganizada, casi imposible de entender

Devuelve la puntuación y una justificación breve en español.
'''

RAZONAMIENTO_LOGICO = '''\
Evalúa el RAZONAMIENTO LÓGICO de la respuesta del asistente: la calidad de la lógica, la \
coherencia de los argumentos y la justificación de las conclusiones.

Escala 0.0-1.0:
- 0.90-1.00: Razonamiento sólido, argumentos bien justificados, conclusiones lógicamente válidas
- 0.70-0.89: Razonamiento mayormente sólido con justificaciones adecuadas
- 0.50-0.69: Razonamiento aceptable pero con algunas inconsistencias lógicas
- 0.30-0.49: Razonamiento débil con varias inconsistencias o saltos lógicos
- 0.00-0.29: Razonamiento falso o lógicamente incoherente

Devuelve la puntuación y una justificación breve en español.
'''

CONTEXTUALIZACION = '''\
Evalúa la CONTEXTUALIZACIÓN de la respuesta del asistente: la comprensión y el uso efectivo del \
contexto proporcionado en la consulta y el escenario.

Escala 0.0-1.0:
- 0.90-1.00: Demuestra comprensión profunda del contexto; lo integra de manera experta
- 0.70-0.89: Entiende y usa bien el contexto en la mayoría de la respuesta
- 0.50-0.69: Reconoce el contexto pero no lo integra completamente
- 0.30-0.49: Comprensión limitada del contexto; referencias inadecuadas
- 0.00-0.29: Ignora o malentiende el contexto completamente

Devuelve la puntuación y una justificación breve en español.
'''

ACCIONABILIDAD = '''\
Evalúa la ACCIONABILIDAD de la respuesta del asistente: la utilidad práctica de la información \
y qué tan aplicable y ejecutable resulta.

Escala 0.0-1.0:
- 0.90-1.00: Información altamente práctica y ejecutable; pasos claros para implementación
- 0.70-0.89: Información mayormente práctica con aplicación clara
- 0.50-0.69: Contiene información útil pero con aplicación limitada
- 0.30-0.49: Poca aplicabilidad práctica; información mayormente teórica
- 0.00-0.29: No aplicable; no proporciona dirección práctica

Devuelve la puntuación y una justificación breve en español.
'''

ESTRUCTURA = '''\
Evalúa la ESTRUCTURA de la respuesta del asistente: la organización, el formato y la \
presentación lógica de la información.

Escala 0.0-1.0:
- 0.90-1.00: Estructura excelente con secciones claras, uso de encabezados o listas cuando es \
apropiado
- 0.70-0.89: Estructura clara y lógica, generalmente bien organizada
- 0.50-0.69: Estructura aceptable pero con algunas desorganizaciones
- 0.30-0.49: Estructura pobre; información desorganizada o difícil de seguir
- 0.00-0.29: Estructura muy deficiente; prácticamente sin organización

Devuelve la puntuación y una justificación breve en español.
'''

PROFUNDIDAD_ANALITICA = '''\
Evalúa la PROFUNDIDAD ANALÍTICA de la respuesta del asistente: el nivel de análisis y la \
profundidad en la exploración del tema.

Escala 0.0-1.0:
- 0.90-1.00: Análisis profundo y perspicaz; explora múltiples dimensiones del problema
- 0.70-0.89: Análisis sólido con exploración adecuada de aspectos clave
- 0.50-0.69: Análisis moderado; algunos aspectos explorados superficialmente
- 0.30-0.49: Análisis superficial; mayormente respuestas de nivel básico
- 0.00-0.29: Análisis mínimo o inexistente; respuesta superficial

Devuelve la puntuación y una justificación breve en español.
'''

COHERENCIA_MULTITURNO = '''\
Evalúa la COHERENCIA MULTI-TURNO de la respuesta del asistente: su consistencia y coherencia a \
lo largo de los turnos previos de la conversación, que se incluyen más abajo.

Escala 0.0-1.0:
- 0.90-1.00: Perfectamente coherente a través de todos los turnos; mantiene consistencia temática
- 0.70-0.89: Generalmente coherente; mantiene la mayoría de elementos consistentes
- 0.50-0.69: Coherencia aceptable con algunas inconsistencias menores
- 0.30-0.49: Varias inconsistencias; el hilo se pierde en algunos puntos
- 0.00-0.29: Incoherente; numerosas contradicciones entre turnos

Devuelve la puntuación y una justificación breve en español.
'''

SEEDED_PROMPTS = (
    {
        'metric': 'relevancia',
        'slug': 'rubric',
        'required_variables': [],
        'description': 'Grado en que la respuesta aborda directamente la consulta del usuario.',
        'template': RELEVANCIA,
    },
    {
        'metric': 'precision',
        'slug': 'rubric',
        'required_variables': [],
        'description': 'Exactitud y corrección de los hechos, datos e información proporcionada.',
        'template': PRECISION,
    },
    {
        'metric': 'completitud',
        'slug': 'rubric',
        'required_variables': [],
        'description': 'Grado en que la respuesta cubre todos los aspectos relevantes de la pregunta.',
        'template': COMPLETITUD,
    },
    {
        'metric': 'claridad',
        'slug': 'rubric',
        'required_variables': [],
        'description': 'Facilidad de comprensión y calidad de la comunicación y estructura del texto.',
        'template': CLARIDAD,
    },
    {
        'metric': 'razonamiento_logico',
        'slug': 'rubric',
        'required_variables': [],
        'description': 'Calidad de la lógica, coherencia de argumentos y justificación de conclusiones.',
        'template': RAZONAMIENTO_LOGICO,
    },
    {
        'metric': 'contextualizacion',
        'slug': 'rubric',
        'required_variables': [],
        'description': 'Comprensión y uso efectivo del contexto proporcionado en la consulta y el escenario.',
        'template': CONTEXTUALIZACION,
    },
    {
        'metric': 'accionabilidad',
        'slug': 'rubric',
        'required_variables': [],
        'description': 'Utilidad práctica de la información; qué tan aplicable y ejecutable es la respuesta.',
        'template': ACCIONABILIDAD,
    },
    {
        'metric': 'estructura',
        'slug': 'rubric',
        'required_variables': [],
        'description': 'Organización, formato y presentación lógica de la información.',
        'template': ESTRUCTURA,
    },
    {
        'metric': 'profundidad_analitica',
        'slug': 'rubric',
        'required_variables': [],
        'description': 'Nivel de análisis y profundidad en la exploración del tema.',
        'template': PROFUNDIDAD_ANALITICA,
    },
    {
        'metric': 'coherencia_multiturno',
        'slug': 'rubric',
        'required_variables': [],
        'description': 'Consistencia y coherencia de la respuesta a lo largo de múltiples turnos de conversación.',
        'template': COHERENCIA_MULTITURNO,
    },
)


def upgrade() -> None:
    """Upgrade schema."""
    _seed_prompts()


def _seed_prompts(bind=None) -> None:
    """Insert the missing ``metrics`` rows and one published, active v1 per rubric slot.

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
                'changelog': 'Texto inicial de la rúbrica.',
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
    """Remove the seeded rubrics, leaving the ``metrics`` rows in place."""
    bind = op.get_bind()
    names = sorted({seed['metric'] for seed in SEEDED_PROMPTS})
    prompt_ids = [
        row[0]
        for row in bind.execute(
            sa.select(_prompts.c.id)
            .select_from(_prompts.join(_metrics, _prompts.c.metric_id == _metrics.c.id))
            .where(_metrics.c.name.in_(names), _prompts.c.slug == 'rubric')
        )
    ]
    if not prompt_ids:
        return
    # Versions first: run_prompt_bindings has no ON DELETE, so a version a run was
    # scored under stops the downgrade rather than orphaning that run's record.
    bind.execute(sa.delete(_prompt_versions).where(_prompt_versions.c.prompt_id.in_(prompt_ids)))
    bind.execute(sa.delete(_prompts).where(_prompts.c.id.in_(prompt_ids)))
