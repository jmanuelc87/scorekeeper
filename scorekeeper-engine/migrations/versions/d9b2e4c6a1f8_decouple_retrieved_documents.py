"""decouple retrieved documents

Decouple ``turns.retrieved_context`` (a JSON ``{"documents": [...]}`` blob) into its own
``retrieved_documents`` child table — one row per document, ordered by ``rank``. Existing
JSON payloads are backfilled into rows; the JSON column is dropped.

Revision ID: d9b2e4c6a1f8
Revises: c8a1f0e2b3d4
Create Date: 2026-07-18 00:00:00.000000

"""
from __future__ import annotations

import uuid
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd9b2e4c6a1f8'
down_revision: Union[str, Sequence[str], None] = 'c8a1f0e2b3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# JSONB on PostgreSQL, plain JSON on the SQLite fallback (mirrors database.JsonColumn).
_JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    turns = sa.table(
        "turns", sa.column("id", sa.Uuid()), sa.column("retrieved_context", _JSON_TYPE)
    )
    rows = list(bind.execute(sa.select(turns.c.id, turns.c.retrieved_context)))

    op.create_table(
        "retrieved_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Float(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_retrieved_documents_turn_id"), "retrieved_documents", ["turn_id"]
    )

    docs = sa.table(
        "retrieved_documents",
        sa.column("id", sa.Uuid()),
        sa.column("turn_id", sa.Uuid()),
        sa.column("rank", sa.Float()),
        sa.column("name", sa.Text()),
        sa.column("document", sa.Text()),
        sa.column("content", sa.Text()),
        sa.column("url", sa.Text()),
    )
    inserts: list[dict[str, Any]] = []
    for turn_id, payload in rows:
        for rank, doc in enumerate((payload or {}).get("documents", [])):
            inserts.append(
                {
                    "id": uuid.uuid4(),
                    "turn_id": turn_id,
                    "rank": rank,
                    "name": doc.get("name", ""),
                    "document": doc.get("document", ""),
                    "content": doc.get("content", ""),
                    "url": doc.get("url"),
                }
            )
    if inserts:
        bind.execute(sa.insert(docs), inserts)

    with op.batch_alter_table("turns") as batch:
        batch.drop_column("retrieved_context")


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()

    op.add_column("turns", sa.Column("retrieved_context", _JSON_TYPE, nullable=True))

    docs = sa.table(
        "retrieved_documents",
        sa.column("turn_id", sa.Uuid()),
        sa.column("rank", sa.Float()),
        sa.column("name", sa.Text()),
        sa.column("document", sa.Text()),
        sa.column("content", sa.Text()),
        sa.column("url", sa.Text()),
    )
    ordered = sa.select(
        docs.c.turn_id, docs.c.rank, docs.c.name, docs.c.document, docs.c.content, docs.c.url
    ).order_by(docs.c.turn_id, docs.c.rank)

    grouped: dict[Any, list[dict[str, Any]]] = {}
    for turn_id, _rank, name, document, content, url in bind.execute(ordered):
        grouped.setdefault(turn_id, []).append(
            {"name": name, "document": document, "content": content, "url": url}
        )

    turns = sa.table(
        "turns", sa.column("id", sa.Uuid()), sa.column("retrieved_context", _JSON_TYPE)
    )
    for turn_id, documents in grouped.items():
        bind.execute(
            sa.update(turns)
            .where(turns.c.id == turn_id)
            .values(retrieved_context={"documents": documents})
        )

    op.drop_index(op.f("ix_retrieved_documents_turn_id"), table_name="retrieved_documents")
    op.drop_table("retrieved_documents")
