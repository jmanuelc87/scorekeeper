# Source layout

Every package under `src/`, with what it is responsible for. Each package's
`__init__` is a docstring only and re-exports nothing, so importing a package
stays side-effect free.

```
src/scorekeeper/            # process entry points: ASGI app, Celery app, tasks
├── api/                    # HTTP layer; aggregates the versioned routers
│   └── v1/                 # one thin module per REST resource
├── core/                   # domain layer: xlsx parsing, the scoring loop
│   ├── services/           # application layer, one module per phase of a run
│   ├── metrics/            # metric taxonomy, scales, judge seam, roll-ups
│   │   ├── catalog/        # the concrete metrics, one module each
│   │   └── judges/         # Judge implementations per LLM provider
│   └── retrieval/          # parse → locate → authorize → fetch → extract
│       └── credentials/    # DB-backed credentials for the authorize stage
│           └── catalog/    # credential providers, one module per backend
├── db/                     # persistence: ORM object graph, engine and session
│   └── repositories/       # the query layer; every select() lives here
├── config/                 # the single Settings source of truth
└── utils/                  # structlog setup shared by every process
```

Three conventions the tree can't show:

- `api/v1` handlers call services **module-qualified**
  (`ingestion.ingest_evaluation`, never a direct function import) so the
  attribute resolves at call time and tests can monkeypatch the service module.
- `db/repositories` functions take `session: AsyncSession` as the first
  positional argument and never commit, roll back, or refresh — that stays in
  the service.
- `metrics/catalog` and `credentials/catalog` populate their registries as an
  import side effect. Every LLM/SDK import in `metrics/judges` and
  `credentials/catalog` is lazy, so the taxonomy imports without the optional
  extras installed.
