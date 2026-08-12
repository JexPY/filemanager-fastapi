"""remove is_linked column from uploads

Revision ID: 0008_remove_is_linked
Revises: 0007_uploads_is_linked
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_remove_is_linked"
down_revision: str | None = "0007_uploads_is_linked"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("uploads", "is_linked")


def downgrade() -> None:
    op.add_column(
        "uploads",
        sa.Column("is_linked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
