from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from scorekeeper.config import get_settings
from scorekeeper.database import create_schema, get_scores

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_schema()
    yield


app = FastAPI(title="Scorekeeper Results API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/results")
def results(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, int | str]]:
    return get_scores(limit)


def main() -> None:
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)

