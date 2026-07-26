"""Queries over ``auth_providers``."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import AuthProviderConfig


async def get(session: AsyncSession, provider_id: UUID) -> AuthProviderConfig | None:
    """Load one provider row by primary key, or ``None`` when the id is unknown."""
    return await session.get(AuthProviderConfig, provider_id)


async def list_providers(
    session: AsyncSession,
    *,
    provider: str | None = None,
    host: str | None = None,
    enabled: bool | None = None,
) -> list[AuthProviderConfig]:
    """Provider rows matching every given filter, ordered by ``(provider, host)``."""
    stmt = select(AuthProviderConfig)
    if provider is not None:
        stmt = stmt.where(AuthProviderConfig.provider == provider)
    if host is not None:
        stmt = stmt.where(AuthProviderConfig.host == host)
    if enabled is not None:
        stmt = stmt.where(AuthProviderConfig.enabled.is_(enabled))
    stmt = stmt.order_by(AuthProviderConfig.provider, AuthProviderConfig.host)
    return list(await session.scalars(stmt))


async def list_enabled(session: AsyncSession) -> list[AuthProviderConfig]:
    """Every enabled provider row."""
    return list(
        await session.scalars(
            select(AuthProviderConfig).where(AuthProviderConfig.enabled.is_(True))
        )
    )
