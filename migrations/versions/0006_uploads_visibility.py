"""add visibility + share_token to uploads

Playback visibility is now owner-controlled. Two columns drive it:

* ``visibility``   -- ``private`` (default; only the owner can fetch via the
                      authenticated ``/files/{id}/download`` endpoint) or
                      ``public`` (anyone with the stable link, no token).
* ``share_token``  -- a high-entropy opaque capability (``secrets.token_urlsafe``)
                      for an unlisted, revocable share link served at
                      ``/share/{token}``. NULL means no share link. It is a
                      secret and is never returned by ``to_public()``.

``visibility`` is NOT NULL DEFAULT 'private' so every existing row (and every
image row) becomes private without a data migration. ``share_token`` is nullable
with a UNIQUE index: Postgres treats NULLs as distinct, so unlimited rows may
have no token while a minted token is guaranteed unique (its uniqueness is what
makes ``get_by_share_token`` an unambiguous grant).

Revision ID: 0006_uploads_visibility
Revises: 0005_uploads_webhook_state
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_uploads_visibility"
down_revision: str | None = "0005_uploads_webhook_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE uploads ADD COLUMN visibility text NOT NULL DEFAULT 'private'")
    op.execute("ALTER TABLE uploads ADD COLUMN share_token text")
    op.execute("CREATE UNIQUE INDEX uq_uploads_share_token ON uploads (share_token)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_uploads_share_token")
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS share_token")
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS visibility")
