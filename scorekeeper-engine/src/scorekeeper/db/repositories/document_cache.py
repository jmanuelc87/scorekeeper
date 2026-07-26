"""Queries over ``document_cache`` — the fetch stage's on-disk blob index."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import DocumentCacheEntry


async def get_by_url(session: AsyncSession, url: str) -> DocumentCacheEntry | None:
    """Load the cache entry for ``url``, or ``None`` when it was never fetched."""
    return await session.scalar(
        select(DocumentCacheEntry).where(DocumentCacheEntry.url == url)
    )
