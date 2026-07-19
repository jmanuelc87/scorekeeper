# HTTP APIs

The HTTP API (`scorekeeper.api`, run with `scorekeeper-api`) serves the results
dashboard and triggers evaluations. This page documents every endpoint; add new
ones as their own `##` section below. For the models these endpoints read and
write, see the [Data model](data-model.md); for how metrics are chosen and scored,
see [Evaluation metrics](evaluation-metrics.md). The same results are also exposed
over the Model Context Protocol — see [MCP tools](mcp.md).

| Method & path                | Purpose |
|------------------------------|---------|
| `GET /health`                | Liveness probe. |
| `POST /evaluations`          | Ingest conversation `.xlsx` files and **enqueue** them for scoring (per-file platform, defaulting to the payload platform). |
| `GET /evaluations/{run_id}`  | Poll a run's status and summary. |
| `GET /runs`                  | Retrieve full scored run details, filtered and at a chosen granularity (HTTP twin of the MCP `retrieve` tool). |
| `GET /turns/{turn_id}/traces` | Retrieve the structured metric traces for a single turn. |

## Architecture: API enqueues, worker scores

Scoring calls the LLM judge once per metric per turn and is slow (minutes for a
full run), so it runs **off the request path**. `POST /evaluations` parses the
upload and persists the run tree synchronously, then enqueues a Celery job and
returns `202` immediately. A separate **worker** process
(`celery -A scorekeeper.celery_app:celery_app worker`) consumes the queue and
scores. The broker is the app's own Postgres (kombu's SQLAlchemy transport — no
extra service); there is no Celery result backend, so clients track progress by
polling `GET /evaluations/{run_id}`, which reads `BenchmarkRun.status`.

Run status lifecycle: `en_cola` (queued) → `en_proceso` (a worker is scoring) →
`completado` | `parcial` | `fallido` (terminal rollup; `fallido` also marks a run
whose scoring raised).

## `GET /health`

Liveness probe. Returns `200` with `{"status": "ok"}`. Takes no parameters.

## `POST /evaluations`

Ingest uploaded conversation `.xlsx` files and enqueue them for scoring. Each file
is scored under its own platform (a per-file override, defaulting to the payload
platform). Parsing and persistence happen synchronously (so a malformed sheet is
rejected here); the LLM scoring runs later in the worker. This is the glue between
the parser (`scorekeeper.importer`) and the scoring runner (`scorekeeper.runner`).

- **Content type:** `multipart/form-data`
- **Parts:**
  - `files` — one or more `.xlsx` uploads. **Each file is one scenario** (one
    conversation). The sheet uses the columns the importer understands (`role` and
    `content` required; `turn`, `retrieved_context`, `expected_output` optional,
    English or Spanish headers). See [Evaluation metrics](evaluation-metrics.md) and
    `scorekeeper.importer`.
  - `payload` — a JSON string with the run metadata (below).

### `payload` fields

| Field       | Type                     | Required | Default     | Description |
|-------------|--------------------------|----------|-------------|-------------|
| `platform`  | `string`                 | yes      | —           | The **default** platform for the upload (e.g. `"claude"`, `"copilot"`, `"gemini"`). Applies to every file that does not override it. Must be non-empty. |
| `use_case`  | `string`                 | no       | `"default"` | Default metric-selection use case for every file. Comma-separated tokens are unioned (e.g. `"document_retrieval,web_search"`). |
| `files`     | `object` (filename → overrides) | no | `{}`   | Per-file overrides keyed by the uploaded filename. A file with no entry uses the defaults. |

Per-file override object:

| Field         | Type     | Default              | Description |
|---------------|----------|----------------------|-------------|
| `scenario_id` | `string` | the file's stem      | Identifier stored on the `ScenarioResult`. |
| `use_case`    | `string` | the payload `use_case` | Metric-selection use case for this file. |
| `platform`    | `string` | the payload `platform` | Platform to score this file under, overriding the payload default. |

### Semantics: one platform per file (defaulting to the payload platform)

Each file is scored under its own platform — its per-file `platform` override when
set, otherwise the payload-level `platform`. The endpoint builds **one
`BenchmarkRun`** with **one `PlatformExecution` per distinct platform**, and attaches
each file's `ScenarioResult` (with its `Turn` rows) to that platform's execution. So
a single request can compare platforms: files sharing a platform group under the same
execution.

```
BenchmarkRun
├─ PlatformExecution(platform="claude")   # esc1 has no override → payload default
│  └─ ScenarioResult(esc1.xlsx) → Turn…
└─ PlatformExecution(platform="gemini")   # esc2 overrides platform
   └─ ScenarioResult(esc2.xlsx) → Turn…
```

