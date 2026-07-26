"""add document cache

Add the ``document_cache`` table indexing documents the retrieval pipeline's fetch stage has
downloaded to the local filesystem (``scorekeeper.retrieval.fetch``), keyed by source ``url``
so a document is fetched at most once. The bytes live on disk under the configured cache
directory; this table only records where and what.

Revision ID: f4d6b8c0e2a1
Revises: e1c3f5a7b9d0
Create Date: 2026-07-22 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4d6b8c0e2a1'
down_revision: Union[str, Sequence[str], None] = 'e1c3f5a7b9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "document_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("cache_path", sa.Text(), nullable=False),
        sa.Column("doc_type", sa.String(length=16), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url", name="uq_document_cache_url"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("document_cache")
