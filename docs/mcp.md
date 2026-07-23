# MCP tools

The MCP server (`scorekeeper.server`, run with `scorekeeper-mcp`) exposes the
scored results over the Model Context Protocol. It reads the same database the
[HTTP API](apis.md) writes; see the [Data model](data-model.md) for the entities
these tools return. This page documents every tool; add new ones as their own `##`
section below.

| Tool       | Purpose |
|------------|---------|
| `retrieve` | Fetch full scored details for the runs matching a set of filters, at a chosen depth. |

## `retrieve`

Return the runs matching the given filters as a **list**, each serialized to the
requested `granularity`. Every filter is optional and combined with AND, so the
same tool answers both "give me this one run in full" and "all `claude` runs scored
in July".

### Parameters

| Name          | Type              | Default             | Description |
|---------------|-------------------|---------------------|-------------|
| `run_id`      | `string \| null`  | `null`              | Narrow to a single run. An unknown or malformed id yields `[]` (never an error). |
| `platform`    | `string \| null`  | `null`              | Exact, **case-sensitive** match on the platform (e.g. `"claude"`, `"copilot"`, `"gemini"`). |
| `start_date`  | `string \| null`  | `null`              | ISO-8601 lower bound (`YYYY-MM-DD` or a full timestamp) on the scoring window. |
| `end_date`    | `string \| null`  | `null`              | ISO-8601 upper bound on the scoring window. |
| `granularity` | `string`          | `scenario_results`  | One of `platform_executions`, `scenario_results`, `metric_scores`. |

### Semantics

The date range filters the **scoring window** — `PlatformExecution.started_at >=
start_date` and `PlatformExecution.finished_at <= end_date`. Those columns stay
`null` until a worker scores the run, so supplying either bound excludes runs still
`en_cola` / `en_proceso`. With no date filter, queued runs are included. Results are
ordered by `BenchmarkRun.created_at`.

### Granularity levels

Granularity controls how deep each run is serialized (each level adds to the one
above):

- `platform_executions` — run fields plus one entry per platform execution
  (`platform`, `average_score`, `started_at`, `finished_at`, `scenarios` count,
  `status_breakdown`).
- `scenario_results` — adds a `scenario_results` list to each platform
  (`scenario_id`, `use_case`, `status`, `average_score`).
- `metric_scores` — adds a `turns` list to each scenario, each turn carrying its
  `turn_id`, `turn_score`, and a `metric_scores` list (`metric_name`, `score`,
  `judge_model`, `rubric_version`). Each score's structured trace is persisted on
  the `metric_traces` table but is not surfaced here — fetch it per turn with
  [`retrieve_turn_traces`](#retrieve_turn_traces) using the `turn_id`.

### Result (`metric_scores`)

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
        "scenarios": 1,
        "status_breakdown": {"completado": 1},
        "scenario_results": [
          {
            "scenario_id": "esc1",
            "use_case": "default",
            "status": "completado",
            "average_score": 0.8,
            "turns": [
              {
                "turn_id": "7c9e…",
                "turn_number": 1,
                "turn_score": 0.8,
                "metric_scores": [
                  {
                    "metric_name": "utilidad",
                    "score": 0.8,
                    "judge_model": "claude-opus-4-8",
                    "rubric_version": "v1"
                  }
                ]
              }
            ]
          }
        ]
      }
    ]
  }
]
```

At `scenario_results` the `turns` key is omitted; at `platform_executions` both
`scenario_results` and `turns` are omitted.

### Errors

A tool error (surfaced from a `ValueError`) is raised when:

| When |
|------|
| `granularity` is not one of the three accepted values. |
| `start_date` / `end_date` is not a valid ISO-8601 date or timestamp. |

No-match filters are **not** errors — they return an empty list `[]`.

## `retrieve_turn_traces`

Fetch the structured metric traces for a single turn — the per-metric reasoning
that `retrieve` omits. Get the `turn_id` (the turn's UUID) from `retrieve` with
`granularity="metric_scores"`.

| Argument     | Type      | Default | Notes |
|--------------|-----------|---------|-------|
| `turn_id`    | `string`  | —       | The turn's UUID. Unknown/invalid → empty list `[]`. |
| `provenance` | `boolean` | `true`  | When `true`, each entry also carries `judge_model` and `rubric_version`; `false` returns the minimal shape. |

Returns one entry per metric on the turn:

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

## Related code

- Tool registration — `src/scorekeeper/server.py` (`retrieve`, `retrieve_turn_traces`)
- Query & serialization — `src/scorekeeper/evaluation.py`
  (`retrieve_runs`, `_serialize_run`, `retrieve_turn_traces`)
- Entities returned — `src/scorekeeper/database.py`, [Data model](data-model.md)
