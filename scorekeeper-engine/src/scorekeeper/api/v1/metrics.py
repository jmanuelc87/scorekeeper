"""``/metrics`` — the registered metric catalog a use case is composed from."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from scorekeeper.api.v1.schemas import MetricRead
from scorekeeper.core.services import use_cases as use_case_service

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("", response_model=list[MetricRead])
async def list_metrics() -> list[dict[str, Any]]:
    """List every metric that can be linked to a use case, ordered by name.

    Read straight off the code registry: adding a metric is a code change, only
    composing use cases from them is runtime data.
    """
    return use_case_service.list_metrics()
