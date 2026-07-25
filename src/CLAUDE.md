# Source layout

Every module under `src/`, folded onto its directory. Extensions are omitted and
`__init__` is left out — each package's `__init__` is a docstring only and
re-exports nothing, so importing a package stays side-effect free. Within a
directory, modules are listed in dependency order rather than alphabetically.

```
src/scorekeeper/            # main, celery_app, tasks
├── api/                    # router
│   └── v1/                 # health, evaluations, captures, runs, scenarios,
├── core/                   # runner, importer, retrieved_context
│   ├── services/           # status, ingestion, retrieval, scoring, runs,
│   ├── metrics/            # base, judge, category, scale, registry, rollup,
│   │   ├── catalog/        # answer_relevance, contextual_precision,
│   │   └── judges/         # base, anthropic_judge, openai_judge,
│   └── retrieval/          # types, protocols, parser, resolver, auth, fetch,
│       └── credentials/    # base, registry, secrets, provider, service
│           └── catalog/    # oauth2, sharepoint
├── db/                     # connection, models
│   └── repositories/       # runs, turns, scenarios, scenario_metrics,
├── config/                 # settings
└── utils/                  # logging_config
```

