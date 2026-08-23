"""add retrieved document embeddings

Add the ``retrieved_document_embeddings`` table: the chunked, embedded projection of a
retrieved document's text (``scorekeeper.db.models.RetrievedDocumentEmbedding``). A
document is stored whole on ``retrieved_documents.content``; this splits into one row per
chunk so a consumer can retrieve or re-rank at chunk granularity rather than handing a
judge the entire document.

``embedding`` is a fixed ``vector(1536)`` — the width of the default embedder,
``openai_embedding_model`` (``text-embedding-3-small``). Fixed rather than free because
pgvector can only index a column of known width; the cost is that the 768-dimension
``lmstudio_embedding_model`` does not fit.

The HNSW index uses ``vector_cosine_ops``: the default embedder returns normalized
vectors and cosine is the conventional distance for text. HNSW rather than IVFFlat
because it needs no training pass and behaves on an empty table.

Requires the ``vector`` extension, created here. The Compose ``database`` service must
therefore run an image that ships it (``pgvector/pgvector:pg17``); stock
``postgres:17-alpine`` has no ``vector.so``. Like the rest of the chain this targets
PostgreSQL — the test suite builds its schema from the models with ``create_all`` and
never migrates.

Nothing populates the table yet.

Revision ID: c4f9a2b8e106
Revises: b8d1c3f5a207
Create Date: 2026-08-20 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = 'c4f9a2b8e106'
down_revision: Union[str, Sequence[str], None] = 'b8d1c3f5a207'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirrors ``db.models.EMBEDDING_DIMENSIONS``. Changing it means a migration *and* a
# re-embed: pgvector rejects a vector whose width differs from the column's.
_EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    """Upgrade schema."""
    # Must precede the table: the column type does not exist until the extension does.
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')

    op.create_table(
        'retrieved_document_embeddings',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('retrieved_document_id', sa.Uuid(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('embedding', Vector(_EMBEDDING_DIMENSIONS), nullable=False),
        sa.ForeignKeyConstraint(
            ['retrieved_document_id'], ['retrieved_documents.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'retrieved_document_id',
            'chunk_index',
            name='uq_retrieved_document_embeddings_chunk',
        ),
    )
    op.create_index(
        op.f('ix_retrieved_document_embeddings_retrieved_document_id'),
        'retrieved_document_embeddings',
        ['retrieved_document_id'],
    )
    op.create_index(
        'ix_retrieved_document_embeddings_hnsw',
        'retrieved_document_embeddings',
        ['embedding'],
        postgresql_using='hnsw',
        postgresql_ops={'embedding': 'vector_cosine_ops'},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_retrieved_document_embeddings_hnsw',
        table_name='retrieved_document_embeddings',
    )
    op.drop_index(
        op.f('ix_retrieved_document_embeddings_retrieved_document_id'),
        table_name='retrieved_document_embeddings',
    )
    op.drop_table('retrieved_document_embeddings')
    # The ``vector`` extension is deliberately left in place: it is shared infrastructure,
    # and dropping it would break anything else that came to depend on it.
