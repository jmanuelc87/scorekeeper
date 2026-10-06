"""embedding width 768

Narrow ``retrieved_document_embeddings.embedding`` from ``vector(1536)`` to
``vector(768)``, following ``db.models.EMBEDDING_DIMENSIONS``. 1536 is the width of
OpenAI's ``text-embedding-3-small``; 768 is the width of nomic-embed-text, the model an
OpenAI-compatible local server (LM Studio, via ``openai_base_url``) serves.

**Every chunk row is deleted.** pgvector cannot cast a 1536-dimension vector down to 768,
so the stored vectors cannot come along. The rows are dropped wholesale rather than only
the vectorised ones: chunks are also the document's only *text*, and deleting just the
ones carrying a vector would leave documents with gaps in ``chunk_index`` that the
embedding phase would never refill (it skips a document that still has any chunk).

Nothing is lost permanently — ``retrieved_documents.sentences`` still holds the text, so
re-running the embedding phase rebuilds every chunk. The cost is paying for the
embeddings again.

The HNSW index is dropped and recreated around the type change: it is built over the
column's vector type and cannot survive a change of width.

Revision ID: e9c1a7d43b58
Revises: d4b6e8a02f17
Create Date: 2026-08-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = 'e9c1a7d43b58'
down_revision: Union[str, Sequence[str], None] = 'd4b6e8a02f17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = 'ix_retrieved_document_embeddings_hnsw'
_TABLE = 'retrieved_document_embeddings'


def _rewidth(old: int, new: int) -> None:
    """Drop the index, empty the table, change the width, rebuild the index."""
    op.drop_index(_INDEX, table_name=_TABLE)
    op.execute(f'DELETE FROM {_TABLE}')
    op.alter_column(
        _TABLE,
        'embedding',
        existing_type=Vector(old),
        type_=Vector(new),
        existing_nullable=True,
    )
    op.create_index(
        _INDEX,
        _TABLE,
        ['embedding'],
        postgresql_using='hnsw',
        postgresql_ops={'embedding': 'vector_cosine_ops'},
    )


def upgrade() -> None:
    """Upgrade schema."""
    _rewidth(1536, 1536)


def downgrade() -> None:
    """Downgrade schema."""
    _rewidth(1536, 1536)
