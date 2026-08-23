"""benchmark run label

Add ``benchmark_runs.label``: the batch a capturing client groups its conversations
under (``scorekeeper.db.models.BenchmarkRun.label``). A ``POST /captures`` carrying a
``run_label`` joins the newest still-unstarted run holding that label, so captures of
*different* scenarios land under one run instead of one run each.

Indexed because it is looked up by value on every labelled capture, and non-unique
because a label is reusable once the run it named has started.

Revision ID: e7a1d4c96b03
Revises: c4f9a2b8e106
Create Date: 2026-08-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7a1d4c96b03'
down_revision: Union[str, Sequence[str], None] = 'c4f9a2b8e106'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable with no server default: every pre-existing run was ingested without a
    # batch, and "no grouping requested" is exactly what NULL means here.
    op.add_column(
        'benchmark_runs',
        sa.Column('label', sa.String(length=128), nullable=True),
    )
    op.create_index(
        op.f('ix_benchmark_runs_label'), 'benchmark_runs', ['label'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_benchmark_runs_label'), table_name='benchmark_runs')
    op.drop_column('benchmark_runs', 'label')
