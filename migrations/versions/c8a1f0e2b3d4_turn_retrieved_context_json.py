"""turn retrieved context json

Convert ``turns.retrieved_context`` from a free-form text blob to a structured
JSON object (``{"documents": [{name, document, content, url}, ...]}``). Existing
blobs are backfilled by blank-line-splitting them into content-only documents.

Revision ID: c8a1f0e2b3d4
Revises: b4e8d2f1a9c7
Create Date: 2026-07-18 00:00:00.000000

"""
from __future__ import annotations

import re
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c8a1f0e2b3d4'
down_revision: Union[str, Sequence[str], None] = 'b4e8d2f1a9c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# JSONB on PostgreSQL, plain JSON on the SQLite fallback (mirrors database.JsonColumn).
_JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
_BLANK_LINE = re.compile(r"\n\s*\n")


def _blob_to_payload(blob: str | None) -> dict[str, Any] | None:
    """Blank-line-split a legacy text blob into the structured document shape."""
    if not blob or not blob.strip():
        return None
    documents = [
        {"name": "", "document": "", "content": block, "url": None}
        for block in (b.strip() for b in _BLANK_LINE.split(blob))
        if block
    ]
    return {"documents": documents} if documents else None


def _payload_to_blob(payload: dict[str, Any] | None) -> str | None:
    """Join a structured context's document contents back into a text blob."""
    if not payload:
        return None
    blocks = [
        doc.get("content", "")
        for doc in payload.get("documents", [])
        if doc.get("content")
    ]
    return "\n\n".join(blocks) or None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    old = sa.table("turns", sa.column("id"), sa.column("retrieved_context", sa.Text()))
    rows = list(bind.execute(sa.select(old.c.id, old.c.retrieved_context)))

    op.add_column("turns", sa.Column("retrieved_context_json", _JSON_TYPE, nullable=True))

    new = sa.table("turns", sa.column("id"), sa.column("retrieved_context_json", _JSON_TYPE))
    for row_id, blob in rows:
        payload = _blob_to_payload(blob)
        if payload is not None:
            bind.execute(
                sa.update(new)
                .where(new.c.id == row_id)
                .values(retrieved_context_json=payload)
            )

    with op.batch_alter_table("turns") as batch:
        batch.drop_column("retrieved_context")
        batch.alter_column("retrieved_context_json", new_column_name="retrieved_context")


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    old = sa.table("turns", sa.column("id"), sa.column("retrieved_context", _JSON_TYPE))
    rows = list(bind.execute(sa.select(old.c.id, old.c.retrieved_context)))

    op.add_column("turns", sa.Column("retrieved_context_text", sa.Text(), nullable=True))

    new = sa.table("turns", sa.column("id"), sa.column("retrieved_context_text", sa.Text()))
    for row_id, payload in rows:
        blob = _payload_to_blob(payload)
        if blob is not None:
            bind.execute(
                sa.update(new)
                .where(new.c.id == row_id)
                .values(retrieved_context_text=blob)
            )

    with op.batch_alter_table("turns") as batch:
        batch.drop_column("retrieved_context")
        batch.alter_column("retrieved_context_text", new_column_name="retrieved_context")
