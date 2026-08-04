"""Storage abstraction for FF.

One `StorageBackend` implementation per target (local FS, S3/R2, GCS) selected once
from ``settings.STORAGE_BACKEND`` and reused for the lifetime of the process. Backends
own their network clients so connection pools / TLS sessions are established once rather
than per request. The same singleton serves both the web process and the TaskIQ worker,
so lifecycle is driven by ``close_storage()`` (wired into FastAPI's lifespan and the
worker's shutdown event) instead of ``app.state``.
"""

from __future__ import annotations

import asyncio
import io
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import aioboto3
import aiofiles
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from gcloud.aio.storage import Storage

from app.config import settings


class StorageError(Exception):
    """Backend-agnostic storage failure. Never carries provider internals to callers."""


@dataclass(frozen=True)
class StorageObject:
    """Result of a successful upload."""

    key: str
    url: str
    size: int
    content_type: str


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------


class StorageBackend(ABC):
    @abstractmethod
    async def upload(self, data: bytes, key: str, content_type: str) -> StorageObject: ...

    @abstractmethod
    async def download(self, key: str) -> bytes: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    async def aclose(self) -> None:  # noqa: B027 -- intentional no-op default, not abstract
        """Release any long-lived clients. Default is a no-op."""


# ---------------------------------------------------------------------------
# Local filesystem backend
# ---------------------------------------------------------------------------


class LocalStorage(StorageBackend):
    def __init__(self, root: str, public_base_url: str) -> None:
        self._root = Path(root).resolve()
        self._base = public_base_url.rstrip("/")

    def _resolve(self, key: str) -> Path:
        # Guard against path traversal escaping the storage root.
        target = (self._root / key).resolve()
        if target != self._root and self._root not in target.parents:
            raise StorageError(f"Invalid object key outside storage root: {key!r}")
        return target

    async def upload(self, data: bytes, key: str, content_type: str) -> StorageObject:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(target, "wb") as f:
            await f.write(data)
        url = f"{self._base}/{key}" if self._base else key
        return StorageObject(key=key, url=url, size=len(data), content_type=content_type)

    async def download(self, key: str) -> bytes:
        target = self._resolve(key)
        try:
            async with aiofiles.open(target, "rb") as f:
                return await f.read()
        except FileNotFoundError as exc:
            raise StorageError(f"Object not found: {key!r}") from exc

    async def delete(self, key: str) -> None:
        # Idempotent: deleting a missing object is not an error.
        self._resolve(key).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# S3 / R2 / MinIO backend (aioboto3)
# ---------------------------------------------------------------------------


