"""add renditions column to uploads

Materialized image renditions are generated in the same libvips pass as the
upload and stored under derived keys (e.g. `images/<uuid>_t300.webp`).

The `renditions` column (JSONB) stores a map of rendition names to storage keys,
e.g. `{"thumbnail": "images/<uuid>_t300.webp"}`.
NULL means no materialized renditions exist (pre-existing rows fall back to
dynamic imgproxy signing cleanly with no I/O).

Revision ID: 0007_uploads_renditions
Revises: 0006_uploads_visibility
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_uploads_renditions"
down_revision: str | None = "0006_uploads_visibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE uploads ADD COLUMN renditions jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS renditions")
