"""add auth providers

Add the ``auth_providers`` table backing the retrieval pipeline's credential taxonomy
(``scorekeeper.retrieval.credentials``): one enabled row per gated host supplies the
settings a credential provider needs to build an authenticated client. The certificate
private key is stored encrypted (Fernet token + per-row salt); the plaintext columns hold
only non-secret identifiers.

Revision ID: e1c3f5a7b9d0
Revises: d9b2e4c6a1f8
Create Date: 2026-07-21 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e1c3f5a7b9d0'
down_revision: Union[str, Sequence[str], None] = 'd9b2e4c6a1f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# JSONB on PostgreSQL, plain JSON on the SQLite fallback (mirrors database.JsonColumn).
_JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "auth_providers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("host", sa.String(length=256), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
        sa.Column("client_id", sa.String(length=128), nullable=True),
        sa.Column("thumbprint", sa.String(length=128), nullable=True),
        sa.Column("site_url", sa.String(length=512), nullable=True),
        sa.Column("private_key_encrypted", sa.Text(), nullable=True),
        sa.Column("private_key_salt", sa.Text(), nullable=True),
        sa.Column("settings", _JSON_TYPE, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "host", name="uq_auth_provider_host"),
    )
    op.create_index(op.f("ix_auth_providers_host"), "auth_providers", ["host"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_auth_providers_host"), table_name="auth_providers")
    op.drop_table("auth_providers")
