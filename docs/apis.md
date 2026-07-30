# HTTP APIs

The HTTP API (`scorekeeper.main` / `scorekeeper.api.v1`, run with `scorekeeper-api`) serves the results
dashboard, triggers evaluations, composes the metric set of each use case, exposes the
versioned prompt catalog, and manages the
retrieval credential store. This page
documents every endpoint; add new ones as their own `##` section below. For the models these
endpoints read and write, see the [Data model](data-model.md); for how metrics are chosen and
scored, see [Evaluation metrics](evaluation-metrics.md); for the credential store, see
[Retrieval credentials](retrieval-credentials.md).

| Method & path                | Purpose |
|------------------------------|---------|
| `GET /health`                | Liveness probe. |
| `POST /api/v1/evaluations`          | Ingest conversation `.xlsx` files for scoring (per-file platform, defaulting to the payload platform). Persists at `ingerido`; does **not** start scoring. |
| `POST /api/v1/captures`             | Ingest conversations captured from a chat UI as JSON for scoring (the browser extension's entry point). Persists at `ingerido`; does **not** start scoring. |
| `PATCH /api/v1/evaluations/{run_id}/turns/selection` | Select (or deselect) which of an ingested run's turns are scored — scoring is **opt-in per turn**. Must be called before `/api/v1/evaluations/{run_id}/start`. |
| `POST /api/v1/evaluations/{run_id}/start` | Start scoring an ingested run: flip it from `ingerido` to `en_cola` and **enqueue** the per-turn chain. |
| `GET /api/v1/evaluations/{run_id}`  | Poll a run's status and summary. |
| `GET /api/v1/runs`                  | Retrieve full scored run details, filtered and at a chosen granularity. |
| `GET /api/v1/runs/{run_id}/scenarios` | Retrieve one run's scenario results as a flat list of rollups (optional `platform`/`status` filters). |
| `GET /api/v1/platform-executions`   | Retrieve platform executions as a flat list of rollups, filtered by platform and scoring window. |
| `GET /api/v1/scenarios/{scenario_id}/turns` | Retrieve one scenario's turns — conversation content plus per-metric scores. |
| `GET /api/v1/metrics`               | List the registered metrics a use case can be composed from. |
| `POST /api/v1/use-cases`            | Create a use case: a name plus the set of metrics it is scored with. |
| `GET /api/v1/use-cases`             | List every use case with its metric names. |
| `GET /api/v1/prompts`               | List every prompt slot the metrics render, with the version currently active. |
| `GET /api/v1/prompts/{prompt_id}`   | Fetch one prompt slot with its full version history. |
| `POST /api/v1/prompts/{prompt_id}/versions` | Open a new draft of a slot's text (replaces any open draft). |
| `POST /api/v1/prompts/{prompt_id}/versions/{version_id}/publish` | Validate a draft and make it the live version. |
| `POST /api/v1/prompts/{prompt_id}/versions/{version_id}/activate` | Roll back to an already-published version. |
| `POST /api/v1/prompts/{prompt_id}/versions/{version_id}/discard` | Abandon an open draft. |
| `GET /api/v1/auth-providers`        | List the retrieval credential store's provider rows (optional filters). |
| `POST /api/v1/auth-providers`       | Create a credential provider row (write-only `private_key`, stored encrypted). |
| `GET /api/v1/auth-providers/{id}`   | Fetch one credential provider by UUID. |
| `PATCH /api/v1/auth-providers/{id}` | Partially update a credential provider (rotate/clear its key). |
| `DELETE /api/v1/auth-providers/{id}`| Delete a credential provider row. |
| `GET /api/v1/turns/{turn_id}/traces` | Retrieve the structured metric traces for a single turn. |
| `GET /api/v1/turns/{turn_id}/token-usage` | Retrieve one turn's raw LLM token usage (no aggregation). |

## Architecture: ingestion is decoupled from scoring; the worker retrieves + scores

Retrieval (fetching/extracting each turn's source documents) and scoring (the LLM judge, once
per metric per turn) are both slow, so they run **off the request path**. Ingestion is also
**decoupled from the start of scoring**: `POST /api/v1/evaluations` (and `POST /api/v1/captures`) parses the
upload and persists the run tree synchronously at status `ingerido`, then returns `202` — it
does **not** enqueue anything. Scoring is also **opt-in per turn**: an ingested turn's
`Turn.is_selected` defaults to `false`, and the worker never visits a turn that is not
selected — so a run scored without selecting turns scores nothing. A
client picks the subset with `PATCH /api/v1/evaluations/{run_id}/turns/selection` before starting.
(An unselected turn is still fed to later selected turns' judge **history**, so the
conversation the judge sees stays complete; it just isn't scored itself.) Scoring starts only
when a client calls `POST /api/v1/evaluations/{run_id}/start`, which flips the run to `en_cola` and
enqueues one Celery job. A separate **worker** process
(`celery -A scorekeeper.celery_app:celery_app worker`) consumes the queue.

The run is evaluated as a **chain of one-turn jobs**, not as a single long job.
`run_chain_task` marks the run `en_proceso`, pins its prompt versions, and enqueues its
first turn; each `score_turn_task` then retrieves and scores exactly one turn
(`services.chain.advance_chain`) and enqueues the next, with the pacing delay handed to
Celery as a `countdown` rather than slept inside the job. The last turn of a scenario rolls
that scenario up, the last of a platform rolls the platform up, and the last of the run
rolls the run up. The broker is the app's own Postgres (kombu's SQLAlchemy transport — no
extra service); there is no Celery result backend, so clients track progress by polling
`GET /api/v1/evaluations/{run_id}`, which reads `BenchmarkRun.status`.

Why one turn per job: a killed worker loses one turn's work instead of the whole run. Every
unit is skippable on re-delivery — a turn that already has a `turn_score`, or whose
`retrieved_documents` are already populated, is left alone — so a re-delivered job resumes
forward rather than restarting. `Turn.attempts` bounds how often a turn that keeps killing
its worker is retried (`core.runner.MAX_TURN_ATTEMPTS`); past the cap it is abandoned with
no score and the chain moves on. A duplicate delivery cannot run alongside the live chain:
each job takes a run-scoped Postgres advisory lock first.

Run status lifecycle: `ingerido` (persisted, not started) → `en_cola` (queued, after
`/start`) → `en_proceso` (a worker is retrieving and scoring the run's turns) →
`completado` | `parcial` | `fallido` (terminal rollup; `fallido` also marks a run whose
retrieval or scoring raised). `en_recuperacion` belongs to the in-process
`services.scoring.run_evaluation` path, which retrieves the whole run as its own phase; the
worker retrieves each turn inside `en_proceso`.

## `GET /health`

Liveness probe. Returns `200` with `{"status": "ok"}`. Takes no parameters.

## `POST /api/v1/evaluations`

Ingest uploaded conversation `.xlsx` files and persist them for scoring. Each file
is scored under its own platform (a per-file override, defaulting to the payload
platform). Parsing and persistence happen synchronously (so a malformed sheet is
rejected here). Ingestion is **decoupled** from scoring: this endpoint does not start
anything — the run lands at status `ingerido` and stays there until
`POST /api/v1/evaluations/{run_id}/start` enqueues it. This is the glue between the parser
(`scorekeeper.core.importer`) and the scoring runner (`scorekeeper.core.runner`).

- **Content type:** `multipart/form-data`
- **Parts:**
  - `files` — one or more `.xlsx` uploads. **Each file is one scenario** (one
    conversation). The sheet uses the columns the importer understands (`role` and
    `content` required; `turn`, `retrieved_context`, `expected_output` optional,
    English or Spanish headers). See [Evaluation metrics](evaluation-metrics.md) and
    `scorekeeper.core.importer`.
  - `payload` — a JSON string with the run metadata (below).

### `payload` fields

| Field       | Type                     | Required | Default     | Description |
|-------------|--------------------------|----------|-------------|-------------|
| `platform`  | `string`                 | yes      | —           | The **default** platform for the upload (e.g. `"claude"`, `"copilot"`, `"gemini"`). Applies to every file that does not override it. Must be non-empty. |
| `use_case`  | `string`                 | no       | `"default"` | Default use case for every file — the metric set it is scored with. Must already exist (create it with [`POST /use-cases`](#post-apiv1use-cases)); an unknown name is a `422`. The fallback `"default"` scores **every** registered metric. |
| `files`     | `object` (filename → overrides) | no | `{}`   | Per-file overrides keyed by the uploaded filename. A file with no entry uses the defaults. |

Per-file override object:

| Field         | Type     | Default              | Description |
|---------------|----------|----------------------|-------------|
| `scenario_id` | `string` | the file's stem      | Identifier stored on the `ScenarioResult`. |
| `use_case`    | `string` | the payload `use_case` | Use case for this file. Must already exist. |
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

1. Mirror the code metric registry into the `metrics` table and seed the
   `default` use case (`sync_metrics`), then resolve each upload's `use_case`
   **name** to its `use_cases` row. An unknown name aborts the whole request with
   `422` before anything is persisted — metric sets are data now, so a typo is a
   client error rather than a run that silently scores nothing.
2. Parse each file into raw messages, then **project** them into `Turn` rows:
   messages are grouped by turn number, `user` content becomes the `prompt` and
   `model` content the `response` (a missing side becomes `""`). The raw
   `retrieved_context` cell is stored verbatim on `Turn.retrieved_context_source` — it is
   **not** interpreted here; the retrieval pipeline handles it in the worker.
3. Build and commit the `BenchmarkRun → PlatformExecution → ScenarioResult → Turn`
   tree — one `PlatformExecution` per distinct platform — with status `ingerido`. Every
   `Turn` lands with `is_selected = false`. Nothing is enqueued; the run waits for a client
   to select turns (`PATCH /api/v1/evaluations/{run_id}/turns/selection`) and call
   `POST /api/v1/evaluations/{run_id}/start`.

After `POST /api/v1/evaluations/{run_id}/start` flips the run to `en_cola` and enqueues the
chain (carrying just the `run_id`), the worker processes **only the selected turns**, one
job per turn, off the request path:

4. **Start** (`run_chain_task` → `chain.start_chain`): mark the run `en_proceso` and pin
   its prompt versions — both once, for the whole run, so an edit published mid-run cannot
   split its rollups across two rubrics, and a slot with no active version fails before any
   judge call. Then enqueue the run's first selected turn. A run with no selected turns is
   finalized here and nothing is enqueued.
5. **One turn** (`score_turn_task` → `chain.advance_chain`), repeated down the chain:
   run the retrieval pipeline over that turn's `retrieved_context_source` into
   `retrieved_documents` (best-effort, skipped if already populated), then score it with
   `EvalRunner.run_turn` — one `MetricScore` per metric, reading the context this same job
   just fetched — and write its `turn_score`. The conversation history the judge sees is
   rebuilt from the stored scenario, so unselected turns still feed it even though they are
   never scored (their `turn_score` stays `null`). Then enqueue the next turn, after the
   pacing delay.
6. **Roll-ups**, at the boundaries the chain crosses: the last selected turn of a scenario
   writes its `average_score` / `status`, the last of a platform writes its `average_score`
   and `finished_at`, and the last of the run rolls the status up to `completado` /
   `parcial` / `fallido`. Scenarios with no selected turns are finalized in that last step,
   since the chain never visits them.

> **Prerequisites.** The database schema must already exist (`alembic upgrade head`)
> and the configured judge must have a valid API key (see `scorekeeper.config.settings`). The
> worker must be running to make progress past `en_cola`, a client must select the turns
> to score (`PATCH /api/v1/evaluations/{run_id}/turns/selection`) — nothing is scored otherwise —
> and call `POST /api/v1/evaluations/{run_id}/start` to move a run past `ingerido`.

### Response `202`

`POST` returns as soon as the run is persisted, at status `ingerido` (not started):

```json
{"run_id": "b1f2…", "status": "ingerido"}
```

Call `POST /api/v1/evaluations/{run_id}/start` to begin scoring, then poll
`GET /api/v1/evaluations/{run_id}` for progress and results (both below).

### Errors

| Status | When |
|--------|------|
| `422`  | `payload` is not valid JSON or fails schema validation (e.g. empty `platform`), **or** a `use_case` names a use case that does not exist. |
| `400`  | An uploaded file is not `.xlsx`, is empty, has no `role`/`content` columns, or no file/platform was provided. Ingest rolls back — nothing is persisted. |

## `POST /api/v1/captures`

Ingest conversations **captured from a chat UI** and persist them for scoring. The
JSON twin of `POST /api/v1/evaluations` for clients that already hold the turns and have no
spreadsheet to upload — the [browser extension](../extension/README.md) scrapes them
straight off Copilot, Gemini and Claude. Like `POST /api/v1/evaluations`, ingestion is
decoupled from scoring: the run lands at `ingerido` and starts only via
`POST /api/v1/evaluations/{run_id}/start`.

Both endpoints converge immediately: the messages are normalized by
`scorekeeper.core.importer.normalize_messages` (the same role aliasing and turn numbering
the `.xlsx` parser uses) and persisted by the same `ingest_evaluation`, so a captured
conversation is indistinguishable downstream from an uploaded one.

- **Content type:** `application/json`

| Field           | Type       | Required | Default     | Description |
|-----------------|------------|----------|-------------|-------------|
| `platform`      | `string`   | yes      | —           | The **default** platform for the request. Applies to every conversation that does not override it. |
| `use_case`      | `string`   | no       | `"default"` | Default use case — the metric set the conversations are scored with. Must already exist ([`POST /use-cases`](#post-apiv1use-cases)); an unknown name is a `422`. |
| `conversations` | `array`    | yes      | —           | One or more captured conversations; **each is one scenario**. Must be non-empty. |

Conversation object:

| Field         | Type              | Required | Default            | Description |
|---------------|-------------------|----------|--------------------|-------------|
| `scenario_id` | `string`          | yes      | —                  | Identifier stored on the `ScenarioResult`. |
| `messages`    | `array`           | yes      | —                  | The conversation, in order. Must be non-empty and at least one message must have content. |
| `use_case`    | `string`          | no       | the payload `use_case` | Use case for this conversation. Must already exist. |
| `platform`    | `string`          | no       | the payload `platform` | Platform to score this conversation under. |
| `model_name`  | `string`          | no       | `null`             | The model that produced the responses (`"Claude Opus 4.5"`), stored on the `ScenarioResult`. The browser extension detects it from the chat's model picker and lets the user correct it; omitted, `null` or blank all store `NULL`. |
| `source_ref`  | `string`          | no       | the `scenario_id`  | Where the capture came from (the chat URL); stored as the scenario's `source_ref`. |

Message object:

| Field                | Type     | Required | Description |
|----------------------|----------|----------|-------------|
| `role`               | `string` | yes      | `user` or `model`; the importer's aliases (`usuario`, `assistant`, `modelo`, …) are accepted. |
| `content`            | `string` | no       | The message text. |
| `turn`               | `int`    | no       | Explicit turn number. Omit it on **every** message to have turns derived — each `user` message following a non-user message opens a new turn, so a user+model pair shares one number. |
| `retrieved_context`  | `string` | no       | Context the platform retrieved, when the client knows it. Free-form text, or a JSON array of `{name, url}` source records (what the browser extension sends) — each array element counts as one retrieved document. |
| `expected_output`    | `string` | no       | Reference answer, when the client knows it. |

Since there is no file to hash, the `SourceFile` provenance hash covers the
serialized capture itself.

### Response `202`

Identical to `POST /api/v1/evaluations` — the run is persisted at `ingerido`. Call
`POST /api/v1/evaluations/{run_id}/start` to begin scoring, then poll `GET /api/v1/evaluations/{run_id}`.

```json
{"run_id": "b1f2…", "status": "ingerido"}
```

### Errors

| Status | When |
|--------|------|
| `422`  | The body fails schema validation (empty `platform`, empty `conversations`, a conversation with no `messages`), **or** a `use_case` names a use case that does not exist. |
| `400`  | A conversation's messages are all blank, or ingest rejected the run. Nothing is persisted. |

## `PATCH /api/v1/evaluations/{run_id}/turns/selection`

Select (or deselect) which of an ingested run's turns are scored. Scoring is **opt-in per
turn**: a freshly ingested turn is not selected (`Turn.is_selected = false`), and the worker
skips unselected turns in **both** retrieval and scoring — so a run started without selecting
any turn scores nothing. Call this to pick the subset to evaluate **before**
`POST /api/v1/evaluations/{run_id}/start`.

The `run_id` is the one returned by `POST /api/v1/evaluations` or `POST /api/v1/captures`; the turn ids are
the `turn_id`s discoverable from [`GET /api/v1/scenarios/{scenario_id}/turns`](#get-apiv1scenariosscenario_idturns)
(or `GET /api/v1/runs?granularity=metric_scores`). The call is bulk and idempotent: it flags every
listed turn that belongs to the run to `is_selected`, ignoring ids that don't belong to the
run (or aren't valid UUIDs), and reports how many turns it actually changed. Selecting is
allowed **only while the run is still `ingerido`** — once it has been started, the selection
is frozen.

- **Content type:** `application/json`

| Field         | Type       | Required | Default | Description |
|---------------|------------|----------|---------|-------------|
| `turn_ids`    | `string[]` | yes      | —       | Turn UUIDs to (de)select. Ids not belonging to the run, or malformed, are ignored. |
| `is_selected` | `boolean`  | no       | `true`  | `true` selects the listed turns for scoring; `false` deselects them. |

### Response `200`

```json
{"run_id": "b1f2…", "updated": 3}
```

- `updated` — how many turns were actually changed (listed ids that belong to the run).

### Errors

| Status | When |
|--------|------|
| `404`  | No run with that `run_id` exists (unknown or malformed id). |
| `409`  | The run is no longer in the `ingerido` state (already started/queued/scored); the selection is frozen once scoring begins. |

## `POST /api/v1/evaluations/{run_id}/start`

Start scoring a previously-ingested run — the explicit trigger decoupled from
ingestion. Flips the run from `ingerido` to `en_cola` and enqueues the Celery chain,
which retrieves and scores one turn per job. This is the only way a run moves past
`ingerido`. Only the
turns selected via `PATCH /api/v1/evaluations/{run_id}/turns/selection` are retrieved and scored;
if none were selected, the run completes without producing any scores.

- **Content type:** none (no body); the `run_id` is the one returned by
  `POST /api/v1/evaluations` or `POST /api/v1/captures`.

### Response `202`

```json
{"run_id": "b1f2…", "status": "en_cola"}
```

Poll `GET /api/v1/evaluations/{run_id}` for progress and results.

### Errors

| Status | When |
|--------|------|
| `404`  | No run with that `run_id` exists (unknown or malformed id). |
| `409`  | The run is not in the `ingerido` state (already started/queued/scored). A run is never enqueued twice. |

## `GET /api/v1/evaluations/{run_id}`

Poll a run's current status and summary. Read the `status` to know where the run
is in its lifecycle (`ingerido → en_cola → en_proceso →
completado|parcial|fallido`). A freshly ingested run stays at `ingerido` until
`POST /api/v1/evaluations/{run_id}/start` is called.

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
  (some failed), `fallido` (none scored or the retrieval/scoring job errored); `ingerido`
  before `/start`, then `en_cola` / `en_proceso` while queued or being scored.
- `progress` — **turn-level** progress: `done` of `total` turns scored, with
  `ratio` = `done / total` (0.0–1.0) for a progress bar. A turn counts as done once
  its `turn_score` is set. The ratio climbs live while `en_proceso`; on
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

## `GET /api/v1/runs`

Retrieve full scored details for the runs matching a set of filters, as a **list**.
Unlike `GET /api/v1/evaluations/{run_id}` (a single run's shallow poll summary), `/api/v1/runs`
returns the deep, granularity-configurable shape.

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
rollups; `scenario_results` → adds each scenario (its `id`, `scenario_id`, `use_case`,
`model_name`, `status`, `average_score`); `metric_scores` → adds each turn and its per-metric scores
(`metric_name`, `score`, `judge_model`, `rubric_version`). Each score's structured `trace`
is persisted on the `metric_traces` table but is not surfaced by this endpoint.

Each scenario carries both an `id` (its `ScenarioResult` UUID — the unique handle
[`GET /api/v1/scenarios/{scenario_id}/turns`](#get-apiv1scenariosscenario_idturns) takes) and the
human-readable, **non-unique** `scenario_id` label (e.g. the file stem).

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

## `GET /api/v1/runs/{run_id}/scenarios`

Retrieve one run's scenario results as a **flat** list of rollups. Same data as the
`scenario_results` granularity of [`GET /api/v1/runs`](#get-apiv1runs), but scoped by path
and already flattened: no `platforms[]` nesting to walk, each entry instead names the
`platform` it ran under. A run holding several platform executions yields every one of its
scenarios in a single list.

Depth stops at the scenario rollup — no turns, no metric scores. Read a scenario's turns
with [`GET /api/v1/scenarios/{scenario_id}/turns`](#get-apiv1scenariosscenario_idturns),
using the `id` of each entry.

Unlike the `run_id` **query parameter** on `GET /api/v1/runs` (which yields `[]` for an
unknown id), an unknown or malformed `{run_id}` here is a `404`.

### Query parameters

Both are optional, exact matches, combined with AND.

| Param      | Type     | Default | Description |
|------------|----------|---------|-------------|
| `platform` | `string` | —       | Exact, **case-sensitive** platform match (e.g. `claude`, `copilot`, `gemini`). |
| `status`   | `string` | —       | Exact match on the scenario's `status` (e.g. `completado`, `pending`). |

### Response `200`

Ordered by platform, then by the `scenario_id` label. A known run whose scenarios no
filter matches yields `[]`.

```json
[
  {
    "id": "3f0a…",
    "scenario_id": "reseña-hotel",
    "platform": "claude",
    "use_case": "rag_completo",
    "model_name": null,
    "status": "completado",
    "average_score": 0.81
  }
]
```

Each scenario carries both an `id` (its `ScenarioResult` UUID) and the human-readable,
**non-unique** `scenario_id` label (e.g. the file stem). `model_name` is the model that
answered, when the capturing client reported one — always `null` for an `.xlsx` import.
`average_score` is `null` until the scenario has been scored.

### Errors

| Status | When |
|--------|------|
| `404`  | The `run_id` is unknown or not a valid UUID. No-match filters are **not** errors — they return `[]`. |

## `GET /api/v1/platform-executions`

Retrieve platform executions as a **flat** list of rollups, filtered by platform and
scoring window. Same data as the `platform_executions` granularity of
[`GET /api/v1/runs`](#get-apiv1runs), but one entry per platform execution instead of per
run: there is no `platforms[]` nesting to walk, and each entry names the `run_id` it
belongs to. A run holding several platform executions (files can override the platform)
yields one entry per execution, all sharing that `run_id`.

Depth stops at the platform rollup — no scenario results, no turns. Read a run's scenarios
via [`GET /api/v1/runs/{run_id}/scenarios`](#get-apiv1runsrun_idscenarios).

### Query parameters

Both are optional and combined with AND.

| Param        | Type     | Default | Description |
|--------------|----------|---------|-------------|
| `platform`   | `string` | —       | Exact, **case-sensitive** platform match (e.g. `claude`, `copilot`, `gemini`). |
| `start_date` | `string` | —       | ISO-8601 lower bound (`YYYY-MM-DD` or full timestamp) on the scoring window. |
| `end_date`   | `string` | —       | ISO-8601 upper bound on the scoring window. |

The date range filters the **scoring window** (`started_at` / `finished_at`) with the same
semantics as [`GET /api/v1/runs`](#get-apiv1runs): `started_at >= start_date` and
`finished_at <= end_date`. Both columns stay `null` until a worker scores the run, so a
bound excludes still-queued and in-progress executions. The two bounds are therefore
asymmetric — a lower bound alone still returns an execution that started but has not
finished, while any upper bound excludes it.

Results are ordered by the run's creation date, then by `platform` (which breaks the tie
between the several executions of one run). `id` is the `PlatformExecution` UUID;
`average_score` is the mean of this platform's scenario averages, `null` until scored.

### Response `200`

```json
[
  {
    "id": "9c4e…",
    "run_id": "b1f2…",
    "platform": "claude",
    "started_at": "2026-07-10T12:00:00+00:00",
    "finished_at": "2026-07-10T12:05:00+00:00",
    "average_score": 0.81,
    "scenarios": 4,
    "status_breakdown": {"completado": 4}
  }
]
```

### Errors

| Status | When |
|--------|------|
| `400`  | `start_date`/`end_date` is not a valid ISO-8601 date. No-match filters are **not** errors — they return `[]`. |

## `GET /api/v1/scenarios/{scenario_id}/turns`

Retrieve a single scenario's turns, in `turn_number` order — the conversation
**content** (`prompt` / `response` / `expected_output` / `retrieved_context_source`)
alongside each turn's rolled-up `turn_score` and per-metric scores. `GET /api/v1/runs`
(even at `metric_scores` granularity) omits the turn content; this endpoint surfaces it,
so a caller can read what was actually scored without re-uploading the source.

The `{scenario_id}` is a **`ScenarioResult` UUID** — the unique handle for one
conversation scored under one platform in one run — discoverable as the `id` on each
scenario in `GET /api/v1/runs?granularity=scenario_results` or, flat,
[`GET /api/v1/runs/{run_id}/scenarios`](#get-apiv1runsrun_idscenarios). The human-readable, non-unique
`ScenarioResult.scenario_id` label is **not** accepted here (it can match many
scenarios). Takes no query parameters.

Returns one entry per turn (a scenario with no turns yields `[]`):

```json
[
  {
    "turn_id": "7c9e…",
    "turn_number": 1,
    "prompt": "¿Cuántos habitantes tiene Madrid?",
    "response": "Madrid tiene unos 3,3 millones de habitantes.",
    "expected_output": null,
    "retrieved_context_source": null,
    "turn_score": 0.8,
    "metric_scores": [
      {"metric_name": "utilidad", "score": 0.8, "judge_model": "claude-opus-4-8", "rubric_version": "v1"},
      {"metric_name": "hallucination", "score": null, "judge_model": null, "rubric_version": "v1"}
    ]
  }
]
```

A `null` `score` means the metric did not apply to that turn — it had nothing to
measure (no retrieved context, no claims), so it is also left out of the turn,
scenario and platform averages.

Like the other read paths, the per-metric structured `trace` is not surfaced here —
read it via [`GET /api/v1/turns/{turn_id}/traces`](#get-apiv1turnsturn_idtraces) using each entry's
`turn_id`.

### Errors

| Status | When |
|--------|------|
| `404`  | The `scenario_id` is unknown or not a valid UUID. |

## `GET /api/v1/metrics`

List every metric that can be linked to a use case — the catalog to pick from when
composing one. Read straight off the code registry, so adding a *metric* is still a
code change; only composing use cases from them is runtime data.

### Response `200`

```json
[
  {"name": "answer_relevance", "category": "rag", "weight": 1.0, "rubric_version": "v1"},
  {"name": "hallucination", "category": "seguridad", "weight": 1.0, "rubric_version": "v1"}
]
```

Ordered by `name`. See the [Metrics catalog](metrics-catalog.md) for what each measures.

## `POST /api/v1/use-cases`

Create a use case: a **name** plus the set of metrics a conversation ingested under it
is scored with. This is what makes a metric set data rather than code — see
[Data model → UseCase](data-model.md#usecase).

### Body

| Field     | Type       | Required | Notes |
|-----------|------------|----------|-------|
| `name`    | `string`   | yes      | Unique. What `use_case` in an ingestion payload names. |
| `metrics` | `string[]` | yes      | At least one metric name, as listed by `GET /api/v1/metrics`. Repeats are deduplicated. |

```json
{"name": "soporte", "metrics": ["hallucination", "answer_relevance"]}
```

### Response `201`

```json
{
  "id": "a20ff2cb-50ab-4c79-ae34-c42af36d0d5c",
  "name": "soporte",
  "metrics": ["answer_relevance", "hallucination"]
}
```

### Errors

| Status | When |
|--------|------|
| `422`  | Blank `name`, an empty `metrics` list, or a metric name not in the registry. |
| `409`  | A use case with that `name` already exists. |

> **No update, no delete.** Every scenario result foreign-keys the use case it was
> scored under, so changing or removing a set would rewrite what a finished run
> means. To score differently, create a new use case.

## `GET /api/v1/use-cases`

List every use case with its metric names.

### Response `200`

```json
[
  {"id": "…", "name": "default", "metrics": []},
  {"id": "…", "name": "soporte", "metrics": ["answer_relevance", "hallucination"]}
]
```

`default` scores **every registered metric** and is the only use case on a fresh
database — it is what an upload that names no use case lands on, so a run works out of
the box without composing anything. It is kept in sync automatically: a metric added to
the code catalog joins `default` on the next ingest. Create your own use case when you
want a *narrower* set than "everything".

## `GET /api/v1/prompts`

List every **prompt slot** the registered metrics render, with the version runs bind.

A slot is one prompt template a metric sends to the judge: `faithfulness_deepeval`
declares two (truths extraction and the per-claim verdict), `answer_relevance` one. The
slug and the required variables are code; the *text* is versioned data — see
[Data model → Prompt catalog](data-model.md#prompt-catalog).

The catalog is reconciled against the code registry on every call, so a slot declared by
a metric is visible here immediately, without waiting for an upload.

### Response `200`

```json
[
  {
    "id": "3f2b9e01-7c44-4a1e-9d2b-1a5c8e6f0d33",
    "metric": "faithfulness_deepeval",
    "slug": "verify",
    "required_variables": ["truths", "claim"],
    "description": "Veredicto por afirmación: 0 solo si las verdades la contradicen…",
    "active_version": {
      "id": "b71c0f5a-2d38-4e91-a0c7-5e9b3d1a8f42",
      "version": 1,
      "template": "¿Las siguientes verdades contradicen la afirmación? …",
      "status": "published",
      "is_active": true,
      "changelog": null,
      "created_by": "system",
      "created_at": "2026-07-28T10:15:00Z",
      "published_by": "system",
      "published_at": "2026-07-28T10:15:00Z"
    }
  }
]
```

Ordered by `metric` then `slug`. A metric declaring no slot does not appear.

`active_version` is `null` when no published version is active for the slot. That is the
state of a slot declared by a metric *after* the prompt-catalog migration: `sync_prompts`
creates the row so the slot is visible, but it never invents text. Scoring refuses to run
for that metric until a version is published — see
[Data model → Prompt catalog](data-model.md#prompt-catalog).

`required_variables` are the placeholders the **metric** fills before the call. A
template may additionally use `{prompt}`, `{response}` and `{context}`, which the
**judge** fills from the turn, and nothing else.

## `GET /api/v1/prompts/{prompt_id}`

One prompt slot with its full edit history, newest version first.

### Response `200`

```json
{
  "id": "3f2b9e01-7c44-4a1e-9d2b-1a5c8e6f0d33",
  "metric": "faithfulness_deepeval",
  "slug": "verify",
  "required_variables": ["truths", "claim"],
  "description": "Veredicto por afirmación…",
  "versions": [
    {"version": 5, "status": "published", "is_active": true,  "template": "…", "…": "…"},
    {"version": 3, "status": "discarded", "is_active": false, "template": "…", "…": "…"},
    {"version": 1, "status": "published", "is_active": false, "template": "…", "…": "…"}
  ]
}
```

Version numbers have **gaps**: the counter is assigned when a row is created, so a
discarded draft keeps its number. That is the honest edit sequence, and it keeps
`slug@version` unique and resolvable.

### Errors

| Status | When |
|--------|------|
| `404`  | No prompt with that id — including a malformed one. |

> **Never a delete, and never a deactivate.** There is no `DELETE`: a version a finished
> benchmark points at is what makes that benchmark's scores readable. There is no route
> that clears `is_active` either — it only ever *moves* to another published version, so
> a slot that has a live version keeps one. An unwanted draft is discarded, an unwanted
> published version superseded by activating another.

## `POST /api/v1/prompts/{prompt_id}/versions`

Open a new **draft** of a slot's text. Drafts are not scored under; publishing is what
makes text live.

### Request

```json
{
  "template": "¿Las siguientes verdades contradicen la afirmación?\nVerdades: {truths}\nAfirmación: {claim}",
  "changelog": "Se aclara que 'no mencionado' no es contradicción.",
  "author": "ana"
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `template` | `string` | yes | The Spanish prompt text. Write-once: it is never rewritten after this call. |
| `changelog` | `string \| null` | no | Why the edit was made. |
| `author` | `string \| null` | no | Stored as `created_by`; max 128 chars. |

### Response `201`

The created version, shaped like the `active_version` object above — `status` is
`"draft"`, `is_active` is `false`, and `published_by` / `published_at` are `null`.

### Errors

| Status | When |
|--------|------|
| `404`  | No prompt with that id — including a malformed one. |
| `422`  | `template` is empty or whitespace-only. |

> **Saving over a draft burns a version number.** `template` is write-once *even while a
> version is a draft*, so a re-save discards the open draft and inserts a new row. Each
> slot has at most one open draft, enforced by a partial unique index. The template is
> **not** validated here — a draft you cannot save until it is correct is not a draft.

## `POST /api/v1/prompts/{prompt_id}/versions/{version_id}/publish`

Validate a draft against its slot's contract and make it the live version.

### Request

```json
{"author": "ana"}
```

`author` is optional and stored as `published_by`.

### Response `200`

The published version: `status` is `"published"`, `is_active` is `true`, and
`published_by` / `published_at` are set.

### Errors

| Status | When |
|--------|------|
| `404`  | Unknown or malformed `prompt_id` or `version_id` — including a version belonging to a different prompt. |
| `409`  | The version is not a draft (already published, or discarded). |
| `422`  | The template omits a required variable, or uses one the slot does not declare. |

A `422` body names the offending variables:

```json
{"detail": "La plantilla no usa la(s) variable(s) requerida(s): truths."}
```

> **Publishing activates.** The previously active version is deactivated in the same
> transaction, so exactly one version is live at every instant. A published-but-inactive
> version is precisely the state scoring refuses to run under, so there is no way to
> reach it deliberately. A run already in flight is unaffected: it pins the versions it
> scored under at the start of scoring.

## `POST /api/v1/prompts/{prompt_id}/versions/{version_id}/activate`

Roll the live version back to an **already-published** one. The text is not copied
forward; `is_active` simply moves.

Takes no request body.

### Response `200`

The now-active version.

### Errors

| Status | When |
|--------|------|
| `404`  | Unknown or malformed `prompt_id` or `version_id`. |
| `409`  | The version is a draft or has been discarded — only a published version can be activated. |

Activating the version that is already live is a `200` no-op, so the control is
idempotent.

## `POST /api/v1/prompts/{prompt_id}/versions/{version_id}/discard`

Abandon an open draft. The row and its version number are kept — nothing is deleted.

Takes no request body.

### Response `200`

The discarded version, with `status` set to `"discarded"`.

### Errors

| Status | When |
|--------|------|
| `404`  | Unknown or malformed `prompt_id` or `version_id`. |
| `409`  | The version is not a draft. |

> **A published version is never discardable, even when inactive.** A finished run's
> `run_prompt_bindings` row points at it, and marking it discarded would claim that run
> scored under nothing.

> **Known edge: concurrent edits to one slot.** The next version number is computed from
> the slot's current history, so two operators opening a draft for the *same* slot at the
> same instant produce a unique-constraint violation for the loser, surfacing as a `500`.
> The constraints guarantee the stored state stays correct — the cost of the race is an
> ugly error, not a wrong catalog. Retry the request.

## Auth providers CRUD

Manage the retrieval pipeline's credential store — the [`auth_providers`](data-model.md#authproviderconfig)
table read by the authorize stage (see [Retrieval credentials](retrieval-credentials.md)). One
enabled row per gated `host` configures how that host is authenticated.

> **Secrets are write-only.** The certificate `private_key` (a PEM) is accepted on create /
> update, stored **encrypted** (per-row salt), and **never returned** — reads expose only a
> `has_private_key` boolean. These endpoints handle secrets and carry **no built-in auth**;
> restrict them at the network / deployment layer.

| Method & path | Purpose | Success |
|---|---|---|
| `GET /api/v1/auth-providers` | List providers; optional `provider`, `host`, `enabled` query filters (AND-combined). | `200` — array of provider views |
| `POST /api/v1/auth-providers` | Create a provider row. | `201` — the created view |
| `GET /api/v1/auth-providers/{id}` | Fetch one provider by UUID. | `200` |
| `PATCH /api/v1/auth-providers/{id}` | Partial update; only the supplied fields change. Sending `private_key` rotates the stored key (a `null`/empty value clears it); omitting it leaves the key untouched. | `200` — the updated view |
| `DELETE /api/v1/auth-providers/{id}` | Delete a provider row. | `204` — no content |

**Body (create / update).** `provider` (kind, e.g. `"sharepoint"` or `"oauth2"`) and `host`
are required on create; `enabled` (default `true`), `tenant_id`, `client_id`, `thumbprint`,
`site_url`, `settings` (JSON), and the write-only `private_key` are optional. `provider` must
be a registered kind. `private_key` carries **that kind's secret** — a certificate private key
for `sharepoint`, an OAuth2 client secret for `oauth2` — and kind-specific non-secret config
(e.g. the OAuth2 `token_url` / `scope`) goes in `settings`.

**Read view.** `id`, `provider`, `host`, `enabled`, the SharePoint identifiers,
`has_private_key`, `settings`, `created_at`, `updated_at`.

### Errors

| Status | When |
|--------|------|
| `404`  | Unknown `{id}` on get / update / delete. |
| `409`  | Create / update would duplicate an existing `(provider, host)` pair. |
| `422`  | Unknown `provider` kind; a `private_key` supplied while `AUTH_ENCRYPTION_KEY` is unset; an empty `PATCH` body; a malformed UUID or missing required field. |

## `GET /api/v1/turns/{turn_id}/traces`

Retrieve the structured metric traces for a single turn — the full per-metric
reasoning that the run/scenario read paths omit. The `turn_id` is the turn's UUID,
discoverable from `GET /api/v1/runs?granularity=metric_scores` (each turn carries a
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

## `GET /api/v1/turns/{turn_id}/token-usage`

Retrieve the LLM token usage for scoring a **single turn** — the turn's 1:1
[`TurnTokenUsage`](data-model.md) entity, read raw with **no aggregation** across turns,
scenarios, or platforms. Summed across every judge call every metric made while scoring the
turn, with provider counts normalized to input/output. The `turn_id` is the turn's UUID,
discoverable from `GET /api/v1/runs?granularity=metric_scores` (each turn carries a `turn_id`). Takes
no query parameters.

`total_tokens` is **derived** (`input_tokens + output_tokens`) and never stored. A turn that
was never scored — or whose scoring recorded no usage — reports zeros rather than `404`ing;
only an unknown or malformed `turn_id` is a `404`.

### Response `200`

```json
{
  "turn_id": "7c9e…",
  "input_tokens": 1280,
  "output_tokens": 320,
  "total_tokens": 1600
}
```

### Errors

| Status | When |
|--------|------|
| `404`  | The `turn_id` is unknown or not a valid UUID. |

## Examples

Ingest (defaults, single file) → returns a `run_id` at status `ingerido`:

```bash
curl -X POST http://localhost:8001/api/v1/evaluations \
  -F 'files=@esc1.xlsx' \
  -F 'payload={"platform":"claude","use_case":"default"}'
# {"run_id":"b1f2…","status":"ingerido"}
```

Select which turns to score (opt-in; do this before starting) → returns how many were flagged:

```bash
curl -X PATCH http://localhost:8001/api/v1/evaluations/b1f2…/turns/selection \
  -H 'Content-Type: application/json' \
  -d '{"turn_ids": ["7c9e…", "8d0f…"], "is_selected": true}'
# {"run_id":"b1f2…","updated":2}
```

Start scoring (decoupled from ingestion) → run moves to `en_cola`. Only the selected turns
are scored:

```bash
curl -X POST http://localhost:8001/api/v1/evaluations/b1f2…/start
# {"run_id":"b1f2…","status":"en_cola"}
```

Poll until terminal:

```bash
curl http://localhost:8001/api/v1/evaluations/b1f2…
```

Ingest a conversation captured from a chat UI (what the browser extension sends):

```bash
curl -X POST http://localhost:8001/api/v1/captures \
  -H 'Content-Type: application/json' \
  -d '{
        "platform": "claude",
        "use_case": "default",
        "conversations": [{
          "scenario_id": "reseña-hotel-2026-07-22-11-30",
          "model_name": "Claude Opus 4.5",
          "source_ref": "https://claude.ai/chat/abc123",
          "messages": [
            {"role": "user",  "content": "¿Cuántos habitantes tiene Madrid?"},
            {"role": "model", "content": "Madrid tiene unos 3,3 millones de habitantes."}
          ]
        }]
      }'
# {"run_id":"b1f2…","status":"ingerido"} — then POST /api/v1/evaluations/{run_id}/start to score
```

Retrieve full details for all `claude` runs scored in July, down to metric scores:

```bash
curl 'http://localhost:8001/api/v1/runs?platform=claude&start_date=2026-07-01&end_date=2026-07-31&granularity=metric_scores'
```

List one run's scenarios, flat — all of them, then only the scored `claude` ones:

```bash
curl 'http://localhost:8001/api/v1/runs/b1f2…/scenarios'
curl 'http://localhost:8001/api/v1/runs/b1f2…/scenarios?platform=claude&status=completado'
```

Read a scenario's turns (its `id` comes from `/api/v1/runs/{run_id}/scenarios`):

```bash
curl 'http://localhost:8001/api/v1/scenarios/3f0a…/turns'
```

Retrieve one turn's metric traces (full, then minimal):

```bash
curl 'http://localhost:8001/api/v1/turns/7c9e…/traces'
curl 'http://localhost:8001/api/v1/turns/7c9e…/traces?provenance=false'
```

Read one turn's token usage:

```bash
curl 'http://localhost:8001/api/v1/turns/7c9e…/token-usage'
```

Multiple files with per-file overrides — `esc1` keeps the payload `claude` default;
`esc2` is scored under `gemini` (producing two platform executions in the one run):

```bash
curl -X POST http://localhost:8001/api/v1/evaluations \
  -F 'files=@esc1.xlsx' \
  -F 'files=@esc2.xlsx' \
  -F 'payload={
        "platform": "claude",
        "use_case": "default",
        "files": {
          "esc1.xlsx": {"scenario_id": "esc1", "use_case": "rag_completo"},
          "esc2.xlsx": {"platform": "gemini"}
        }
      }'
```

Compose a use case, then ingest under it:

```bash
curl http://localhost:8001/api/v1/metrics
# [{"name":"answer_relevance","category":"rag","weight":1.0,"rubric_version":"v1"}, …]

curl -X POST http://localhost:8001/api/v1/use-cases \
  -H 'Content-Type: application/json' \
  -d '{"name":"soporte","metrics":["hallucination","answer_relevance"]}'
# {"id":"a20ff2cb-…","name":"soporte","metrics":["answer_relevance","hallucination"]}

curl -X POST http://localhost:8001/api/v1/evaluations \
  -F 'files=@esc1.xlsx' \
  -F 'payload={"platform":"copilot","use_case":"soporte"}'
# {"run_id":"…","status":"ingerido"}

# A use case that does not exist is rejected up front:
curl -X POST http://localhost:8001/api/v1/evaluations \
  -F 'files=@esc1.xlsx' \
  -F 'payload={"platform":"copilot","use_case":"inexistente"}'
# 422 {"detail":"Caso(s) de uso desconocido(s): inexistente. Créalo con POST /use-cases."}
```

Inspect the prompts the judge runs, then read one slot's history:

```bash
curl http://localhost:8001/api/v1/prompts
# [{"metric":"answer_relevance","slug":"generate_question","required_variables":[],
#   "active_version":{"version":1,"status":"published","is_active":true,"template":"…"}}, …]

curl http://localhost:8001/api/v1/prompts/3f2b9e01-7c44-4a1e-9d2b-1a5c8e6f0d33
# {"metric":"faithfulness_deepeval","slug":"verify","versions":[{"version":1, …}]}
```

Edit one: draft the new text, publish it (which makes it live), then roll back:

```bash
PROMPT=3f2b9e01-7c44-4a1e-9d2b-1a5c8e6f0d33

VERSION=$(curl -s -X POST http://localhost:8001/api/v1/prompts/$PROMPT/versions \
  -H 'Content-Type: application/json' \
  -d '{"template":"¿Contradicen estas verdades la afirmación?\nVerdades: {truths}\nAfirmación: {claim}",
       "changelog":"Se aclara que omitir algo no es contradecirlo.","author":"ana"}' | jq -r .id)
# 201 {"version":2,"status":"draft","is_active":false, …}

curl -X POST http://localhost:8001/api/v1/prompts/$PROMPT/versions/$VERSION/publish \
  -H 'Content-Type: application/json' -d '{"author":"ana"}'
# 200 {"version":2,"status":"published","is_active":true,"published_by":"ana", …}
# 422 {"detail":"La plantilla no usa la(s) variable(s) requerida(s): truths."}  ← if it were wrong

# The v1 that was live is still published, just no longer active — roll back to it:
curl -X POST http://localhost:8001/api/v1/prompts/$PROMPT/versions/$V1/activate
# 200 {"version":1,"is_active":true, …}
```

Configure a SharePoint credential provider, then list it (note the key is not echoed back):

```bash
curl -X POST http://localhost:8001/api/v1/auth-providers \
  -H 'Content-Type: application/json' \
  -d '{
        "provider": "sharepoint",
        "host": "cognitactix-my.sharepoint.com",
        "tenant_id": "<tenant-guid>",
        "client_id": "<app-client-id>",
        "thumbprint": "<cert-thumbprint>",
        "site_url": "https://cognitactix-my.sharepoint.com/sites/x",
        "private_key": "-----BEGIN PRIVATE KEY-----\n…"
      }'
# {"id":"7d3e…","provider":"sharepoint","has_private_key":true, … }  (no private_key field)

curl http://localhost:8001/api/v1/auth-providers
```

## Running the worker

```bash
celery -A scorekeeper.celery_app:celery_app worker --loglevel=info --concurrency=2
```

Under Docker Compose this is the `worker` service; it shares the app image and the
`DATABASE_URL` (which also backs the broker). Set `CELERY_BROKER_URL` to point at a
dedicated broker (e.g. Redis) instead of Postgres.

## Related code

- App factory & router mounting — `scorekeeper-engine/src/scorekeeper/main.py`, `scorekeeper-engine/src/scorekeeper/api/router.py`
- Endpoints — `scorekeeper-engine/src/scorekeeper/api/v1/` (one module per resource);
  request/response models — `scorekeeper-engine/src/scorekeeper/api/v1/schemas.py`
- Ingest / retrieve / score split + polling — `scorekeeper-engine/src/scorekeeper/core/services/`
  (`ingestion.ingest_evaluation`, `runs.set_turn_selection`, `retrieval.retrieve_run`,
  `scoring.score_run`, `runs.get_run_summary`, `scoring.run_evaluation`)
- Read paths — `scorekeeper-engine/src/scorekeeper/core/services/read_models.py` (`retrieve_runs`,
  `retrieve_run_scenarios`, `retrieve_platform_executions`, `retrieve_scenario_turns`,
  `retrieve_turn_traces`, `retrieve_turn_token_usage`);
  their projections — `scorekeeper-engine/src/scorekeeper/core/services/serializers.py`
- Queries — `scorekeeper-engine/src/scorekeeper/db/repositories/`; models — `scorekeeper-engine/src/scorekeeper/db/models.py`
- Retrieval orchestrator — `scorekeeper-engine/src/scorekeeper/core/retrieval/pipeline.py` (`RetrievalOrchestrator`)
- Auth-provider CRUD service — `scorekeeper-engine/src/scorekeeper/core/retrieval/credentials/service.py`
- Use-case composition service — `scorekeeper-engine/src/scorekeeper/core/services/use_cases.py`
- Prompt catalog service — `scorekeeper-engine/src/scorekeeper/core/services/prompts.py`
  (reads, plus the `create_version` → `publish_version` → `activate_version` / `discard_version`
  lifecycle); its queries — `scorekeeper-engine/src/scorekeeper/db/repositories/prompts.py`
- Prompt slot declarations & template validation — `scorekeeper-engine/src/scorekeeper/core/metrics/prompts.py`
  (`PromptSlot`, `safe_format`, `validate_template` — the publish gate); each metric's slots live
  on its class in `scorekeeper-engine/src/scorekeeper/core/metrics/catalog/`
- Celery app & tasks — `scorekeeper-engine/src/scorekeeper/celery_app.py`, `scorekeeper-engine/src/scorekeeper/tasks.py`
  (`run_chain_task` starts a run, `score_turn_task` does one turn; `enqueue_run` /
  `enqueue_turn`); the chain itself — `scorekeeper-engine/src/scorekeeper/core/services/chain.py`
  (`start_chain`, `advance_chain`)
- Parsing & message normalization — `scorekeeper-engine/src/scorekeeper/core/importer.py`
  (`parse_conversation`, `normalize_messages`)
- Browser capture client — `extension/` (see its [README](../extension/README.md))
- Scoring — `scorekeeper-engine/src/scorekeeper/core/runner.py`
- Metric selection — `scorekeeper-engine/src/scorekeeper/core/metrics/selection.py`
  (`sync_metrics` mirrors the registry into `metrics`; `sync_prompts` mirrors each metric's
  prompt slots into `prompts`, seeding a published v1; `resolve` reads a use case's set)
