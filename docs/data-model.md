# Data model

Scorekeeper stores benchmark results as a hierarchy. The SQLAlchemy models live
in `scorekeeper-engine/src/scorekeeper/db/models.py`, and the schema is managed with Alembic (see the
"Database migrations" section in the [README](../README.md)). For how metrics are
defined and scored, see [Evaluation metrics](evaluation-metrics.md).

## Overview

A **SourceFile** is an imported `.xlsx` of interactions that seeds one or more
**BenchmarkRun** rows. Each run fans out into one **PlatformExecution** per
platform, whose conversations are stored as **ScenarioResult** rows. Every
conversation is split into **Turn** rows (one user/model exchange each), and
each turn is scored by an LLM-as-a-judge into multiple **MetricScore** rows. The
LLM tokens consumed while scoring a turn are summed into a **TurnTokenUsage** row
(one per turn).

Scores flow upward:

- a turn's `turn_score` is the **weighted mean of its `MetricScore` values,
  normalized to [0, 1]**. Metrics score on their own scale (1-5, 0-1, boolean);
  each raw score is normalized and weighted using the metric's `scale`/`weight`,
  which live in code (`scorekeeper.core.metrics`) keyed by `metric_name`,
- a scenario's `average_score` averages its turns' `turn_score`,
- a platform's `average_score` averages its scenarios' `average_score`.

`MetricScore.score` stores the **raw** score in the metric's own scale;
normalization happens at rollup (`scorekeeper.core.metrics.rollup`). Because scale and
weight are code metadata (not stored per score), recomputing an old run applies
the *current* weights/scales — `rubric_version` captures rubric drift but not
weight drift. This is acceptable for a benchmarking tool whose rollups are
derived and recomputable.

Which metrics apply to a scenario is data, stored in **ScenarioMetric** and keyed
by `use_case`. Each metric class declares its scenarios via the `@register`
decorator; `scorekeeper.core.metrics.selection.sync_selection` materializes that
declaration into the table, and the scoring runner reads it to pick the metric
subset per scenario.

## ER diagram

```mermaid
erDiagram
    SourceFile {
        UUID id PK
        String filename
        String file_hash
        DateTime imported_at
        JSON sheet_metadata
    }

    BenchmarkRun {
        UUID id PK
        UUID source_file_id FK
        DateTime created_at
        String status
    }

    PlatformExecution {
        UUID id PK
        UUID run_id FK
        String platform
        DateTime started_at
        DateTime finished_at
        Float average_score
    }

    ScenarioResult {
        UUID id PK
        UUID platform_execution_id FK
        String scenario_id
        String use_case
        String source_ref
        String status
        String screenshot_path
        Float average_score
        JSON raw_conversation
    }

    Turn {
        UUID id PK
        UUID scenario_result_id FK
        Integer turn_number
        Text prompt
        Text response
        Boolean is_selected
        Text expected_output
        Text retrieved_context_source
        Integer response_time_ms
        Float turn_score
    }

    RetrievedDocument {
        UUID id PK
        UUID turn_id FK
        Float rank
        Text name
        Text document
        Text content
        Text url
    }

    MetricScore {
        UUID id PK
        UUID turn_id FK
        String metric_name
        Float score
        String judge_model
        String rubric_version
    }

    MetricTrace {
        UUID id PK
        UUID metric_score_id FK
        JSON steps
    }

    TurnTokenUsage {
        UUID id PK
        UUID turn_id FK
        Integer input_tokens
        Integer output_tokens
    }

    ScenarioMetric {
        UUID id PK
        String use_case
        String metric_name
    }

    AuthProviderConfig {
        UUID id PK
        String provider UK
        String host UK
        Boolean enabled
        String tenant_id
        String client_id
        String thumbprint
        String site_url
        Text private_key_encrypted
        Text private_key_salt
        JSON settings
        DateTime created_at
        DateTime updated_at
    }

    DocumentCacheEntry {
        UUID id PK
        Text url UK
        String sha256
        Text cache_path
        String doc_type
        String content_type
        Integer size_bytes
        DateTime fetched_at
    }

    SourceFile ||--o{ BenchmarkRun : "seeds"
    BenchmarkRun ||--o{ PlatformExecution : "has"
    PlatformExecution ||--o{ ScenarioResult : "has"
    ScenarioResult ||--o{ Turn : "has"
    Turn ||--o{ RetrievedDocument : "grounded on"
    Turn ||--o{ MetricScore : "has"
    Turn ||--|| TurnTokenUsage : "has"
    MetricScore ||--|| MetricTrace : "has"
```

