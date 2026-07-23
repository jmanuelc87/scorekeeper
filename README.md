# Scorekeeper

Scorekeeper is an mcp server that benchmarks AI assistant platforms (Copilot, Gemini, Claude) by loading each conversation (user and model interactions) from a `.xlsx` file, storing every turn, and scoring each turn with an LLM-as-a-judge. Per-turn metric scores roll up into scenario- and platform-level averages. All scenarios, prompts, and evaluation outputs are in spanish.

## Run everything with Docker

```bash
docker compose up --build
```

The services are then available at:

- MCP Streamable HTTP: `http://localhost:8000/mcp`
- Frontend: `http://localhost:8080`
- PostgreSQL: `localhost:5432`

## Run the MCP server locally

Install dependencies:

```bash
uv sync --extra dev
```

Run over stdio (the default):

```bash
uv run scorekeeper-mcp --transport stdio
```

Run over Streamable HTTP:

```bash
uv run scorekeeper-mcp --transport http
```

Transport can also be selected with `MCP_TRANSPORT=stdio|http`. Without a
`DATABASE_URL`, local commands use `scorekeeper.db` through SQLite. Copy
`.env.example` to `.env` to use the Compose PostgreSQL instance from the host.

### Judge providers

Scoring uses an LLM-as-a-judge selected by `JUDGE_PROVIDER` (`anthropic`, `openai`,
or `lmstudio`); install the SDKs with `uv sync --extra judges`. For end-to-end
testing with no API key or external cost, set `JUDGE_PROVIDER=lmstudio` and point
`LMSTUDIO_BASE_URL` at a running [LM Studio](https://lmstudio.ai/) server (default
`http://localhost:1234/v1`). The local judge remaps every requested/pinned model to
the loaded model named by `LMSTUDIO_JUDGE_MODEL`, so every metric runs against
whatever LM Studio has loaded; `answer_relevance` additionally needs an embedding
model loaded (`LMSTUDIO_EMBEDDING_MODEL`). See `.env.example` for all knobs.

### Tools

- `retrieve` — fetch full scored details for the runs matching a set of filters
  (`run_id`, `platform`, a `start_date`/`end_date` scoring-window range), at a
  chosen `granularity` (`platform_executions`, `scenario_results`, or
  `metric_scores`). See [docs/mcp.md](docs/mcp.md).

## Database migrations

The schema is owned by [Alembic](https://alembic.sqlalchemy.org/), not by the
application — the services no longer create tables on startup. Under Docker, the
one-shot `migrate` service runs `alembic upgrade head` before `mcp` and `api`
start, so the schema is always current.

Run migrations manually against the database named by `DATABASE_URL`:

```bash
uv run alembic upgrade head      # apply all pending migrations
uv run alembic downgrade -1      # roll back the most recent migration
uv run alembic current           # show the applied revision
```

After changing the models in `src/scorekeeper/database.py`, autogenerate a new
migration and apply it:

```bash
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
```

Autogenerate compares the models against a live database, so point
`DATABASE_URL` at PostgreSQL when generating migrations to render native types
(`JSONB`, `uuid`) correctly. Always review the generated script before committing.

## Develop the frontend

```bash
cd frontend
npm install
npm run dev
```

Vite runs at `http://localhost:5173` and reads `VITE_API_URL` when provided.

## Browser extension

`extension/` is a Manifest V3 Chrome extension that captures the conversation open
in Copilot, Gemini or Claude and posts it to `POST /captures` — the live-session
alternative to uploading a conversation `.xlsx`. No build step: load the folder
unpacked from `chrome://extensions` with **Developer mode** on. See
[extension/README.md](extension/README.md) for the adapters it supports and how to
update their selectors when a vendor reskins its chat.

## Checks

```bash
uv run pytest
uv run ruff check .
cd frontend && npm run build
```

