"""add dominant_color and blur_data_url columns to uploads

LQIP placeholders for ready images:
- dominant_color: average colour as 7-char hex string, e.g. '#1e293b'
- blur_data_url: 16px WebP encoded as 'data:image/webp;base64,...' URI

Both columns are nullable, unindexed, with no default and no backfill.
Existing rows retain NULL for both fields.

Revision ID: 0008_uploads_blur_and_color
Revises: 0007_uploads_renditions
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_uploads_blur_and_color"
down_revision: str | None = "0007_uploads_renditions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE uploads ADD COLUMN dominant_color varchar(7)")
    op.execute("ALTER TABLE uploads ADD COLUMN blur_data_url text")


def downgrade() -> None:
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS dominant_color")
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS blur_data_url")
