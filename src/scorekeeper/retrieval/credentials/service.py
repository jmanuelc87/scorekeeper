"""CRUD service for ``auth_providers`` rows (the retrieval credential store).

Thin persistence layer over :class:`~scorekeeper.database.AuthProviderConfig`, following the
codebase's session-injection convention (``session`` defaults to ``SessionLocal()``; tests
inject an in-memory session). The HTTP layer (``scorekeeper.api``) validates input and maps
these functions' results and exceptions onto status codes.

Two invariants live here, not in the API:

* **Secrets never leave.** ``_serialize`` returns only non-secret fields plus a
  ``has_private_key`` flag — the encrypted key material and salt are never emitted. A
  ``private_key`` supplied on create/update is encrypted (per-row salt) before it is stored.
* **Only known provider kinds.** ``provider`` is validated against the credential-provider
  registry, so a row can't be configured for a kind with no implementation.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from scorekeeper.config import get_settings
from scorekeeper.database import AuthProviderConfig, SessionLocal
from scorekeeper.retrieval.credentials.registry import CredentialProviderRegistry
from scorekeeper.retrieval.credentials.secrets import encrypt_secret

# Columns a client may set directly. ``private_key`` is handled separately (encrypted);
# id / timestamps / encrypted-key columns are server-owned and never mass-assigned.
_WRITABLE_FIELDS = (
    "provider",
    "host",
    "enabled",
    "tenant_id",
    "client_id",
    "thumbprint",
    "site_url",
    "settings",
)


class ProviderError(Exception):
    """Base class for credential-store service errors."""


class ProviderValidationError(ProviderError):
    """Invalid input (unknown provider kind, missing encryption key). → HTTP 422."""


class ProviderConflictError(ProviderError):
    """A row for this ``(provider, host)`` already exists. → HTTP 409."""


def _serialize(row: AuthProviderConfig) -> dict[str, Any]:
    """Project a row into its safe read view — no secret material."""
    return {
        "id": str(row.id),
        "provider": row.provider,
        "host": row.host,
        "enabled": row.enabled,
        "tenant_id": row.tenant_id,
        "client_id": row.client_id,
        "thumbprint": row.thumbprint,
        "site_url": row.site_url,
        "has_private_key": bool(row.private_key_encrypted),
        "settings": row.settings,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _validate_kind(provider: str) -> None:
    """Reject a ``provider`` kind that has no registered credential provider."""
    try:
        CredentialProviderRegistry.get(provider)
    except KeyError as exc:
        known = ", ".join(sorted(p.kind for p in CredentialProviderRegistry.all()))
        raise ProviderValidationError(
            f"Proveedor desconocido: {provider!r}. Disponibles: {known or '(ninguno)'}"
        ) from exc


def _apply_private_key(
    row: AuthProviderConfig, private_key: str, encryption_key: str | None
) -> None:
    """Encrypt ``private_key`` (per-row salt) and store the token + salt on ``row``."""
    key = encryption_key if encryption_key is not None else get_settings().auth_encryption_key
    if not key:
        raise ProviderValidationError(
            "AUTH_ENCRYPTION_KEY no está configurado; no se puede almacenar la clave privada"
        )
    salt, token = encrypt_secret(private_key, key)
    row.private_key_salt = salt
    row.private_key_encrypted = token


def list_providers(
    *,
    provider: str | None = None,
    host: str | None = None,
    enabled: bool | None = None,
    session: Session | None = None,
) -> list[dict[str, Any]]:
    """List provider rows, optionally filtered, ordered by ``(provider, host)``."""
    owns_session = session is None
    db = SessionLocal() if session is None else session
    try:
        stmt = select(AuthProviderConfig)
        if provider is not None:
            stmt = stmt.where(AuthProviderConfig.provider == provider)
        if host is not None:
            stmt = stmt.where(AuthProviderConfig.host == host)
        if enabled is not None:
            stmt = stmt.where(AuthProviderConfig.enabled.is_(enabled))
        stmt = stmt.order_by(AuthProviderConfig.provider, AuthProviderConfig.host)
        return [_serialize(row) for row in db.scalars(stmt)]
    finally:
        if owns_session:
            db.close()


def get_provider(
    provider_id: UUID, *, session: Session | None = None
) -> dict[str, Any] | None:
    """Return one provider's safe view, or ``None`` when the id is unknown."""
    owns_session = session is None
    db = SessionLocal() if session is None else session
    try:
        row = db.get(AuthProviderConfig, provider_id)
        return _serialize(row) if row is not None else None
    finally:
        if owns_session:
            db.close()


def create_provider(
    data: dict[str, Any],
    *,
    session: Session | None = None,
    encryption_key: str | None = None,
) -> dict[str, Any]:
    """Create a provider row from validated ``data``.

    ``data`` carries the writable columns plus an optional ``private_key`` (encrypted before
    storage). Raises :class:`ProviderValidationError` for an unknown kind / missing encryption
    key and :class:`ProviderConflictError` on a duplicate ``(provider, host)``.
    """
    values = dict(data)
    private_key = values.pop("private_key", None)
    _validate_kind(values.get("provider", ""))

    owns_session = session is None
    db = SessionLocal() if session is None else session
    try:
        row = AuthProviderConfig(
            **{k: v for k, v in values.items() if k in _WRITABLE_FIELDS}
        )
        if private_key:
            _apply_private_key(row, private_key, encryption_key)
        db.add(row)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ProviderConflictError(
                f"Ya existe un proveedor {row.provider!r} para el host {row.host!r}"
            ) from exc
        db.refresh(row)
        return _serialize(row)
    finally:
        if owns_session:
            db.close()


def update_provider(
    provider_id: UUID,
    changes: dict[str, Any],
    *,
    session: Session | None = None,
    encryption_key: str | None = None,
) -> dict[str, Any] | None:
    """Partially update a provider row; returns its new view, or ``None`` if unknown.

    Only keys present in ``changes`` are touched. A ``private_key`` key re-encrypts the stored
    key (or clears it when falsy), so rotating a certificate never exposes the old material.
    """
    changes = dict(changes)
    rotate_key = "private_key" in changes
    private_key = changes.pop("private_key", None)
    if "provider" in changes and changes["provider"] is not None:
        _validate_kind(changes["provider"])

    owns_session = session is None
    db = SessionLocal() if session is None else session
    try:
        row = db.get(AuthProviderConfig, provider_id)
        if row is None:
            return None
        for field, value in changes.items():
            if field in _WRITABLE_FIELDS:
                setattr(row, field, value)
        if rotate_key:
            if private_key:
                _apply_private_key(row, private_key, encryption_key)
            else:
                row.private_key_encrypted = None
                row.private_key_salt = None
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ProviderConflictError(
                f"Ya existe un proveedor {row.provider!r} para el host {row.host!r}"
            ) from exc
        db.refresh(row)
        return _serialize(row)
    finally:
        if owns_session:
            db.close()


def delete_provider(provider_id: UUID, *, session: Session | None = None) -> bool:
    """Delete a provider row. Returns ``True`` when a row was removed, ``False`` if unknown."""
    owns_session = session is None
    db = SessionLocal() if session is None else session
    try:
        row = db.get(AuthProviderConfig, provider_id)
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True
    finally:
        if owns_session:
            db.close()
