import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from taskiq_fastapi import populate_dependency_context

from app.broker import broker
from app.middleware import RequestIDLogFilter, RequestIDMiddleware
from app.routers import files, health
from app.services.metadata import close_metadata_store, get_metadata_store
from app.services.storage import close_storage

_handler = logging.StreamHandler()
_handler.addFilter(RequestIDLogFilter())
_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
)
logging.basicConfig(level=logging.INFO, handlers=[_handler])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize TaskIQ context for dependencies
    if not broker.is_worker_process:
        await broker.startup()
    populate_dependency_context(broker, app)
    # Build the metadata pool at startup so a bad DATABASE_URL fails fast. The
    # `uploads` schema itself is owned by Alembic and applied by the `migrate`
    # step before this process starts -- not created here.
    store = await get_metadata_store()
    await store.connect()
    yield
    # Release pooled storage + metadata clients held by the web process.
    await close_metadata_store()
    await close_storage()
    if not broker.is_worker_process:
        await broker.shutdown()


app = FastAPI(
    title="Filemanager-Fastapi",
    description="2026 Production-grade distributed media processing microservice",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(RequestIDMiddleware)

app.include_router(files.router)
app.include_router(health.router)
