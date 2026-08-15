"""add poster_upload_id to uploads

Video posters are their own image `uploads` record (produced by the worker via
ffmpeg frame-extract -> the existing pyvips validate/strip -> WebP). A video row
points at its poster through this column; the poster image row is otherwise a
normal image record. Nullable -- most video rows have no poster (posters are
generated on request), and image rows never do.

Revision ID: 0004_uploads_poster
Revises: 0003_uploads_callback_url
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_uploads_poster"
down_revision: str | None = "0003_uploads_callback_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE uploads ADD COLUMN poster_upload_id text")


def downgrade() -> None:
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS poster_upload_id")
