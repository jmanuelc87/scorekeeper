"""The ASGI application: builds the app and mounts the versioned API.

The database schema is owned by Alembic — run ``alembic upgrade head`` before
starting the app (the compose ``migrate`` service does this).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from scorekeeper.api.router import api_router
from scorekeeper.api.v1 import health
from scorekeeper.config.settings import get_settings
from scorekeeper.db.connection import engine
from scorekeeper.utils.logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Drain the asyncpg connection pool on shutdown."""
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    # Plain-text logging to stdout. uvicorn only configures its own loggers, so
    # without this our app events would be swallowed.
    configure_logging(settings.log_level)

    app = FastAPI(title="Scorekeeper Results API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )
    # Liveness stays unversioned: a probe should not have to follow the API's
    # version bumps. It is also reachable at /api/v1/health via api_router.
    app.include_router(health.router)
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