`ScenarioMetric`, `AuthProviderConfig`, and `DocumentCacheEntry` are standalone tables (no FK
into the run hierarchy). `ScenarioMetric` is joined to scenarios by matching `use_case`;
`AuthProviderConfig` is read by the retrieval pipeline's authorize stage, keyed by `host`
(unique together with `provider`); `DocumentCacheEntry` is the fetch stage's download index,
keyed by `url`.

## Entities

### SourceFile

An imported `.xlsx` file of interactions — the source of one or more runs.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `filename` | String(512) | Name of the imported file. |
| `file_hash` | String(128) | Content hash (indexed) for reproducibility and dedup. |
| `imported_at` | DateTime (tz) | When the file was imported. |
| `sheet_metadata` | JSON / JSONB | Sheet names, row counts, column mapping, etc. recorded by the importer. |

### BenchmarkRun

One benchmark invocation, scored under a single platform (one `PlatformExecution`).

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `source_file_id` | UUID | FK → `source_files.id`, `ON DELETE SET NULL`. Nullable; the file the run was seeded from. |
| `created_at` | DateTime (tz) | When the run was created. |
| `status` | String(32) | Run lifecycle, e.g. `pending`, `running`, `completed`. |

### PlatformExecution

Results for a single platform (Copilot, Gemini, Claude) within a run.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `run_id` | UUID | FK → `benchmark_runs.id`, `ON DELETE CASCADE`. |
| `platform` | String(64) | Platform identifier. |
| `started_at` | DateTime (tz) | Nullable until the execution begins. |
| `finished_at` | DateTime (tz) | Nullable until the execution ends. |
| `average_score` | Float | Mean of this platform's scenario averages; computed after scoring. |

### ScenarioResult

One conversation loaded from the source file for a use case.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `platform_execution_id` | UUID | FK → `platform_executions.id`, `ON DELETE CASCADE`. |
| `scenario_id` | String(128) | Identifier of the scenario / use case tested. |
| `use_case` | String(128) | Human-readable use case label. |
| `source_ref` | String(256) | Reference into the source file: sheet name, conversation key, or row range. |
| `status` | String(32) | Scenario lifecycle status. |
| `screenshot_path` | String(512) | Path to a stored screenshot (binary kept on disk, not in the DB). |
| `average_score` | Float | Mean of this conversation's `turn_score` values. |
| `raw_conversation` | JSON / JSONB | Parsed rows for this conversation from the source file. JSONB on PostgreSQL, JSON on SQLite. `Turn` rows are the evaluation projection derived from it. |

### Turn

One user/model exchange within a conversation, evaluated on its own.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `scenario_result_id` | UUID | FK → `scenario_results.id`, `ON DELETE CASCADE`. |
| `turn_number` | Integer | Order of the turn within the conversation. |
| `prompt` | Text | User message. |
| `response` | Text | Model response. |
| `is_selected` | Boolean | Whether this turn is scored. Opt-in: defaults to `false`, and the worker skips unselected turns in both retrieval and scoring. Set via `PATCH /evaluations/{run_id}/turns/selection` before the run starts. An unselected turn still feeds later turns' judge history. |
| `expected_output` | Text | Ground-truth answer for reference-based metrics (e.g. contextual precision); `NULL` when no reference is available. |
| `retrieved_context_source` | Text | Raw `retrieved_context` cell (ranked source references) captured at ingest; the retrieval pipeline parses/fetches/extracts it into `retrieved_documents`. `NULL` when the sheet had no context column. |
| `response_time_ms` | Integer | Response latency, if available. |
| `turn_score` | Float | Composite score for the turn; mean of its `MetricScore` values. |

