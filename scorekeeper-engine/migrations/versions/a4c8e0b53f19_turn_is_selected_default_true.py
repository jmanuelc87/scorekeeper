"""turn is_selected defaults to true

Flip scoring from opt-in to opt-out. ``b7c9e1f30a25`` added ``turns.is_selected`` defaulting
to false, so an ingested run scored nothing until a client called the selection endpoint;
evaluating everything is the far more common intent, and deselecting the handful of turns you
want to skip is the smaller ask.

Two changes, both needed: the column's ``server_default`` (what ingest relies on — it never
sets the flag explicitly) and the rows already stored, which are backfilled to true so an
existing database behaves like a fresh one.

The backfill is not reversible: which turns were deliberately deselected is not recorded
anywhere, so ``downgrade`` restores the default for *new* rows and leaves the data as it is.
Note also that a run already delivered as ``completado`` will, if it is ever re-delivered,
recompute its scenario score over turns it previously excluded (``services.chain`` averages
the selected turns), and those turns have no score.

Revision ID: a4c8e0b53f19
Revises: 462cfe159cf6
Create Date: 2026-08-18 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4c8e0b53f19'
down_revision: Union[str, Sequence[str], None] = '462cfe159cf6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_turns = sa.table('turns', sa.column('is_selected', sa.Boolean()))


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        'turns',
        'is_selected',
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default='1',
    )
    op.execute(sa.update(_turns).values(is_selected=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Only the default: the backfill cannot be undone, see the module docstring.
    op.alter_column(
        'turns',
        'is_selected',
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default='0',
    )
