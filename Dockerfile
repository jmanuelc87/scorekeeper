FROM python:3.13-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src ./src
# Include the `judges` extra so the LLM-as-a-judge SDKs (anthropic/openai) are
# available at runtime — the scoring runner imports them to build a judge.
RUN pip install --no-cache-dir ".[judges,retrieval]"

# Alembic config + migration scripts for `alembic upgrade head` at deploy time.
COPY alembic.ini ./
COPY migrations ./migrations

CMD ["scorekeeper-api"]
