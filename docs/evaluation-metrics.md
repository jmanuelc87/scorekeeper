# Evaluation metrics

Scorekeeper scores each conversation **turn** on one or more metrics using an
LLM-as-a-judge. This page describes the metric taxonomy — the classes that define
*what* a metric is and *how* it is scored — and how to add your own.

The taxonomy lives in `src/scorekeeper/metrics/` and is **database-free and
LLM-free**: metrics run against a plain projection of a turn and call the judge
through a swappable seam, so the whole package is unit-testable without a
database or a live model. The concrete metrics themselves are project-specific,
so the `catalog/` package ships **empty** — you add metrics there (see
[Adding a metric](#adding-a-metric)). The concrete metrics that have been added
are documented in the [Metrics catalog](metrics-catalog.md).

## The big picture

```
ScenarioResult.use_case ──▶ scenario_metrics table ──▶ metric names
                                    ▲                        │
                        sync_selection() reads          resolve() instantiates
                        @register(scenarios=…)                │
                                                              ▼
        Turn ──▶ TurnView ──▶ Metric.evaluate(turn, judge) ──▶ MetricResult
                                         │                        │
                                       Judge                 persisted as
                                   (LLM seam)                 MetricScore rows
                                                                  │
                                                          rollup: weighted,
                                                          normalized turn_score
```

- A metric declares which scenarios it applies to with a decorator; that mapping
  is materialized into the `scenario_metrics` table.
- The scoring runner reads a scenario's `use_case`, resolves its metric subset,
  and runs each metric's `evaluate()` against the turn.
- Each result becomes a `MetricScore` row; rollup turns them into a per-turn
  score (see [Data model](data-model.md)).

## Core concepts

### `Metric`

`scorekeeper.metrics.base.Metric` is the abstract base. **Metadata is class-level;
behavior is the `evaluate()` method.**

| Attribute | Meaning |
| --- | --- |
| `name` | Stable identifier; stored in `MetricScore.metric_name`. |
| `category` | A `MetricCategory` for grouping/reporting. |
| `scale` | A `Scale` (see below); the raw score's range. |
| `weight` | Relative weight in the rollup (default `1.0`). |
| `rubric_version` | Rubric version string; stored on each score (default `"v1"`). |
| `scenarios` | The `use_case` values this metric applies to (usually set by the decorator). |

```python
class Metric(ABC):
    name: ClassVar[str]
    category: ClassVar[MetricCategory]
    scale: ClassVar[Scale]
    weight: ClassVar[float] = 1.0
    rubric_version: ClassVar[str] = "v1"
    scenarios: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult: ...

    def normalize(self, raw: float) -> float:
        return self.scale.normalize(raw)
```

`evaluate()` receives a **`TurnView`** (`prompt`, `response`, `turn_number`,
`history`) — never the ORM `Turn` — and a **`Judge`**, and returns a
**`MetricResult`**:

```python
class MetricResult(BaseModel):
    metric_name: str
    raw_score: float          # in the metric's own scale
    normalized_score: float   # always [0, 1], used at rollup
    justification: str        # Spanish rationale
    judge_model: str | None = None
    rubric_version: str | None = None
    trace: list[StepTrace] = []   # multi-step steps; NOT persisted
```

Two ready-made shapes cover almost everything.

### `SingleRubricMetric` — one rubric, one judge call

The common case. Declare a Spanish `rubric` and the metadata; the base class does
the rest.

```python
@register(scenarios=["soporte_tecnico", "ventas"])
class Correccion(SingleRubricMetric):
    name = "correccion"
    category = MetricCategory.RAG
    scale = Likert()          # 1-5
    weight = 2.0
    rubric = RUBRICA_CORRECCION   # Spanish prompt template
```

### `MultiStepMetric` — orchestrate several judge calls

When a score needs more than one step (extract → verify → aggregate), subclass
`MultiStepMetric` and implement `evaluate()`. Build a list of `StepTrace` entries
and flatten them into the single Spanish `justification` with
`render_justification()`.

```python
@register(scenarios=["soporte_tecnico"])
class SeguridadFactual(MultiStepMetric):
    name = "seguridad_factual"
    category = MetricCategory.RAG
    scale = Unit()            # 0-1
    weight = 3.0

    def evaluate(self, turn, judge):
        trace = []
        extraction = judge.structured(
            instruction=EXTRAER_AFIRMACIONES, turn=turn, schema=Afirmaciones
        )
        trace.append(StepTrace(label="Extracción de afirmaciones", detail=extraction.summary))

        verdicts = [
            judge.score(rubric=VERIFICAR.format(afirmacion=a), turn=turn,
                        scale=Boolean(), rubric_version=self.rubric_version)
            for a in extraction.afirmaciones
        ]
        for a, v in zip(extraction.afirmaciones, verdicts, strict=True):
            trace.append(StepTrace(label=f"Verificación: {a}", detail=v.justification))

        raw = sum(v.score for v in verdicts) / len(verdicts) if verdicts else 1.0
        return MetricResult(
            metric_name=self.name, raw_score=raw, normalized_score=self.normalize(raw),
            justification=self.render_justification(trace),
            judge_model=verdicts[0].model if verdicts else None,
            rubric_version=self.rubric_version, trace=trace,
        )
```

### Scales and normalization

Metrics score on their own scale; rollup needs a common range. Each `Scale`
(`scorekeeper.metrics.scale`) knows how to map a raw score to `[0, 1]`:

| Scale | Range | `normalize` |
| --- | --- | --- |
| `Likert(lo=1, hi=5)` | `lo`–`hi` | `(raw - lo) / (hi - lo)` |
| `Unit()` | 0–1 | identity |
| `Boolean()` | 0/1 | `1.0 if raw >= 0.5 else 0.0` |

`MetricScore.score` always stores the **raw** score; normalization happens only
at rollup, so the stored value stays interpretable in the rubric's own terms.

### Categories

`MetricCategory` (`scorekeeper.metrics.category`) is a data-only `StrEnum`
used for grouping and dashboards. It currently defines a single value, `RAG`;
add categories here as needed — they carry no behavior.

### The Judge seam

Metrics never import an LLM SDK. They depend on the `Judge` **Protocol**
(`scorekeeper.metrics.judge`):

```python
class Judge(Protocol):
    def score(self, *, rubric, turn, scale, rubric_version=None) -> JudgeVerdict: ...
    def structured(self, *, instruction, turn, schema: type[T]) -> T: ...
```

- `score()` runs a Spanish rubric prompt and returns a `JudgeVerdict`
  (`score`, `justification`, `model`).
- `structured()` runs a non-scoring step (extraction/classification) and returns
  an instance of the given Pydantic `schema`.

Tests inject a stub that satisfies this Protocol (see
[Testing](#testing)). The real implementation is added later (see
[Going live](#going-live)).

## Registry and the `@register` decorator

Concrete metrics register themselves with `@register`
(`scorekeeper.metrics.registry`), co-located with the class. The decorator also
carries the **scenarios** the metric applies to:

```python
@register(scenarios=["soporte_tecnico", "ventas"])   # applies to these use cases
class Correccion(SingleRubricMetric): ...

@register                                             # applies to the "default" set
class Utilidad(SingleRubricMetric): ...
```

`MetricRegistry` provides `get(name)`, `create(name)` (instantiate),
`all()`, `add(cls)`, and `clear()`. Importing `scorekeeper.metrics.catalog`
imports every metric module, which is what populates the registry — so **every
metric module must be imported from `catalog/__init__.py`**.

## Per-scenario selection (in the database)

Different scenarios score on different metric subsets. The mapping is **data,
stored in the `scenario_metrics` table** (see [Data model](data-model.md)), but
the *authoring source of truth is the decorator on each class*:

- `selection.declared_selection()` queries the registered classes at runtime and
  returns the desired `(use_case, metric_name)` pairs (a metric with no declared
  scenarios belongs to the reserved `"default"` set).
- `selection.sync_selection(session)` reconciles the table with that declaration
  — inserting missing rows and removing stale ones. It is **idempotent**; run it
  after changing metric scenarios. The caller commits.
- `selection.metrics_for(session, use_case)` returns the metric names for a
  `use_case`, falling back to `"default"` when a scenario has no rows.
- `selection.resolve(session, use_case)` instantiates those metrics (raising a
  Spanish `KeyError` if a stored name is not in the code registry).

```python
from scorekeeper.database import SessionLocal
from scorekeeper.metrics.selection import sync_selection, resolve

with SessionLocal() as session:
    sync_selection(session)          # materialize decorator scenarios → table
    session.commit()
    metrics = resolve(session, "soporte_tecnico")   # [Metric, Metric, ...]
```

## Rollup

`scorekeeper.metrics.rollup.turn_score(scores)` computes a turn's score as the
**weighted mean of normalized metric scores**. It takes anything with
`metric_name` and `score` (e.g. `MetricScore` rows), looks up each metric's
`scale`/`weight` in the registry, normalizes, and weights:

```
turn_score = Σ (scale.normalize(score) · weight) / Σ weight
```

`average(values)` (aliased as `scenario_average` / `platform_average`) is the
plain mean of the non-null children, used for scenario and platform rollups.

Because scale and weight are code metadata (not stored per score), recomputing an
old run applies the *current* weights/scales — `rubric_version` captures rubric
drift but not weight drift. This is acceptable for a tool whose rollups are
derived and recomputable.

## Adding a metric

1. Create a module `src/scorekeeper/metrics/catalog/<nombre>.py`.
2. Subclass `SingleRubricMetric` (one rubric) or `MultiStepMetric` (several
   steps). Set `name`, `category`, `scale`, `weight`, and — for single-rubric —
   the Spanish `rubric`.
3. Decorate it with `@register(scenarios=[...])` listing the `use_case`s it
   applies to (omit `scenarios` to put it in the `"default"` set).
4. Import the module in `catalog/__init__.py` so it registers on import.
5. Run `sync_selection(session)` once to materialize the new mapping into
   `scenario_metrics`.
6. Add a unit test (see below).

```python
# src/scorekeeper/metrics/catalog/claridad.py
from scorekeeper.metrics.base import SingleRubricMetric
from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.registry import register
from scorekeeper.metrics.scale import Likert

RUBRICA_CLARIDAD = """\
Evalúa la CLARIDAD de la respuesta (1-5): {prompt} {response}
Devuelve la puntuación y una justificación breve en español.
"""

@register(scenarios=["soporte_tecnico"])
class Claridad(SingleRubricMetric):
    name = "claridad"
    category = MetricCategory.RAG
    scale = Likert()
    weight = 1.0
    rubric = RUBRICA_CLARIDAD
```

```python
# src/scorekeeper/metrics/catalog/__init__.py
from scorekeeper.metrics.catalog import claridad  # noqa: F401
```

## Testing

Metric classes are testable with **no database and no LLM**. Inject a stub judge
that satisfies the `Judge` Protocol and returns scripted verdicts; assert on the
`MetricResult`. For multi-step metrics, assert the **call order/count** and that
each step appears in the flattened `justification`. Selection tests run against an
in-memory SQLite session. See `tests/metrics/` for the patterns, including a
`registered_metrics` fixture that isolates the global registry per test.

```python
class StubJudge:
    def __init__(self, verdicts=None, extractions=None):
        self._verdicts = list(verdicts or []); self._ext = list(extractions or [])
        self.calls = []
    def score(self, *, rubric, turn, scale, rubric_version=None):
        self.calls.append("score"); return self._verdicts.pop(0)
    def structured(self, *, instruction, turn, schema):
        self.calls.append("structured"); return self._ext.pop(0)
```

## Going live

No LLM SDK is wired up yet. To score for real:

1. Add a judge implementation under `scorekeeper/metrics/judges/` that satisfies
   the `Judge` Protocol (e.g. `AnthropicJudge`, using the latest Claude models via
   structured/JSON tool output for the score and Spanish justification).
2. Add its SDK (e.g. `anthropic`) to `pyproject.toml`.
3. Build the scoring runner that: reads each `ScenarioResult.use_case`, calls
   `resolve(session, use_case)`, runs `evaluate()` per turn, persists
   `MetricScore` rows, and fills `Turn.turn_score` /
   `ScenarioResult.average_score` / `PlatformExecution.average_score` via
   `rollup`.

Because metrics depend only on the `Judge` Protocol, none of the taxonomy changes
when the SDK lands — only the new judge file and the runner.
