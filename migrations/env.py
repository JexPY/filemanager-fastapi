"""Alembic runtime environment.

Deliberately decoupled from ``app.config``: the migrate step runs with only
``DATABASE_URL`` meaningfully set (no imgproxy key, no bearer tokens), and
importing ``app.config`` would trigger the full ``Settings()`` validation and
fail. So we read ``DATABASE_URL`` straight from the environment here.

Migrations are hand-written (``op.execute`` with explicit SQL), matching the
app's raw-asyncpg style -- there is no ORM model layer, so ``target_metadata``
stays ``None`` and autogenerate is not used.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None

_DEFAULT_URL = "postgresql://filemanager:filemanager@db:5432/filemanager"


def _database_url() -> str:
    # SQLAlchemy's async engine needs the +asyncpg driver marker; the app itself
    # (raw asyncpg) uses the bare postgresql:// form, so translate it here.
    url = os.environ.get("DATABASE_URL", _DEFAULT_URL)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
