import taskiq_fastapi
from taskiq import TaskiqEvents, TaskiqState
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker
from app.config import settings

# Create result backend
result_backend = RedisAsyncResultBackend(
    redis_url=settings.REDIS_URL,
)

# Create broker
broker = RedisStreamBroker(
    url=settings.REDIS_URL,
).with_result_backend(result_backend)

taskiq_fastapi.init(broker, "app.main:app")


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _close_worker_storage(state: TaskiqState) -> None:
    # The worker process reuses pooled storage clients too; release them on shutdown.
    from app.services.storage import close_storage

    await close_storage()
