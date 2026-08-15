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
import shutil
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import aioboto3
import aiofiles
import aiofiles.os
import aiohttp
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from gcloud.aio.storage import Bucket, Storage

from app.config import settings


class StorageError(Exception):
    """Backend-agnostic storage failure. Never carries provider internals to callers."""


class StorageNotFound(StorageError):
    """The object is not there -- distinct from "the backend is unreachable".

    Callers need to tell these apart: a record whose object is already gone is a
    real, reachable state (DELETE removes the object before the row, so a failed
    row-delete leaves one behind), and operations like a key rotation should skip
    rather than fail when there is nothing to copy. A backend that is simply down
    must still surface as a 502.
    """


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

    @abstractmethod
    def public_url(self, key: str) -> str:
        """The object's plain (unsigned) URL, i.e. what upload() sets on
        StorageObject.url. Lets callers that hold only a key (e.g. serving a
        deduplicated record) rebuild the same source URL without re-uploading."""

    async def aclose(self) -> None:  # noqa: B027
        """Release any long-lived clients. Default is a no-op."""

    async def presigned_get_url(  # noqa: B027
        self,
        key: str,
        expires_in: int = 3600,
        *,
        content_type: str | None = None,
        content_disposition: str | None = None,
    ) -> str | None:
        """Temporary signed GET URL, for backends where the plain object URL
        isn't usable as-is (e.g. a private S3 bucket). None means the
        backend doesn't support presigning -- callers should fall back to
        the object's regular url.

        ``content_type``/``content_disposition`` ask the store to override those
        response headers, which makes the *record* authoritative for them rather
        than whatever metadata the object happens to carry. That matters in two
        real cases: a backend migration (rclone infers Content-Type from the file
        extension, and the `local` backend has no content-type metadata to carry
        over at all -- a video landing as application/octet-stream downloads
        instead of playing), and filename preservation (the local byte paths set
        an inline Content-Disposition, so without this the object-store 302
        silently drops the original filename)."""
        return None

    def local_path(self, key: str) -> str | None:
        """The object's path on a filesystem the caller shares, if any
        (LocalStorage), else None. Lets a co-located worker hand ffmpeg the path
        directly instead of downloading the bytes into RAM. Object stores have no
        shared filesystem -> None."""
        return None

    async def upload_from_path(self, path: str, key: str, content_type: str) -> StorageObject:
        """Upload the file at ``path`` to ``key``. Default reads it whole and
        delegates to upload(); backends that can stream from disk (local/s3/gcp)
        override this so a large object never lands fully in memory -- the
        write-side counterpart to the worker reading its input in place."""
        async with aiofiles.open(path, "rb") as f:
            data = await f.read()
        return await self.upload(data, key, content_type)

    async def copy(self, src_key: str, dst_key: str) -> None:
        """Copy an object within the same backend.

        Used to rotate an object's key when a record turns private, which is what
        makes already-cached public URLs dead rather than merely unadvertised.
        The default pulls the bytes through this process; every real backend
        overrides it with a server-side copy, so a multi-hundred-MB video is
        never moved through here."""
        data = await self.download(src_key)
        await self.upload(data, dst_key, "application/octet-stream")


# ---------------------------------------------------------------------------
# Local filesystem backend
# ---------------------------------------------------------------------------


