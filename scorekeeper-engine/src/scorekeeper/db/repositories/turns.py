"""Queries over ``turns``."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from scorekeeper.db.models import MetricScore, Turn


async def _load(db: AsyncSession, turn_id: str, *options) -> Turn | None:
    """Load a ``Turn`` by its string id, or ``None`` for an unknown/invalid id."""
    try:
        key = uuid.UUID(turn_id)
    except ValueError:
        return None
    stmt = select(Turn).where(Turn.id == key).options(*options)
    return (await db.execute(stmt)).scalars().one_or_none()


async def get_turn_with_traces(session: AsyncSession, turn_id: str) -> Turn | None:
    """Load a turn with its metric scores and each score's structured trace."""
    return await _load(
        session, turn_id, selectinload(Turn.metric_scores).selectinload(MetricScore.trace)
    )


async def get_turn_with_token_usage(session: AsyncSession, turn_id: str) -> Turn | None:
    """Load a turn with its judge token usage."""
    return await _load(session, turn_id, selectinload(Turn.token_usage))