The LLM token usage spent scoring the turn lives in a separate `TurnTokenUsage`
entity (below), not a column.

The documents a RAG answer was grounded on are stored as `RetrievedDocument` child
rows (below), not on the turn itself — **populated by the retrieval stage** (not ingest)
from `retrieved_context_source`.

### RetrievedDocument

One document a RAG answer was grounded on, for groundedness metrics. Decoupled from the
turn into its own table — one row per document, ordered within a turn by `rank`
(retriever order). The in-memory `RetrievedContext` (`scorekeeper.core.retrieved_context`) is
assembled from these rows; a turn with no rows has no retrieved context.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `turn_id` | UUID | FK → `turns.id`, `ON DELETE CASCADE` (indexed). |
| `rank` | Float | Retriever order within the turn (integer or float; 0-based); `contextual_precision` relies on it. |
| `name` | Text | Short label/title for the retrieved item. |
| `document` | Text | Source document reference (filename, title, id). |
| `content` | Text | The retrieved text. |
| `url` | Text | Source URL for retrieved web documents; `NULL` otherwise. |

Groundedness metrics evaluate each document's *node text* — its `document` plus
`content` — via `RetrievedContext.node_texts()`. Legacy plain-text spreadsheet cells
still import: `RetrievedContext.from_blob` blank-line-splits them into content-only
documents.

Real source cells hold *source references* (label + URL), not document text. Turning
those into these rows — parse → locate → authorize → fetch → filter → extract →
assemble — is the [retrieval pipeline](retrieval-pipeline.md).

### MetricScore

An LLM-as-a-judge score for a single metric on a single turn. A turn has many.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `turn_id` | UUID | FK → `turns.id`, `ON DELETE CASCADE`. |
| `metric_name` | String(128) | Name of the evaluated metric. |
| `score` | Float | Numeric score for the metric. |
| `judge_model` | String(128) | Model that produced the score, for reproducibility. |
| `rubric_version` | String(64) | Version of the scoring rubric used. |

Its structured trace lives in a separate `MetricTrace` entity (below), not a column.

### MetricTrace

The structured record of what a metric produced for one turn — its own entity,
1:1 with `MetricScore` (`ON DELETE CASCADE`). Replaces the former flattened
`justification` string.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `metric_score_id` | UUID | FK → `metric_scores.id`, `ON DELETE CASCADE`, unique (enforces 1:1). |
| `steps` | JSON (JSONB on PostgreSQL) | The list of steps (each `label`/`summary`/`entries`, entries carrying typed `value`/`justification`/`metadata`). |

### TurnTokenUsage

