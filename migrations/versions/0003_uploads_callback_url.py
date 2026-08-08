"""add callback_url to uploads

Holds the optional per-upload webhook target a client passes on
POST /upload/video. The worker POSTs a signed completion payload here when
compression finishes. Nullable -- most uploads have no callback.

Revision ID: 0003_uploads_callback_url
Revises: 0002_video_duration_truncated
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_uploads_callback_url"
down_revision: str | None = "0002_video_duration_truncated"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE uploads ADD COLUMN callback_url text")


def downgrade() -> None:
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS callback_url")
