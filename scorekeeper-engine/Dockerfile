FROM python:3.13-slim

# uv resolves and installs the dependency tree; copied from its official image so
# there is no bootstrap install step.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
# uv sync installs into /app/.venv; putting it first on PATH is what makes the
# console scripts (scorekeeper-api, alembic, celery) resolve without activation.
ENV PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
COPY src ./src
# --locked pins every version to uv.lock and fails the build if the lock has drifted
# from pyproject.toml. The `judges` extra brings the LLM-as-a-judge SDKs
# (anthropic/openai) that the scoring runner imports to build a judge.
RUN uv sync --locked --no-cache --no-dev --extra judges --extra retrieval

# Alembic config + migration scripts for `alembic upgrade head` at deploy time.
COPY alembic.ini ./
COPY migrations ./migrations

CMD ["scorekeeper-api"]
