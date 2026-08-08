"""initial uploads table

Reproduces, byte for byte, the schema the metadata store used to self-create on
first pool use (the old ``_SCHEMA_SQL`` in app/services/metadata.py). This is
revision 0001 of the greenfield table: a from-scratch ``alembic upgrade head``
against an empty database must produce exactly the table the metadata pass ran
against, so nothing about the store's observable behaviour changes -- only who
owns the DDL (migrations now, not the app).

Revision ID: 0001_initial_uploads
Revises:
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial_uploads"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE uploads (
            id                text PRIMARY KEY,
            owner             text NOT NULL,
            kind              text NOT NULL,
            storage_key       text NOT NULL,
            content_type      text NOT NULL,
            size_bytes        bigint NOT NULL,
            width             integer,
            height            integer,
            content_hash      text,
            status            text NOT NULL,
            task_id           text,
            original_filename text,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX uploads_owner_created_idx ON uploads (owner, created_at DESC)")
    op.execute("CREATE INDEX uploads_owner_hash_idx ON uploads (owner, content_hash)")
    op.execute("CREATE UNIQUE INDEX uploads_storage_key_idx ON uploads (storage_key)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS uploads")
