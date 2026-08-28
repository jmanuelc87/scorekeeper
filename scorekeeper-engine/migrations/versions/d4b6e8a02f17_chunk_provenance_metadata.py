"""chunk provenance metadata

Add ``page``, ``sentence_start`` and ``sentence_end`` to
``retrieved_document_embeddings``: where in its document a chunk came from
(``scorekeeper.db.models.RetrievedDocumentEmbedding``). The page is the chunk's *first*
sentence's — a window of several sentences can straddle a page break and only one page is
recorded. The sentence range is inclusive at both ends and indexes
``retrieved_documents.sentences``.

All three are nullable and **not backfilled**. Chunks written before this revision keep
NULL: the embedding phase skips a document that is already chunked, so filling them in
would mean deleting those rows and paying for the embeddings again — a call for an
operator to make, not for a migration.

Revision ID: d4b6e8a02f17
Revises: c7e2a4b81f30
Create Date: 2026-08-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4b6e8a02f17'
down_revision: Union[str, Sequence[str], None] = 'c7e2a4b81f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'retrieved_document_embeddings',
        sa.Column('page', sa.Integer(), nullable=True),
    )
    op.add_column(
        'retrieved_document_embeddings',
        sa.Column('sentence_start', sa.Integer(), nullable=True),
    )
    op.add_column(
        'retrieved_document_embeddings',
        sa.Column('sentence_end', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('retrieved_document_embeddings', 'sentence_end')
    op.drop_column('retrieved_document_embeddings', 'sentence_start')
    op.drop_column('retrieved_document_embeddings', 'page')