class LocalStorage(StorageBackend):
    def __init__(self, root: str, public_base_url: str) -> None:
        self._root = Path(root).resolve()
        self._base = public_base_url.rstrip("/")

    def _resolve(self, key: str) -> Path:
        # Guard against path traversal escaping the storage root.
        p = Path(key)
        if p.is_absolute() or key.startswith("/") or key.startswith("\\"):
            raise StorageError(f"Invalid object key outside storage root: {key!r}")
        target = (self._root / key).resolve()
        if not target.is_relative_to(self._root):
            raise StorageError(f"Invalid object key outside storage root: {key!r}")
        return target

    def public_url(self, key: str) -> str:
        safe_key = key.lstrip("/")
        return f"{self._base}/{safe_key}" if self._base else safe_key

    def local_path(self, key: str) -> str:
        """The object's on-disk path, through the same traversal guard as
        read/write. A co-located worker (sharing the media volume) hands this
        straight to ffmpeg, so the bytes never round-trip through its memory."""
        return str(self._resolve(key))

    async def upload(self, data: bytes, key: str, content_type: str) -> StorageObject:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(target, "wb") as f:
            await f.write(data)
        return StorageObject(
            key=key, url=self.public_url(key), size=len(data), content_type=content_type
        )

    async def upload_from_path(self, path: str, key: str, content_type: str) -> StorageObject:
        # Stream the staged temp file into place a chunk at a time; never load the
        # whole (possibly hundreds-of-MB) object into memory.
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        async with aiofiles.open(path, "rb") as src, aiofiles.open(target, "wb") as dst:
            while chunk := await src.read(1024 * 1024):
                await dst.write(chunk)
                size += len(chunk)
        return StorageObject(
            key=key, url=self.public_url(key), size=size, content_type=content_type
        )

    async def download(self, key: str) -> bytes:
        target = self._resolve(key)
        try:
            async with aiofiles.open(target, "rb") as f:
                return await f.read()
        except FileNotFoundError as exc:
            raise StorageNotFound(f"Object not found: {key!r}") from exc

    async def delete(self, key: str) -> None:
        # Idempotent: deleting a missing object is not an error.
        self._resolve(key).unlink(missing_ok=True)

    async def copy(self, src_key: str, dst_key: str) -> None:
        src, dst = self._resolve(src_key), self._resolve(dst_key)
        if not src.is_file():
            raise StorageNotFound(f"Object not found: {src_key!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        # shutil does the copy in C and off the event loop; never read the whole
        # object into this process just to write it back out.
        await asyncio.to_thread(shutil.copyfile, src, dst)


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
        safe_key = key.lstrip("/")
        # Explicit CDN / public base wins (CloudFront, custom domain).
        if self._public_base:
            return f"{self._public_base}/{safe_key}"
        # R2 / MinIO expose a path-style endpoint.
        if self._endpoint:
            return f"{self._endpoint.rstrip('/')}/{self._bucket}/{safe_key}"
        # Real AWS: virtual-hosted style.
        host = f"s3.{self._region}.amazonaws.com" if self._region else "s3.amazonaws.com"
        return f"https://{self._bucket}.{host}/{safe_key}"

    def public_url(self, key: str) -> str:
        return self._object_url(key)

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

    async def upload_from_path(self, path: str, key: str, content_type: str) -> StorageObject:
        # upload_file streams from disk (managed multipart) -- the object never
        # lands fully in this process's memory, unlike upload_fileobj(BytesIO(data)).
        client = await self._get_client()
        try:
            await client.upload_file(
                path, self._bucket, key, ExtraArgs={"ContentType": content_type}
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 upload failed for {key!r}") from exc
        size = await aiofiles.os.path.getsize(path)
        return StorageObject(
            key=key, url=self._object_url(key), size=size, content_type=content_type
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

    async def copy(self, src_key: str, dst_key: str) -> None:
        """Server-side copy: the bytes never traverse this process.

        `copy_object` is a single API call, capped by S3 at 5 GB per object --
        comfortably above MAX_VIDEO_UPLOAD_BYTES (2000 MiB by default). Raise
        that past 5 GB and this needs the multipart copy flow instead.
        """
        client = await self._get_client()
        try:
            await client.copy_object(
                Bucket=self._bucket,
                Key=dst_key,
                CopySource={"Bucket": self._bucket, "Key": src_key},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "NotFound", "404"}:
                raise StorageNotFound(f"Object not found: {src_key!r}") from exc
            raise StorageError(f"S3 copy failed for {src_key!r}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"S3 copy failed for {src_key!r}") from exc

    async def presigned_get_url(
        self,
        key: str,
        expires_in: int = 3600,
        *,
        content_type: str | None = None,
        content_disposition: str | None = None,
    ) -> str:
        """Presigned GET for private buckets / temporary access.

        The optional response-header overrides are signed into the URL (S3's
        response-content-* query parameters), so the record -- not the stored
        object's metadata -- decides what the client sees. See the base class."""
        client = await self._get_client()
        params: dict[str, str] = {"Bucket": self._bucket, "Key": key}
        if content_type:
            params["ResponseContentType"] = content_type
        if content_disposition:
            params["ResponseContentDisposition"] = content_disposition
        try:
            return await client.generate_presigned_url(
                "get_object",
                Params=params,
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
        safe_key = key.lstrip("/")
        if self._public_base:
            return f"{self._public_base}/{safe_key}"
        return f"https://storage.googleapis.com/{self._bucket}/{safe_key}"

    def public_url(self, key: str) -> str:
        return self._object_url(key)

    async def upload(self, data: bytes, key: str, content_type: str) -> StorageObject:
        client = await self._get_client()
        try:
            await client.upload(self._bucket, key, data, content_type=content_type)
        except aiohttp.ClientError as exc:
            raise StorageError(f"GCS upload failed for {key!r}") from exc
        return StorageObject(
            key=key, url=self._object_url(key), size=len(data), content_type=content_type
        )

    async def upload_from_path(self, path: str, key: str, content_type: str) -> StorageObject:
        # Hand gcloud-aio the open file object so it streams the upload (resumable)
        # rather than buffering the whole object in memory. gcloud-aio reads the
        # stream synchronously, so it must be a plain file object (not aiofiles);
        # GCS is mock-only here (no live GCP), asserted by test.
        client = await self._get_client()
        size = await aiofiles.os.path.getsize(path)
        try:
            with open(path, "rb") as f:  # noqa: ASYNC230  # NOSONAR -- gcloud-aio requires sync file object
                await client.upload(
                    self._bucket, key, f, content_type=content_type, force_resumable_upload=True
                )
        except aiohttp.ClientError as exc:
            raise StorageError(f"GCS upload failed for {key!r}") from exc
        return StorageObject(
            key=key, url=self._object_url(key), size=size, content_type=content_type
        )

    async def download(self, key: str) -> bytes:
        client = await self._get_client()
        try:
            return await client.download(self._bucket, key)
        except aiohttp.ClientError as exc:
            raise StorageError(f"GCS download failed for {key!r}") from exc

    async def delete(self, key: str) -> None:
        client = await self._get_client()
        try:
            await client.delete(self._bucket, key)
        except aiohttp.ClientResponseError as exc:
            # Idempotent: an already-deleted object (404) is success, matching
            # LocalStorage.unlink(missing_ok=True) and S3's delete_object (204 on a
            # missing key). gcloud-aio-storage surfaces a non-2xx as a
            # ClientResponseError (a ClientError subclass), so this branch must
            # precede the generic ClientError handler below. Without it a retried
            # DELETE /files/{id} after a storage-delete-succeeded / row-delete-failed
            # race would 502 forever instead of self-healing.
            if exc.status == 404:
                return
            raise StorageError(f"GCS delete failed for {key!r}") from exc
        except aiohttp.ClientError as exc:
            raise StorageError(f"GCS delete failed for {key!r}") from exc

    async def copy(self, src_key: str, dst_key: str) -> None:
        """Server-side copy within the same bucket (rewrite API under the hood)."""
        client = await self._get_client()
        try:
            await client.copy(self._bucket, src_key, self._bucket, new_name=dst_key)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                raise StorageNotFound(f"Object not found: {src_key!r}") from exc
            raise StorageError(f"GCS copy failed for {src_key!r}") from exc
        except aiohttp.ClientError as exc:
            raise StorageError(f"GCS copy failed for {src_key!r}") from exc

    async def presigned_get_url(
        self,
        key: str,
        expires_in: int = 3600,
        *,
        content_type: str | None = None,
        content_disposition: str | None = None,
    ) -> str:
        """V4 signed GET URL for a private GCS object.

        Signed **locally** from the service-account key already loaded by the
        client (gcloud-aio-storage's ``Blob.get_signed_url`` uses the key's
        ``private_key``/``client_email`` when present) -- no CDN, no extra Google
        product, no network round-trip for signing. This is the GCS analogue of
        S3's presigned GET and is what makes private video playback on the ``gcp``
        backend possible; without it GCS can only serve objects that are public.
        GCS caps a V4 signature at 7 days (604800s); a longer request is clamped.

        The response-header overrides ride as signed `response-content-*` query
        params (GCS's equivalent of S3's ResponseContentType) -- see the base
        class for why the record, not the object's metadata, is authoritative.
        """
        client = await self._get_client()
        expires_in = min(expires_in, 604800)
        query_params: dict[str, str] = {}
        if content_type:
            query_params["response-content-type"] = content_type
        if content_disposition:
            query_params["response-content-disposition"] = content_disposition
        try:
            blob = Bucket(client, self._bucket).new_blob(key)
            return await blob.get_signed_url(expires_in, query_params=query_params or None)
        except (aiohttp.ClientError, ValueError, KeyError) as exc:
            raise StorageError(f"GCS presign failed for {key!r}") from exc

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


def public_object_url(key: str) -> str:
    """The object's plain, unsigned public URL -- resolved *synchronously*.

    Exists because the sync callers that need it (``UploadRecord.to_public``,
    which serializes inside a Pydantic response model) cannot await
    ``get_storage()``. Reuses the singleton when it is already built and
    otherwise constructs a throwaway backend, which is safe precisely because
    ``_build_backend`` does no I/O and opens no client -- every backend defers
    client construction to first use. It deliberately does NOT populate the
    singleton, since that would bypass ``_storage_lock``.
    """
    backend = _storage if _storage is not None else _build_backend()
    return backend.public_url(key)


def has_public_base_url() -> bool:
    """Whether the active backend can produce a URL a client can fetch directly.

    True only for the object stores with a ``*_PUBLIC_BASE_URL`` (a CDN or public
    bucket domain that actually serves the object). **Never for local**: that
    media volume is exposed only through nginx's internal X-Accel location -- by
    design, since that is what makes ownership checks unbypassable -- so a
    ``LOCAL_PUBLIC_BASE_URL``-derived link would dead-end on a 404.
    """
    if settings.STORAGE_BACKEND == "s3":
        return bool(settings.S3_PUBLIC_BASE_URL)
    if settings.STORAGE_BACKEND == "gcp":
        return bool(settings.GCS_PUBLIC_BASE_URL)
    return False


async def upload_file(
    file_data: bytes,
    object_name: str,
    content_type: str = "application/octet-stream",
) -> StorageObject:
    backend = await get_storage()
    return await backend.upload(file_data, object_name, content_type)


async def upload_file_from_path(
    path: str,
    object_name: str,
    content_type: str = "application/octet-stream",
) -> StorageObject:
    """Stream a staged temp file into storage without loading it into memory (the
    write-side counterpart to upload_file's bytes-in interface)."""
    backend = await get_storage()
    return await backend.upload_from_path(path, object_name, content_type)


async def download_file(object_name: str) -> bytes:
    backend = await get_storage()
    return await backend.download(object_name)


async def delete_file(object_name: str) -> None:
    backend = await get_storage()
    await backend.delete(object_name)


async def copy_file(src_key: str, dst_key: str) -> None:
    backend = await get_storage()
    await backend.copy(src_key, dst_key)


async def close_storage() -> None:
    """Release backend clients. Safe to call when no backend was ever built."""
    global _storage
    if _storage is not None:
        await _storage.aclose()
        _storage = None
