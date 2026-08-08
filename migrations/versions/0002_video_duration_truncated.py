"""add duration_seconds + truncated to uploads

The video pipeline caps output duration (ffmpeg `-t`), silently truncating
longer inputs. The worker now ffprobes the input duration and records whether
truncation happened; these two columns let `GET /files` reflect it. Nullable /
defaulted so existing image rows (and pre-existing video rows) are unaffected:
images stay `duration_seconds NULL, truncated false`.

Revision ID: 0002_video_duration_truncated
Revises: 0001_initial_uploads
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_video_duration_truncated"
down_revision: str | None = "0001_initial_uploads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE uploads ADD COLUMN duration_seconds double precision")
    op.execute("ALTER TABLE uploads ADD COLUMN truncated boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS truncated")
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS duration_seconds")
