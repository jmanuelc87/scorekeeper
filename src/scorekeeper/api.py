import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from scorekeeper.config import get_settings

settings = get_settings()

# The database schema is owned by Alembic — run `alembic upgrade head`
# before starting the app (the compose `migrate` service does this).
app = FastAPI(title="Scorekeeper Results API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)

