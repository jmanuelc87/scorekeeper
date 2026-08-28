"""retrieved document sentences

Add ``retrieved_documents.sentences``: the document's ``content`` segmented by the
extract stage into ``[{page, index, text}, ...]`` — the page-attributed sentences a
later chunker groups into ``retrieved_document_embeddings`` rows
(``scorekeeper.db.models.RetrievedContextDocument.sentences``).

JSONB on PostgreSQL, plain JSON on the SQLite fallback, matching ``JsonColumn``.

Revision ID: b3d5f7a91c26
Revises: e7a1d4c96b03
Create Date: 2026-08-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'b3d5f7a91c26'
down_revision: Union[str, Sequence[str], None] = 'e7a1d4c96b03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable with no backfill: existing rows were extracted before segmentation
    # existed, and NULL says exactly that — re-running retrieval is what fills them.
    op.add_column(
        'retrieved_documents',
        sa.Column(
            'sentences',
            sa.JSON().with_variant(JSONB, 'postgresql'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('retrieved_documents', 'sentences')
