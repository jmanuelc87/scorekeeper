FROM python:3.13-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Alembic config + migration scripts for `alembic upgrade head` at deploy time.
COPY alembic.ini ./
COPY migrations ./migrations

CMD ["scorekeeper-mcp", "--transport", "http"]
