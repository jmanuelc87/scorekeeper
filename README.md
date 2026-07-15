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

## Checks

```bash
uv run pytest
uv run ruff check .
cd frontend && npm run build
```

