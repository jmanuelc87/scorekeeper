"""turn retrieved context source

Add ``turns.retrieved_context_source`` — the raw ``retrieved_context`` cell (ranked source
references) captured at ingest, which the retrieval pipeline later parses/fetches/extracts into
the ``retrieved_documents`` child rows. Nullable; no backfill (existing runs predate retrieval).

Revision ID: a7f3c1d5e9b2
Revises: f4d6b8c0e2a1
Create Date: 2026-07-22 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7f3c1d5e9b2'
down_revision: Union[str, Sequence[str], None] = 'f4d6b8c0e2a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("turns", sa.Column("retrieved_context_source", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("turns") as batch:
        batch.drop_column("retrieved_context_source")