class S3Storage(StorageBackend):
    def __init__(self) -> None:
        if not settings.S3_BUCKET:
            raise StorageError("S3_BUCKET must be set for the 's3' storage backend")
        self._bucket = settings.S3_BUCKET
        self._endpoint = settings.S3_ENDPOINT_URL or None
        self._public_base = settings.S3_PUBLIC_BASE_URL.rstrip("/")
        self._region = settings.AWS_REGION or None
        # Empty strings suppress boto's default credential chain (IAM roles, env, ...),
        # so pass None when unset to let that chain resolve.
        self._session = aioboto3.Session(
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
            region_name=self._region,
        )
        self._config = BotoConfig(
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        self._stack: AsyncExitStack | None = None
        self._client = None
        self._lock = asyncio.Lock()

    async def _get_client(self):
        # Enter the client's async context once and keep it for connection reuse.
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    stack = AsyncExitStack()
                    client = await stack.enter_async_context(
                        self._session.client("s3", endpoint_url=self._endpoint, config=self._config)
                    )
                    self._client, self._stack = client, stack
        return self._client

    def _object_url(self, key: str) -> str:
        # Explicit CDN / public base wins (CloudFront, custom domain).
        if self._public_base:
            return f"{self._public_base}/{key}"
        # R2 / MinIO expose a path-style endpoint.
        if self._endpoint:
            return f"{self._endpoint.rstrip('/')}/{self._bucket}/{key}"
        # Real AWS: virtual-hosted style.
        host = f"s3.{self._region}.amazonaws.com" if self._region else "s3.amazonaws.com"
        return f"https://{self._bucket}.{host}/{key}"

    async def upload(self, data: bytes, key: str, content_type: str) -> StorageObject:
        client = await self._get_client()
        try:
            await client.upload_fileobj(
                io.BytesIO(data),
                self._bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 upload failed for {key!r}") from exc
        return StorageObject(
            key=key, url=self._object_url(key), size=len(data), content_type=content_type
        )

    async def download(self, key: str) -> bytes:
        client = await self._get_client()
        try:
            resp = await client.get_object(Bucket=self._bucket, Key=key)
            async with resp["Body"] as body:
                return await body.read()
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 download failed for {key!r}") from exc

    async def delete(self, key: str) -> None:
        client = await self._get_client()
        try:
            await client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 delete failed for {key!r}") from exc

    async def presigned_get_url(self, key: str, expires_in: int = 3600) -> str:
        """Presigned GET for private buckets / temporary access."""
        client = await self._get_client()
        try:
            return await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 presign failed for {key!r}") from exc

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack, self._client = None, None


# ---------------------------------------------------------------------------
# Google Cloud Storage backend (gcloud-aio-storage)
# ---------------------------------------------------------------------------


class GCSStorage(StorageBackend):
    def __init__(self) -> None:
        if not settings.GCS_BUCKET:
            raise StorageError("GCS_BUCKET must be set for the 'gcp' storage backend")
        self._bucket = settings.GCS_BUCKET
        self._public_base = settings.GCS_PUBLIC_BASE_URL.rstrip("/")
        self._client: Storage | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> Storage:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = Storage(service_file=settings.GCP_SERVICE_ACCOUNT_FILE)
        return self._client

    def _object_url(self, key: str) -> str:
        if self._public_base:
            return f"{self._public_base}/{key}"
        return f"https://storage.googleapis.com/{self._bucket}/{key}"

    async def upload(self, data: bytes, key: str, content_type: str) -> StorageObject:
        client = await self._get_client()
        try:
            await client.upload(self._bucket, key, data, content_type=content_type)
        except Exception as exc:  # gcloud-aio raises aiohttp/ResponseError types
            raise StorageError(f"GCS upload failed for {key!r}") from exc
        return StorageObject(
            key=key, url=self._object_url(key), size=len(data), content_type=content_type
        )

    async def download(self, key: str) -> bytes:
        client = await self._get_client()
        try:
            return await client.download(self._bucket, key)
        except Exception as exc:
            raise StorageError(f"GCS download failed for {key!r}") from exc

    async def delete(self, key: str) -> None:
        client = await self._get_client()
        try:
            await client.delete(self._bucket, key)
        except Exception as exc:
            raise StorageError(f"GCS delete failed for {key!r}") from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


# ---------------------------------------------------------------------------
# Process-wide singleton + public API
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, type[StorageBackend]] = {
    "s3": S3Storage,
    "gcp": GCSStorage,
}

_storage: StorageBackend | None = None
_storage_lock = asyncio.Lock()


def _build_backend() -> StorageBackend:
    backend = settings.STORAGE_BACKEND
    if backend == "local":
        return LocalStorage(settings.LOCAL_STORAGE_DIR, settings.LOCAL_PUBLIC_BASE_URL)
    return _BACKENDS[backend]()


async def get_storage() -> StorageBackend:
    global _storage
    if _storage is None:
        async with _storage_lock:
            if _storage is None:
                _storage = _build_backend()
    return _storage


async def upload_file(
    file_data: bytes,
    object_name: str,
    content_type: str = "application/octet-stream",
) -> StorageObject:
    backend = await get_storage()
    return await backend.upload(file_data, object_name, content_type)


async def download_file(object_name: str) -> bytes:
    backend = await get_storage()
    return await backend.download(object_name)


async def delete_file(object_name: str) -> None:
    backend = await get_storage()
    await backend.delete(object_name)


async def close_storage() -> None:
    """Release backend clients. Safe to call when no backend was ever built."""
    global _storage
    if _storage is not None:
        await _storage.aclose()
        _storage = None
