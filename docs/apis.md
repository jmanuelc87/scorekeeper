# HTTP APIs

The HTTP API (`scorekeeper.api`, run with `scorekeeper-api`) serves the results
dashboard, triggers evaluations, and manages the retrieval credential store. This page
documents every endpoint; add new ones as their own `##` section below. For the models these
endpoints read and write, see the [Data model](data-model.md); for how metrics are chosen and
scored, see [Evaluation metrics](evaluation-metrics.md); for the credential store, see
[Retrieval credentials](retrieval-credentials.md).

| Method & path                | Purpose |
|------------------------------|---------|
| `GET /health`                | Liveness probe. |
| `POST /evaluations`          | Ingest conversation `.xlsx` files for scoring (per-file platform, defaulting to the payload platform). Persists at `ingerido`; does **not** start scoring. |
| `POST /captures`             | Ingest conversations captured from a chat UI as JSON for scoring (the browser extension's entry point). Persists at `ingerido`; does **not** start scoring. |
| `PATCH /evaluations/{run_id}/turns/selection` | Select (or deselect) which of an ingested run's turns are scored — scoring is **opt-in per turn**. Must be called before `/start`. |
| `POST /evaluations/{run_id}/start` | Start scoring an ingested run: flip it from `ingerido` to `en_cola` and **enqueue** the pipeline. |
| `GET /evaluations/{run_id}`  | Poll a run's status and summary. |
| `GET /runs`                  | Retrieve full scored run details, filtered and at a chosen granularity. |
| `GET /scenarios/{scenario_id}/turns` | Retrieve one scenario's turns — conversation content plus per-metric scores. |
| `GET /auth-providers`        | List the retrieval credential store's provider rows (optional filters). |
| `POST /auth-providers`       | Create a credential provider row (write-only `private_key`, stored encrypted). |
| `GET /auth-providers/{id}`   | Fetch one credential provider by UUID. |
| `PATCH /auth-providers/{id}` | Partially update a credential provider (rotate/clear its key). |
| `DELETE /auth-providers/{id}`| Delete a credential provider row. |
| `GET /turns/{turn_id}/traces` | Retrieve the structured metric traces for a single turn. |
| `GET /turns/{turn_id}/token-usage` | Retrieve one turn's raw LLM token usage (no aggregation). |

## Architecture: ingestion is decoupled from scoring; the worker retrieves + scores

Retrieval (fetching/extracting each turn's source documents) and scoring (the LLM judge, once
per metric per turn) are both slow, so they run **off the request path**. Ingestion is also
**decoupled from the start of scoring**: `POST /evaluations` (and `POST /captures`) parses the
upload and persists the run tree synchronously at status `ingerido`, then returns `202` — it
does **not** enqueue anything. Scoring is also **opt-in per turn**: an ingested turn's
`Turn.is_selected` defaults to `false`, and both worker phases (retrieval and scoring) skip
any turn that is not selected — so a run scored without selecting turns scores nothing. A
client picks the subset with `PATCH /evaluations/{run_id}/turns/selection` before starting.
(An unselected turn is still fed to later selected turns' judge **history**, so the
conversation the judge sees stays complete; it just isn't scored itself.) Scoring starts only
when a client calls `POST /evaluations/{run_id}/start`, which flips the run to `en_cola` and
enqueues one Celery job. A separate **worker** process
(`celery -A scorekeeper.celery_app:celery_app worker`)
consumes the queue and runs the pipeline orchestrator `run_pipeline_task`: **retrieval first,
then scoring** (`evaluation.retrieve_run` → `evaluation.score_run`). The broker is the app's own
Postgres (kombu's SQLAlchemy transport — no extra service); there is no Celery result backend,
so clients track progress by polling `GET /evaluations/{run_id}`, which reads
`BenchmarkRun.status`.

Run status lifecycle: `ingerido` (persisted, not started) → `en_cola` (queued, after
`/start`) → `en_recuperacion` (a worker is retrieving the turns' source documents) →
`en_proceso` (a worker is scoring) → `completado` | `parcial` | `fallido` (terminal rollup;
`fallido` also marks a run whose retrieval or scoring raised).

## `GET /health`

Liveness probe. Returns `200` with `{"status": "ok"}`. Takes no parameters.

## `POST /evaluations`

Ingest uploaded conversation `.xlsx` files and persist them for scoring. Each file
is scored under its own platform (a per-file override, defaulting to the payload
platform). Parsing and persistence happen synchronously (so a malformed sheet is
rejected here). Ingestion is **decoupled** from scoring: this endpoint does not start
anything — the run lands at status `ingerido` and stays there until
`POST /evaluations/{run_id}/start` enqueues it. This is the glue between the parser
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
| `use_case`  | `string`                 | no       | `"default"` | Default metric-selection use case for every file. Comma-separated tokens are unioned (e.g. `"faithfulness_ragas,hallucination"`). |
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
   `model` content the `response` (a missing side becomes `""`). The raw
   `retrieved_context` cell is stored verbatim on `Turn.retrieved_context_source` — it is
   **not** interpreted here; the retrieval pipeline handles it in the worker.
3. Build and commit the `BenchmarkRun → PlatformExecution → ScenarioResult → Turn`
   tree — one `PlatformExecution` per distinct platform — with status `ingerido`. Every
   `Turn` lands with `is_selected = false`. Nothing is enqueued; the run waits for a client
   to select turns (`PATCH /evaluations/{run_id}/turns/selection`) and call
   `POST /evaluations/{run_id}/start`.

After `POST /evaluations/{run_id}/start` flips the run to `en_cola` and enqueues the
pipeline job (carrying just the `run_id`), the worker (`run_pipeline_task`, off the
request path) processes **only the selected turns**:

4. **Retrieval** (`retrieve_run`): mark the run `en_recuperacion` and run the retrieval
   pipeline over each **selected** turn's `retrieved_context_source`, populating
   `retrieved_documents` (best-effort; commits **per scenario**). Unselected turns are
   skipped (no retrieval work).
5. **Scoring** (`score_run`): mark the run `en_proceso` and score with
   `EvalRunner.run_benchmark` (one `MetricScore` per metric per **selected** turn, reading
   the just-retrieved context), rolling scores up to scenario, platform, and run level. The
   runner also commits **per scenario**, so partial progress survives an interruption.
   Unselected turns are never scored (their `turn_score` stays `null`) but still feed the
   conversation history the judge sees.
6. Roll the run status up to `completado` / `parcial` / `fallido` and commit.

> **Prerequisites.** The database schema must already exist (`alembic upgrade head`)
> and the configured judge must have a valid API key (see `scorekeeper.config.settings`). The
> worker must be running to make progress past `en_cola`, a client must select the turns
> to score (`PATCH /evaluations/{run_id}/turns/selection`) — nothing is scored otherwise —
> and call `POST /evaluations/{run_id}/start` to move a run past `ingerido`.

### Response `202`

`POST` returns as soon as the run is persisted, at status `ingerido` (not started):

```json
{"run_id": "b1f2…", "status": "ingerido"}
```

Call `POST /evaluations/{run_id}/start` to begin scoring, then poll
`GET /evaluations/{run_id}` for progress and results (both below).

### Errors

| Status | When |
|--------|------|
| `422`  | `payload` is not valid JSON or fails schema validation (e.g. empty `platform`). |
| `400`  | An uploaded file is not `.xlsx`, is empty, has no `role`/`content` columns, or no file/platform was provided. Ingest rolls back — nothing is persisted. |

## `POST /captures`

Ingest conversations **captured from a chat UI** and persist them for scoring. The
JSON twin of `POST /evaluations` for clients that already hold the turns and have no
spreadsheet to upload — the [browser extension](../extension/README.md) scrapes them
straight off Copilot, Gemini and Claude. Like `POST /evaluations`, ingestion is
decoupled from scoring: the run lands at `ingerido` and starts only via
`POST /evaluations/{run_id}/start`.

Both endpoints converge immediately: the messages are normalized by
`scorekeeper.core.importer.normalize_messages` (the same role aliasing and turn numbering
the `.xlsx` parser uses) and persisted by the same `ingest_evaluation`, so a captured
conversation is indistinguishable downstream from an uploaded one.

- **Content type:** `application/json`

| Field           | Type       | Required | Default     | Description |
|-----------------|------------|----------|-------------|-------------|
| `platform`      | `string`   | yes      | —           | The **default** platform for the request. Applies to every conversation that does not override it. |
| `use_case`      | `string`   | no       | `"default"` | Default metric-selection use case. Comma-separated tokens are unioned. |
| `conversations` | `array`    | yes      | —           | One or more captured conversations; **each is one scenario**. Must be non-empty. |

Conversation object:

| Field         | Type              | Required | Default            | Description |
|---------------|-------------------|----------|--------------------|-------------|
| `scenario_id` | `string`          | yes      | —                  | Identifier stored on the `ScenarioResult`. |
| `messages`    | `array`           | yes      | —                  | The conversation, in order. Must be non-empty and at least one message must have content. |
| `use_case`    | `string`          | no       | the payload `use_case` | Metric-selection use case for this conversation. |
| `platform`    | `string`          | no       | the payload `platform` | Platform to score this conversation under. |
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

Identical to `POST /evaluations` — the run is persisted at `ingerido`. Call
`POST /evaluations/{run_id}/start` to begin scoring, then poll `GET /evaluations/{run_id}`.

```json
{"run_id": "b1f2…", "status": "ingerido"}
```

### Errors

| Status | When |
|--------|------|
| `422`  | The body fails schema validation (empty `platform`, empty `conversations`, a conversation with no `messages`). |
| `400`  | A conversation's messages are all blank, or ingest rejected the run. Nothing is persisted. |

## `PATCH /evaluations/{run_id}/turns/selection`

Select (or deselect) which of an ingested run's turns are scored. Scoring is **opt-in per
turn**: a freshly ingested turn is not selected (`Turn.is_selected = false`), and the worker
skips unselected turns in **both** retrieval and scoring — so a run started without selecting
any turn scores nothing. Call this to pick the subset to evaluate **before**
`POST /evaluations/{run_id}/start`.

The `run_id` is the one returned by `POST /evaluations` or `POST /captures`; the turn ids are
the `turn_id`s discoverable from [`GET /scenarios/{scenario_id}/turns`](#get-scenariosscenario_idturns)
(or `GET /runs?granularity=metric_scores`). The call is bulk and idempotent: it flags every
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

## `POST /evaluations/{run_id}/start`

Start scoring a previously-ingested run — the explicit trigger decoupled from
ingestion. Flips the run from `ingerido` to `en_cola` and enqueues the Celery pipeline
(retrieval + LLM scoring). This is the only way a run moves past `ingerido`. Only the
turns selected via `PATCH /evaluations/{run_id}/turns/selection` are retrieved and scored;
if none were selected, the run completes without producing any scores.

- **Content type:** none (no body); the `run_id` is the one returned by
  `POST /evaluations` or `POST /captures`.

### Response `202`

```json
{"run_id": "b1f2…", "status": "en_cola"}
```

Poll `GET /evaluations/{run_id}` for progress and results.

### Errors

| Status | When |
|--------|------|
| `404`  | No run with that `run_id` exists (unknown or malformed id). |
| `409`  | The run is not in the `ingerido` state (already started/queued/scored). A run is never enqueued twice. |

## `GET /evaluations/{run_id}`

Poll a run's current status and summary. Read the `status` to know where the run
is in its lifecycle (`ingerido → en_cola → en_recuperacion → en_proceso →
completado|parcial|fallido`). A freshly ingested run stays at `ingerido` until
`POST /evaluations/{run_id}/start` is called.

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
  before `/start`, then `en_cola` / `en_recuperacion` / `en_proceso` while queued,
  retrieving, or scoring.
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

## `GET /runs`

Retrieve full scored details for the runs matching a set of filters, as a **list**.
Unlike `GET /evaluations/{run_id}` (a single run's shallow poll summary), `/runs`
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
`status`, `average_score`); `metric_scores` → adds each turn and its per-metric scores
(`metric_name`, `score`, `judge_model`, `rubric_version`). Each score's structured `trace`
is persisted on the `metric_traces` table but is not surfaced by this endpoint.

Each scenario carries both an `id` (its `ScenarioResult` UUID — the unique handle
[`GET /scenarios/{scenario_id}/turns`](#get-scenariosscenario_idturns) takes) and the
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

## `GET /scenarios/{scenario_id}/turns`

Retrieve a single scenario's turns, in `turn_number` order — the conversation
**content** (`prompt` / `response` / `expected_output` / `retrieved_context_source`)
alongside each turn's rolled-up `turn_score` and per-metric scores. `GET /runs`
(even at `metric_scores` granularity) omits the turn content; this endpoint surfaces it,
so a caller can read what was actually scored without re-uploading the source.

The `{scenario_id}` is a **`ScenarioResult` UUID** — the unique handle for one
conversation scored under one platform in one run — discoverable as the `id` on each
scenario in `GET /runs?granularity=scenario_results`. The human-readable, non-unique
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
      {"metric_name": "utilidad", "score": 0.8, "judge_model": "claude-opus-4-8", "rubric_version": "v1"}
    ]
  }
]
```

Like the other read paths, the per-metric structured `trace` is not surfaced here —
read it via [`GET /turns/{turn_id}/traces`](#get-turnsturn_idtraces) using each entry's
`turn_id`.

### Errors

| Status | When |
|--------|------|
| `404`  | The `scenario_id` is unknown or not a valid UUID. |

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
| `GET /auth-providers` | List providers; optional `provider`, `host`, `enabled` query filters (AND-combined). | `200` — array of provider views |
| `POST /auth-providers` | Create a provider row. | `201` — the created view |
| `GET /auth-providers/{id}` | Fetch one provider by UUID. | `200` |
| `PATCH /auth-providers/{id}` | Partial update; only the supplied fields change. Sending `private_key` rotates the stored key (a `null`/empty value clears it); omitting it leaves the key untouched. | `200` — the updated view |
| `DELETE /auth-providers/{id}` | Delete a provider row. | `204` — no content |

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

## `GET /turns/{turn_id}/token-usage`

Retrieve the LLM token usage for scoring a **single turn** — the turn's 1:1
[`TurnTokenUsage`](data-model.md) entity, read raw with **no aggregation** across turns,
scenarios, or platforms. Summed across every judge call every metric made while scoring the
turn, with provider counts normalized to input/output. The `turn_id` is the turn's UUID,
discoverable from `GET /runs?granularity=metric_scores` (each turn carries a `turn_id`). Takes
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
curl -X POST http://localhost:8001/evaluations \
  -F 'files=@esc1.xlsx' \
  -F 'payload={"platform":"claude","use_case":"default"}'
# {"run_id":"b1f2…","status":"ingerido"}
```

Select which turns to score (opt-in; do this before starting) → returns how many were flagged:

```bash
curl -X PATCH http://localhost:8001/evaluations/b1f2…/turns/selection \
  -H 'Content-Type: application/json' \
  -d '{"turn_ids": ["7c9e…", "8d0f…"], "is_selected": true}'
# {"run_id":"b1f2…","updated":2}
```

Start scoring (decoupled from ingestion) → run moves to `en_cola`. Only the selected turns
are scored:

```bash
curl -X POST http://localhost:8001/evaluations/b1f2…/start
# {"run_id":"b1f2…","status":"en_cola"}
```

Poll until terminal:

```bash
curl http://localhost:8001/evaluations/b1f2…
```

Ingest a conversation captured from a chat UI (what the browser extension sends):

```bash
curl -X POST http://localhost:8001/captures \
  -H 'Content-Type: application/json' \
  -d '{
        "platform": "claude",
        "use_case": "default",
        "conversations": [{
          "scenario_id": "reseña-hotel-2026-07-22-11-30",
          "source_ref": "https://claude.ai/chat/abc123",
          "messages": [
            {"role": "user",  "content": "¿Cuántos habitantes tiene Madrid?"},
            {"role": "model", "content": "Madrid tiene unos 3,3 millones de habitantes."}
          ]
        }]
      }'
# {"run_id":"b1f2…","status":"ingerido"} — then POST /evaluations/{run_id}/start to score
```

Retrieve full details for all `claude` runs scored in July, down to metric scores:

```bash
curl 'http://localhost:8001/runs?platform=claude&start_date=2026-07-01&end_date=2026-07-31&granularity=metric_scores'
```

Read a scenario's turns (its `id` comes from `/runs?granularity=scenario_results`):

```bash
curl 'http://localhost:8001/scenarios/3f0a…/turns'
```

Retrieve one turn's metric traces (full, then minimal):

```bash
curl 'http://localhost:8001/turns/7c9e…/traces'
curl 'http://localhost:8001/turns/7c9e…/traces?provenance=false'
```

Read one turn's token usage:

```bash
curl 'http://localhost:8001/turns/7c9e…/token-usage'
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
          "esc1.xlsx": {"scenario_id": "esc1", "use_case": "faithfulness_ragas,hallucination"},
          "esc2.xlsx": {"platform": "gemini"}
        }
      }'
```

Configure a SharePoint credential provider, then list it (note the key is not echoed back):

```bash
curl -X POST http://localhost:8001/auth-providers \
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

curl http://localhost:8001/auth-providers
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
- Ingest / retrieve / score split + polling — `src/scorekeeper/evaluation.py`
  (`ingest_evaluation`, `set_turn_selection`, `retrieve_run`, `score_run`, `get_run_summary`,
  `run_evaluation`)
- Read paths — `src/scorekeeper/evaluation.py` (`retrieve_runs`, `retrieve_scenario_turns`,
  `retrieve_turn_traces`, `retrieve_turn_token_usage`)
- Retrieval orchestrator — `src/scorekeeper/retrieval/pipeline.py` (`RetrievalOrchestrator`)
- Auth-provider CRUD service — `src/scorekeeper/retrieval/credentials/service.py`
- Celery app & tasks — `src/scorekeeper/celery_app.py`, `src/scorekeeper/tasks.py`
  (`run_pipeline_task` = retrieval then scoring; `enqueue_run`)
- Parsing & message normalization — `src/scorekeeper/importer.py`
  (`parse_conversation`, `normalize_messages`)
- Browser capture client — `extension/` (see its [README](../extension/README.md))
- Scoring — `src/scorekeeper/runner.py`
- Metric selection — `src/scorekeeper/metrics/selection.py`
