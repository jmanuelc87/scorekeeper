"""``/auth-providers`` — CRUD over the retrieval pipeline's credential store."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from scorekeeper.api.v1.schemas import (
    AuthProviderCreate,
    AuthProviderRead,
    AuthProviderUpdate,
)
from scorekeeper.core.retrieval.credentials import service as auth_providers
from scorekeeper.core.retrieval.credentials.service import (
    ProviderConflictError,
    ProviderValidationError,
)

router = APIRouter(prefix="/auth-providers", tags=["auth-providers"])


# Manage the retrieval pipeline's credential store (the ``auth_providers`` table). The
# certificate ``private_key`` is **write-only**: it is accepted on create/update, stored
# encrypted, and never returned — reads expose only ``has_private_key``. These endpoints
# manage secrets and carry no built-in auth, so restrict them at the network/deployment layer.

@router.get("", response_model=list[AuthProviderRead])
async def list_auth_providers(
    provider: str | None = Query(None, description="Filtra por tipo de proveedor."),
    host: str | None = Query(None, description="Coincidencia exacta de host."),
    enabled: bool | None = Query(None, description="Filtra por estado habilitado."),
) -> list[dict[str, Any]]:
    """List configured credential providers (filters optional, AND-combined)."""
    return await auth_providers.list_providers(provider=provider, host=host, enabled=enabled)


@router.post("", response_model=AuthProviderRead, status_code=201)
async def create_auth_provider(body: AuthProviderCreate) -> dict[str, Any]:
    """Create a credential provider row. ``409`` on a duplicate ``(provider, host)``."""
    try:
        return await auth_providers.create_provider(body.model_dump())
    except ProviderValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{provider_id}", response_model=AuthProviderRead)
async def get_auth_provider(provider_id: UUID) -> dict[str, Any]:
    """Return one credential provider. ``404`` when the id is unknown."""
    row = await auth_providers.get_provider(provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"El proveedor {provider_id} no existe.")
    return row


@router.patch("/{provider_id}", response_model=AuthProviderRead)
async def update_auth_provider(provider_id: UUID, body: AuthProviderUpdate) -> dict[str, Any]:
    """Partially update a credential provider. ``404`` unknown; ``409`` on a duplicate key."""
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No hay campos para actualizar.")
    try:
        row = await auth_providers.update_provider(provider_id, changes)
    except ProviderValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"El proveedor {provider_id} no existe.")
    return row


@router.delete("/{provider_id}", status_code=204)
async def delete_auth_provider(provider_id: UUID) -> None:
    """Delete a credential provider. ``404`` when the id is unknown."""
    if not await auth_providers.delete_provider(provider_id):
        raise HTTPException(status_code=404, detail=f"El proveedor {provider_id} no existe.")
