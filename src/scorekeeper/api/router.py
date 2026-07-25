"""Aggregates the v1 routers into the router the app mounts.

No prefix is applied here — :mod:`scorekeeper.main` mounts this whole router
under ``/api/v1``, so introducing a v2 is one more ``include_router`` there.
"""

from fastapi import APIRouter

from scorekeeper.api.v1 import (
    auth_providers,
    captures,
    evaluations,
    health,
    runs,
    scenarios,
    turns,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(evaluations.router)
api_router.include_router(captures.router)
api_router.include_router(runs.router)
api_router.include_router(scenarios.router)
api_router.include_router(turns.router)
api_router.include_router(auth_providers.router)
