"""``/use-cases`` — name a use case and pick the metrics it is scored with."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from scorekeeper.api.v1.schemas import UseCaseCreate, UseCaseRead, UseCaseUpdate
from scorekeeper.core.services import use_cases as use_case_service

router = APIRouter(prefix="/use-cases", tags=["use-cases"])


# A use case is the unit of metric selection: every uploaded conversation is ingested
# under exactly one, and ``POST /evaluations`` rejects a ``use_case`` created here.

@router.get("", response_model=list[UseCaseRead])
async def list_use_cases() -> list[dict[str, Any]]:
    """List every use case with the metric names linked to it."""
    return await use_case_service.list_use_cases()


@router.post("", response_model=UseCaseRead, status_code=201)
async def create_use_case(body: UseCaseCreate) -> dict[str, Any]:
    """Create a use case scoring the given metrics.

    ``422`` when a metric is not in the registry (see ``GET /metrics``), ``409`` when
    the name is already taken.
    """
    try:
        return await use_case_service.create_use_case(body.name, body.metrics)
    except use_case_service.UseCaseValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except use_case_service.UseCaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/{id}", response_model=UseCaseRead)
async def update_use_case(id: str, body: UseCaseUpdate) -> dict[str, Any]:
    """Update the metrics a use case is scored with.

    ``404`` when the id does not exist, ``409`` when editing ``default``, ``422`` when
    a metric is not in the registry or the set is empty.
    """
    try:
        return await use_case_service.update_use_case_metrics(id, body.metrics)
    except use_case_service.UseCaseValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except use_case_service.UseCaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except use_case_service.UseCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
