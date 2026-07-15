# Scorekeeper

A starter project with an MCP server, PostgreSQL persistence, a read-only results API,
and a React results dashboard.

## Run everything with Docker

```bash
docker compose up --build
```

The services are then available at:

- MCP Streamable HTTP: `http://localhost:8000/mcp`
- Results API and docs: `http://localhost:8001/api/results` and `http://localhost:8001/docs`
- Frontend: `http://localhost:8080`
- PostgreSQL: `localhost:5432`

The starter MCP tools are `record_score(player, points)` and `list_scores(limit)`.

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
