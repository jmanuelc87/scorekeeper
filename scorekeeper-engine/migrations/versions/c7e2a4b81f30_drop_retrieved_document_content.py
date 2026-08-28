"""drop retrieved document content

Remove ``retrieved_documents.content``, the whole-document markdown a judge used to be
handed in full, and make ``retrieved_document_embeddings.embedding`` nullable so a chunk
can be stored without a vector. A document's text now lives only on its chunks
(``retrieved_document_embeddings``), and scoring renders just the ones closest to the
turn's prompt — see ``scorekeeper.core.services.embedding``.

**Lossy on purpose, and not reversible.** Rows written before segmentation existed carry
``content`` but no ``sentences``, so they have nothing to chunk from; the deliberate
decision is to drop them rather than backfill, and to re-run retrieval for the runs that
need them. The downgrade recreates the column *nullable and empty* — the text is gone.

Revision ID: c7e2a4b81f30
Revises: b3d5f7a91c26
Create Date: 2026-08-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = 'c7e2a4b81f30'
down_revision: Union[str, Sequence[str], None] = 'b3d5f7a91c26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('retrieved_documents', 'content')
    # Chunking and embedding are separable: the chunks are the document's only text and
    # are always written, while the vector is the enrichment that lets scoring narrow
    # them. A deployment with no ``openai_api_key`` stores chunks with a NULL embedding
    # and still hands a judge the whole document.
    op.alter_column(
        'retrieved_document_embeddings',
        'embedding',
        existing_type=Vector(1536),
        nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Nullable, unlike the column it replaces: the original text is not recoverable,
    # so NOT NULL could only be satisfied by inventing empty strings.
    # Rows stored without a vector cannot satisfy NOT NULL, so they go first.
    op.execute(
        'DELETE FROM retrieved_document_embeddings WHERE embedding IS NULL'
    )
    op.alter_column(
        'retrieved_document_embeddings',
        'embedding',
        existing_type=Vector(1536),
        nullable=False,
    )
    op.add_column(
        'retrieved_documents',
        sa.Column('content', sa.Text(), nullable=True),
    )