Each uploaded file is also recorded as a `SourceFile` (filename + SHA-256) for
provenance; the run links `source_file` only when exactly one file is uploaded
(the FK is singular). Every scenario stores the file's parsed rows in
`raw_conversation` and its name in `source_ref`.

### Processing

On the request (`ingest_evaluation`):

1. Seed the `use_case → metric` selection table (`sync_selection`) so metrics
   resolve — without it every turn scores `None`.
2. Parse each file into raw messages, then **project** them into `Turn` rows:
   messages are grouped by turn number, `user` content becomes the `prompt` and
   `model` content the `response` (a missing side becomes `""`).
3. Build and commit the `BenchmarkRun → PlatformExecution → ScenarioResult → Turn`
   tree — one `PlatformExecution` per distinct platform — with status `en_cola`, and
   enqueue a scoring job carrying just the `run_id`.

In the worker (`score_run`, off the request path):

4. Load the queued run, mark it `en_proceso`, and score it with
   `EvalRunner.run_benchmark` (one `MetricScore` per metric per turn), rolling
   scores up to scenario, platform, and run level. The runner commits **per
   scenario**, so partial progress survives an interruption.
5. Roll the run status up to `completado` / `parcial` / `fallido` and commit.

> **Prerequisites.** The database schema must already exist (`alembic upgrade head`)
> and the configured judge must have a valid API key (see `scorekeeper.config`). The
> worker must be running to make progress past `en_cola`.

### Response `202`

`POST` returns as soon as the run is queued:

```json
{"run_id": "b1f2…", "status": "en_cola"}
```

Poll `GET /evaluations/{run_id}` for progress and results (below).

### Errors

| Status | When |
|--------|------|
| `422`  | `payload` is not valid JSON or fails schema validation (e.g. empty `platform`). |
| `400`  | An uploaded file is not `.xlsx`, is empty, has no `role`/`content` columns, or no file/platform was provided. Ingest rolls back — nothing is persisted and no job is enqueued. |

## `GET /evaluations/{run_id}`

Poll a run's current status and summary. Read the `status` to know where the run
is in its lifecycle (`en_cola → en_proceso → completado|parcial|fallido`).

### Response `200`

```json
{
  "run_id": "b1f2…",
  "status": "completado",
  "progress": {"done": 2, "total": 2, "ratio": 1.0},
  "platforms": [
    {"platform": "claude", "average_score": 0.81, "scenarios": 2, "status_breakdown": {"completado": 2}},
    {"platform": "gemini", "average_score": 0.74, "scenarios": 1, "status_breakdown": {"completado": 1}}
  ]
}
```

- `status` — run lifecycle / rollup. `completado` (all scenarios scored), `parcial`
  (some failed), `fallido` (none scored or the job errored); `en_cola` / `en_proceso`
  while still queued or running.
- `progress` — **turn-level** progress: `done` of `total` turns scored, with
  `ratio` = `done / total` (0.0–1.0) for a progress bar. A turn counts as done once
  its `turn_score` is set. The ratio climbs live while `en_cola` / `en_proceso`; on
  any terminal status it is pinned to `1.0` (a turn whose every metric failed keeps
  `turn_score = null`, so a finished run must not read below 100%).
- `platforms` — one entry per distinct platform in the run. `average_score` is the
  mean of that platform's scenario averages (`null` until scoring finishes) and
  `status_breakdown` counts its scenarios by status. A single-platform run has one
  entry.

### Errors

| Status | When |
|--------|------|
| `404`  | No run with that `run_id` exists. |

## `GET /runs`

