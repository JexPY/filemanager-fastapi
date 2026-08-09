"""add webhook delivery state to uploads

Webhook delivery moved out of the compression task into its own TaskIQ task, and
an exhausted delivery is now persisted (a dead-letter record) so it can be
inspected and manually redelivered instead of only being logged. These columns
hold the outcome of the last delivery cycle for a row's callback_url:

* ``webhook_status``      -- NULL (no callback / never attempted), ``pending``
                             (a delivery task is in flight), ``delivered``, or
                             ``failed`` (all attempts exhausted -> dead-lettered).
* ``webhook_attempts``    -- attempts made in the last cycle.
* ``webhook_last_error``  -- short reason string for the last failed cycle.
* ``webhook_updated_at``  -- when the state last changed.

All nullable / defaulted, so existing rows (and every image row) are unaffected.

Revision ID: 0005_uploads_webhook_state
Revises: 0004_uploads_poster
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_uploads_webhook_state"
down_revision: str | None = "0004_uploads_poster"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE uploads ADD COLUMN webhook_status text")
    op.execute("ALTER TABLE uploads ADD COLUMN webhook_attempts integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE uploads ADD COLUMN webhook_last_error text")
    op.execute("ALTER TABLE uploads ADD COLUMN webhook_updated_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS webhook_updated_at")
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS webhook_last_error")
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS webhook_attempts")
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS webhook_status")
