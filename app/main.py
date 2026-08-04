import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from taskiq_fastapi import populate_dependency_context

from app.routers import files
from app.broker import broker
from app.services.storage import close_storage

logging.basicConfig(level=logging.INFO)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize TaskIQ context for dependencies
    if not broker.is_worker_process:
        await broker.startup()
    populate_dependency_context(broker)
    yield
    # Release pooled storage clients held by the web process.
    await close_storage()
    if not broker.is_worker_process:
        await broker.shutdown()

app = FastAPI(
    title="Filemanager-Fastapi",
    description="2026 Production-grade distributed media processing microservice",
    version="2.0.0",
    lifespan=lifespan
)

app.include_router(files.router)
