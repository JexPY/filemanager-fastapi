"""add is_linked column and index to uploads

Revision ID: 0007_uploads_is_linked
Revises: 0006_uploads_visibility
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_uploads_is_linked"
down_revision: str | None = "0006_uploads_visibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE uploads ADD COLUMN is_linked boolean NOT NULL DEFAULT false")
    op.execute("CREATE INDEX uploads_unlinked_created_idx ON uploads (is_linked, created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uploads_unlinked_created_idx")
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS is_linked")