Retrieve full scored details for the runs matching a set of filters, as a **list**.
This is the HTTP twin of the MCP [`retrieve` tool](mcp.md#retrieve) — same filters,
granularity, and semantics. Unlike `GET /evaluations/{run_id}` (a single run's shallow
poll summary), `/runs` returns the deep, granularity-configurable shape.

### Query parameters

All are optional and combined with AND.

| Param         | Type     | Default             | Description |
|---------------|----------|---------------------|-------------|
| `run_id`      | `string` | —                   | Narrow to a single run. An unknown/invalid id yields `[]` (not an error). |
| `platform`    | `string` | —                   | Exact, **case-sensitive** platform match (e.g. `claude`, `copilot`, `gemini`). |
| `start_date`  | `string` | —                   | ISO-8601 lower bound (`YYYY-MM-DD` or full timestamp) on the scoring window. |
| `end_date`    | `string` | —                   | ISO-8601 upper bound on the scoring window. |
| `granularity` | `string` | `scenario_results`  | One of `platform_executions`, `scenario_results`, `metric_scores`. |

The date range filters the **scoring window** (`PlatformExecution.started_at` /
`finished_at`), which is `null` until a worker scores the run — so a bound excludes
still-queued/in-progress runs. Results are ordered by run creation date. Granularity
controls depth (each level adds to the one above): `platform_executions` → per-platform
rollups; `scenario_results` → adds each scenario; `metric_scores` → adds each turn and
its per-metric scores (`metric_name`, `score`, `judge_model`, `rubric_version`). Each
score's structured `trace` is persisted on the `metric_traces` table but is not surfaced
by either read path (this endpoint or the MCP [`retrieve` tool](mcp.md#retrieve)).

### Response `200`

```json
[
  {
    "run_id": "b1f2…",
    "status": "completado",
    "created_at": "2026-07-10T12:00:00+00:00",
    "progress": {"done": 2, "total": 2, "ratio": 1.0},
    "platforms": [
      {
        "platform": "claude",
        "average_score": 0.81,
        "started_at": "2026-07-10T12:00:00+00:00",
        "finished_at": "2026-07-10T12:05:00+00:00",
        "scenarios": 2,
        "status_breakdown": {"completado": 2},
        "scenario_results": [ … ]
      }
    ]
  }
]
```

### Errors

| Status | When |
|--------|------|
| `400`  | `granularity` is not one of the three accepted values, or `start_date`/`end_date` is not a valid ISO-8601 date. No-match filters are **not** errors — they return `[]`. |

## `GET /turns/{turn_id}/traces`

Retrieve the structured metric traces for a single turn — the full per-metric
reasoning that the run/scenario read paths omit. The `turn_id` is the turn's UUID,
discoverable from `GET /runs?granularity=metric_scores` (each turn carries a
`turn_id`).

| Query param  | Type      | Default | Notes |
|--------------|-----------|---------|-------|
| `provenance` | `boolean` | `true`  | When `true`, each entry also includes `judge_model` and `rubric_version`; `false` returns the minimal shape (`metric_name` + `trace`). |

Returns one entry per metric scored on the turn (a turn with no scores yields `[]`):

```json
[
  {
    "metric_name": "utilidad",
    "judge_model": "claude-opus-4-8",
    "rubric_version": "v1",
    "trace": {
      "steps": [
        {
          "label": "Puntuación",
          "summary": null,
          "entries": [
            {"label": "utilidad", "value": 0.8, "justification": "razón", "metadata": {}}
          ]
        }
      ]
    }
  }
]
```

### Errors

| Status | When |
|--------|------|
| `404`  | The `turn_id` is unknown or not a valid UUID. |

## Examples

Enqueue (defaults, single file) → returns a `run_id`:

```bash
curl -X POST http://localhost:8001/evaluations \
  -F 'files=@esc1.xlsx' \
  -F 'payload={"platform":"claude","use_case":"default"}'
# {"run_id":"b1f2…","status":"en_cola"}
```

Poll until terminal:

```bash
curl http://localhost:8001/evaluations/b1f2…
```

Retrieve full details for all `claude` runs scored in July, down to metric scores:

```bash
curl 'http://localhost:8001/runs?platform=claude&start_date=2026-07-01&end_date=2026-07-31&granularity=metric_scores'
```

Retrieve one turn's metric traces (full, then minimal):

```bash
curl 'http://localhost:8001/turns/7c9e…/traces'
curl 'http://localhost:8001/turns/7c9e…/traces?provenance=false'
```

Multiple files with per-file overrides — `esc1` keeps the payload `claude` default;
`esc2` is scored under `gemini` (producing two platform executions in the one run):

```bash
curl -X POST http://localhost:8001/evaluations \
  -F 'files=@esc1.xlsx' \
  -F 'files=@esc2.xlsx' \
  -F 'payload={
        "platform": "claude",
        "use_case": "default",
        "files": {
          "esc1.xlsx": {"scenario_id": "esc1", "use_case": "document_retrieval,web_search"},
          "esc2.xlsx": {"platform": "gemini"}
        }
      }'
```

## Running the worker

```bash
celery -A scorekeeper.celery_app:celery_app worker --loglevel=info --concurrency=2
```

Under Docker Compose this is the `worker` service; it shares the app image and the
`DATABASE_URL` (which also backs the broker). Set `CELERY_BROKER_URL` to point at a
dedicated broker (e.g. Redis) instead of Postgres.

## Related code

- Endpoint & request/response models — `src/scorekeeper/api.py`
- Ingest / score split + polling — `src/scorekeeper/evaluation.py`
  (`ingest_evaluation`, `score_run`, `get_run_summary`, `run_evaluation`)
- Celery app & task — `src/scorekeeper/celery_app.py`, `src/scorekeeper/tasks.py`
- Parsing — `src/scorekeeper/importer.py`
- Scoring — `src/scorekeeper/runner.py`
- Metric selection — `src/scorekeeper/metrics/selection.py`
