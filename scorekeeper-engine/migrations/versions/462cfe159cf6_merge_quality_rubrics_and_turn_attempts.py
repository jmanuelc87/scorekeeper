"""merge quality rubrics and turn attempts

Rejoin the two branches that both descend from the prompt catalog (``c1a5e7d3f0b6``):
``e2d7b4a13c95`` (turn attempts) and ``e2b4f6a8c135`` → ``d7f2b6c1a840`` (the metric-score
scoring key, then the quality rubrics). They touch different tables, so the merge is a
no-op — it exists so ``alembic upgrade head`` resolves to one revision again, which is
what the compose ``migrate`` service runs.

Revision ID: 462cfe159cf6
Revises: d7f2b6c1a840, e2d7b4a13c95
Create Date: 2026-08-18 11:23:05.407950

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = '462cfe159cf6'
down_revision: Union[str, Sequence[str], None] = ('d7f2b6c1a840', 'e2d7b4a13c95')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
