import logging

import redis.asyncio as redis
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.config import settings
from app.services.storage import StorageError, get_storage

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Pure liveness: 200 whenever the process can handle a request at all.
    No dependency checks -- that's /readyz."""
    return {"status": "ok"}


async def _check_redis() -> bool:
    try:
        async with redis.Redis.from_url(settings.REDIS_URL) as client:
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


@router.get("/readyz")
async def readyz() -> JSONResponse:
    """200 only if Redis and the storage backend are both reachable/usable;
    503 otherwise. Meant for orchestrator readiness probes, not liveness."""
    redis_ok, storage_ok = await _check_redis(), await _check_storage()
    ready = redis_ok and storage_ok
    body = {
        "status": "ok" if ready else "unavailable",
        "checks": {
            "redis": "ok" if redis_ok else "unavailable",
            "storage": "ok" if storage_ok else "unavailable",
        },
    }
    status_code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=status_code, content=body)
