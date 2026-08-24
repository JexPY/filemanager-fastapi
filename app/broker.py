from typing import Any

import taskiq_fastapi
from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from app.config import settings

# Create result backend.
#
# `result_ex_time` is not optional in practice: without it taskiq-redis falls
# through to a plain SET, so every task result is retained forever. See the note
# on TASKIQ_RESULT_TTL_SECONDS in config.py for why unbounded, non-expiring keys
# are specifically dangerous on a Redis instance shared with another service.
result_backend: RedisAsyncResultBackend[Any] = RedisAsyncResultBackend(
    redis_url=settings.REDIS_URL,
    result_ex_time=settings.TASKIQ_RESULT_TTL_SECONDS,
)

# Create broker.
#
# `maxlen` bounds the stream. taskiq acknowledges with XACK, which clears the
# pending-entries list but does NOT delete the entry, and it never issues XDEL —
# so without a trim the stream grows by every job ever processed.
broker = RedisStreamBroker(
    url=settings.REDIS_URL,
    maxlen=settings.TASKIQ_STREAM_MAXLEN,
).with_result_backend(result_backend)

scheduler = TaskiqScheduler(broker, [LabelScheduleSource(broker)])

taskiq_fastapi.init(broker, "app.main:app")


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _close_worker_resources(state: TaskiqState) -> None:
    # The worker process holds its own pooled storage clients and metadata pool
    # (built lazily on first task); release both on shutdown.
    from app.services.metadata import close_metadata_store
    from app.services.storage import close_storage

    await close_storage()
    await close_metadata_store()
