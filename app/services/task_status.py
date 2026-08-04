"""Tracks whether a task id was ever actually issued.

TaskIQ's result backend can only tell us "result ready" or "not ready" --
both a genuinely still-running task and a bogus/typo'd task id look
identical (not ready) from that alone. This keeps a short-lived marker, set
when a video task is enqueued, so GET /tasks/{id} can tell the two apart.

Unlike the pooled storage clients, this opens a fresh connection per call
rather than keeping a process-wide singleton: it's called at most once per
upload and once per status poll (not once per request on a hot path), so
the connection overhead is negligible, and it avoids tying a long-lived
client to whichever event loop happened to be running when it was built.
"""

from __future__ import annotations

import redis.asyncio as redis

from app.config import settings

_ISSUED_KEY_PREFIX = "task:issued:"


async def mark_task_issued(task_id: str) -> None:
    async with redis.Redis.from_url(settings.REDIS_URL) as client:
        await client.set(f"{_ISSUED_KEY_PREFIX}{task_id}", "1", ex=settings.TASK_STATUS_TTL_SECONDS)


async def was_task_issued(task_id: str) -> bool:
    async with redis.Redis.from_url(settings.REDIS_URL) as client:
        return bool(await client.exists(f"{_ISSUED_KEY_PREFIX}{task_id}"))