The LLM token usage spent scoring one turn — its own entity, 1:1 with `Turn`
(`ON DELETE CASCADE`). Counts are **summed across every judge call every metric
made** while scoring the turn, with provider counts normalized to input/output
(Anthropic `input`/`output`, OpenAI `prompt`/`completion`). Kept out of the hot
`turns` row so cost/usage accounting can grow independently. The total is derived
(`input + output`), not stored. See
[Evaluation metrics → Token usage](evaluation-metrics.md#token-usage) for how the
counts are collected.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `turn_id` | UUID | FK → `turns.id`, `ON DELETE CASCADE`, unique (enforces 1:1). |
| `input_tokens` | Integer | Prompt/input tokens summed over the turn's judge calls (`0` when the judge reports none). |
| `output_tokens` | Integer | Completion/output tokens summed over the turn's judge calls (embeddings contribute input only). |

Re-scoring a turn updates the existing row in place, so there is always exactly
one row per turn.

### ScenarioMetric

Which metric applies to which scenario `use_case`. The metric taxonomy lives in
code; this table is the queryable projection of each metric's decorator-declared
scenarios, materialized by `scorekeeper.core.metrics.selection.sync_selection`.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `use_case` | String(128) | Scenario type the metric applies to (indexed). `"default"` is the fallback set used when a `use_case` has no rows. |
| `metric_name` | String(128) | Metric name, validated against the code registry at resolve time (no FK). Unique together with `use_case`. |

### AuthProviderConfig

Per-provider authentication settings for the retrieval pipeline's authorize stage
(`table auth_providers`). A single table with a `provider` discriminator backs the
credential taxonomy in `scorekeeper.core.retrieval.credentials`: one enabled row per gated
`host` supplies the settings its `CredentialProvider` needs to build an authenticated
client. Standalone — no FK into the run hierarchy. The provider's secret (a certificate
private key or an OAuth2 client secret) is stored **encrypted** (Fernet token + per-row salt),
decrypted with `AUTH_ENCRYPTION_KEY` only when a client is built; the other columns hold
non-secret identifiers. Shipped kinds: `sharepoint` (client certificate) and `oauth2`
(client-credentials bearer token).

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `provider` | String(64) | Credential-provider *kind* (`sharepoint`, `oauth2`). Unique together with `host`. |
| `host` | String(256) | Gated host this row authorizes (indexed). Matches a locator host equal to it or a subdomain of it. |
| `enabled` | Boolean | Whether the row is active (default `true`). Disabled rows are ignored. |
| `tenant_id` / `client_id` / `thumbprint` / `site_url` | String | Non-secret identifiers. SharePoint uses all four; `oauth2` uses `client_id`. Nullable per kind. |
| `private_key_encrypted` | Text | The row's encrypted secret (Fernet token) — cert private key or OAuth2 client secret; `NULL` when none stored. |
| `private_key_salt` | Text | Per-row base64 salt used to derive the encryption key. |
| `settings` | JSON / JSONB | Kind-specific non-secret config (e.g. OAuth2 `token_url` / `scope`) and overflow for fields that don't map onto the columns above. |
| `created_at` / `updated_at` | DateTime (tz) | Row timestamps (`updated_at` refreshes on change). |

### DocumentCacheEntry

The retrieval pipeline's fetch-stage download index (`table document_cache`). One row per
distinct source `url` (unique) points at the document's bytes cached on the local filesystem
under `RETRIEVAL_CACHE_DIR`, so a document is downloaded **at most once per platform
execution** (see [Retrieval pipeline § Fetch](retrieval-pipeline.md#fetch)). Standalone — no FK
into the run hierarchy; the bytes live on disk, not in the DB.

**Transient.** Rows and blobs are deleted together when retrieval for a platform execution
finishes ([§ Cache cleanup](retrieval-pipeline.md#cache-cleanup)) — the extracted markdown on
[`RetrievedContextDocument`](#retrieveddocument) is the durable record. A non-empty table
outside a running retrieval phase means a worker died mid-phase; the entries are harmless
(a missing blob is treated as a cache miss) and are reclaimed by the next execution that
fetches the same URL.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key. |
| `url` | Text | Source document URL — the fetch/cache key. Unique. |
| `sha256` | String(64) | Hex digest of the URL; also the cached blob's filename. |
| `cache_path` | Text | Blob path relative to the cache root (sharded by the SHA prefix). |
| `doc_type` | String(16) | `DocType` value of the cached document. |
| `content_type` | String(255) | HTTP `Content-Type` captured on a public fetch; `NULL` for auth'd fetches. |
| `size_bytes` | Integer | Size of the cached blob. |
| `fetched_at` | DateTime (tz) | When the blob was last (re)written (`onupdate` refreshes it). |

## Cascade behavior

The `BenchmarkRun` subtree uses `ON DELETE CASCADE` and SQLAlchemy
`cascade="all, delete-orphan"`, so deleting a `BenchmarkRun` removes its entire
subtree of executions, scenarios, turns, each turn's `TurnTokenUsage`, its scores,
and each score's `MetricTrace`.

The `SourceFile → BenchmarkRun` link uses `ON DELETE SET NULL` instead: deleting
a source file leaves its runs and their results intact, only clearing their
`source_file_id`.
