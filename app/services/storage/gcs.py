"""Google Cloud Storage backend (gcloud-aio-storage).

``Bucket`` is imported at module scope on purpose: ``presigned_get_url`` resolves
it through this module's globals, which is what lets the unit tests swap in a
fake signer (``monkeypatch.setattr(storage_gcs, "Bucket", ...)``) without a live
GCP project.
"""

from __future__ import annotations

import asyncio

import aiofiles.os
import aiohttp
from gcloud.aio.storage import Bucket, Storage

from app.config import settings
from app.services.storage.base import StorageBackend, StorageError, StorageNotFound, StorageObject


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
