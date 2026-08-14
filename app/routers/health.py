import asyncio
import logging

import redis.asyncio as redis
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.config import settings
from app.schemas import HealthResponse, ReadinessResponse
from app.services.metadata import MetadataError, get_metadata_store
from app.services.storage import StorageError, get_storage

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz", tags=["System"], summary="Liveness probe", response_model=HealthResponse)
async def healthz() -> dict[str, str]:
    """Pure liveness: 200 whenever the process can handle a request at all.
    No dependency checks -- that's /readyz."""
    return {"status": "ok"}


_redis_pool: redis.Redis | None = None


async def _get_redis() -> redis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis.Redis.from_url(settings.REDIS_URL)
    return _redis_pool


async def close_redis() -> None:
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.aclose()
        _redis_pool = None


async def _check_redis() -> bool:
    try:
        client = await _get_redis()
        return bool(await client.ping())
    except Exception:
        logger.warning("readyz: redis check failed", exc_info=True)
        return False


async def _check_storage() -> bool:
    try:
        await get_storage()
        return True
    except StorageError:
        logger.warning("readyz: storage check failed", exc_info=True)
        return False


async def _check_db() -> bool:
    try:
        store = await get_metadata_store()
        return await store.ping()
    except MetadataError:
        logger.warning("readyz: metadata store check failed", exc_info=True)
        return False


@router.get(
    "/readyz",
    tags=["System"],
    summary="Readiness probe",
    response_model=ReadinessResponse,
    responses={
        503: {"model": ReadinessResponse, "description": "One or more dependencies unavailable"}
    },
)
async def readyz() -> JSONResponse:
    """200 only if Redis, the storage backend, and the metadata store are all
    reachable/usable; 503 otherwise. Meant for orchestrator readiness probes,
    not liveness."""
    redis_ok, storage_ok, db_ok = await asyncio.gather(
        _check_redis(), _check_storage(), _check_db()
    )
    ready = redis_ok and storage_ok and db_ok
    body = {
        "status": "ok" if ready else "unavailable",
        "checks": {
            "redis": "ok" if redis_ok else "unavailable",
            "storage": "ok" if storage_ok else "unavailable",
            "db": "ok" if db_ok else "unavailable",
        },
    }
    status_code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=status_code, content=body)
