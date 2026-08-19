"""S3-compatible backends (aioboto3).

Two backends share this module because they share a wire protocol, not just a
convenience: ``S3Storage`` targets AWS S3 / Cloudflare R2 / MinIO / Garage, and
``B2Storage`` targets Backblaze B2 through B2's own S3-compatible API. Every
operation -- SigV4 presigning, managed multipart upload, server-side copy -- is
literally the same code path; only which settings supply the credentials, how the
endpoint is derived, and two client-config knobs differ. That difference is
expressed as an ``_S3Params`` the subclass builds, so there is exactly one
implementation of each verb.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

import aioboto3
import aiofiles.os
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.services.storage.base import StorageBackend, StorageError, StorageNotFound, StorageObject

# Backblaze publishes one S3 endpoint per region, and the region is also what
# SigV4 binds into the credential scope -- so the endpoint is derivable and
# B2_ENDPOINT_URL only exists as an override.
B2_ENDPOINT_TEMPLATE = "https://s3.{region}.backblazeb2.com"

# botocore >= 1.36 defaults `request_checksum_calculation` /
# `response_checksum_validation` to "when_supported", which makes the SDK attach
# an `x-amz-sdk-checksum-algorithm` header and a CRC32 trailer to requests whose
# operations model the httpChecksum trait. That is an AWS-side feature; non-AWS
# S3 implementations are the known failure mode for it, and B2 does not need it.
# "when_required" restores the pre-1.36 behaviour (send a checksum only when the
# operation genuinely requires one), which is what B2 expects.
_B2_CLIENT_CONFIG: Mapping[str, object] = {
    "request_checksum_calculation": "when_required",
    "response_checksum_validation": "when_required",
}


@dataclass(frozen=True)
class _S3Params:
    """Everything that distinguishes one S3-compatible target from another.

    ``label`` appears only in ``StorageError`` messages and logs, so an operator
    reading a failure sees which backend actually failed rather than a blanket
    "S3".
    """

    label: str
    bucket: str
    endpoint: str | None
    public_base: str
    region: str | None
    access_key: str | None
    secret_key: str | None
    extra_config: Mapping[str, object] = field(default_factory=dict)


class S3Storage(StorageBackend):
    def __init__(self, params: _S3Params | None = None) -> None:
        p = params if params is not None else self._params_from_settings()
        self._label = p.label
        self._bucket = p.bucket
        self._endpoint = p.endpoint
        self._public_base = p.public_base
        self._region = p.region
        self._session = aioboto3.Session(
            aws_access_key_id=p.access_key,
            aws_secret_access_key=p.secret_key,
            region_name=p.region,
        )
        self._config = BotoConfig(
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
            **p.extra_config,
        )
        self._stack: AsyncExitStack | None = None
        self._client = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _params_from_settings() -> _S3Params:
        if not settings.S3_BUCKET:
            raise StorageError("S3_BUCKET must be set for the 's3' storage backend")
        # Empty strings suppress boto's default credential chain (IAM roles, env, ...),
        # so pass None when unset to let that chain resolve.
        return _S3Params(
            label="S3",
            bucket=settings.S3_BUCKET,
            endpoint=settings.S3_ENDPOINT_URL or None,
            public_base=settings.S3_PUBLIC_BASE_URL.rstrip("/"),
            region=settings.AWS_REGION or None,
            access_key=settings.AWS_ACCESS_KEY_ID or None,
            secret_key=settings.AWS_SECRET_ACCESS_KEY or None,
        )

    def _fail(self, verb: str, key: str) -> StorageError:
        """One sanitized error shape for every verb, labelled with the backend.

        Keeps the provider exception out of the message (callers turn this into a
        generic 502) and keeps the message template in exactly one place."""
        return StorageError(f"{self._label} {verb} failed for {key!r}")

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
        # R2 / MinIO / Garage / B2 expose a path-style endpoint.
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
            raise self._fail("upload", key) from exc
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
            raise self._fail("upload", key) from exc
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
            raise self._fail("download", key) from exc

    async def delete(self, key: str) -> None:
        client = await self._get_client()
        try:
            await client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise self._fail("delete", key) from exc

    async def copy(self, src_key: str, dst_key: str) -> None:
        """Server-side copy: the bytes never traverse this process.

        `copy_object` is a single API call, capped by S3 at 5 GB per object --
        comfortably above MAX_VIDEO_UPLOAD_BYTES (2000 MiB by default). Raise
        that past 5 GB and this needs the multipart copy flow instead. B2's
        S3-compatible CopyObject carries the same 5 GB single-call cap.
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
            raise self._fail("copy", src_key) from exc
        except BotoCoreError as exc:
            raise self._fail("copy", src_key) from exc

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
            raise self._fail("presign", key) from exc

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack, self._client = None, None


def b2_endpoint_url() -> str:
    """B2's S3 endpoint for the configured region, or the explicit override.

    Exposed (rather than inlined) so the config layer and the tests can assert on
    the same derivation the backend actually uses."""
    if settings.B2_ENDPOINT_URL:
        return settings.B2_ENDPOINT_URL.rstrip("/")
    return B2_ENDPOINT_TEMPLATE.format(region=settings.B2_REGION)


class B2Storage(S3Storage):
    """Backblaze B2, via B2's S3-compatible API.

    Deliberately *not* the B2 native API: the S3-compatible endpoint speaks SigV4,
    which means presigned GET URLs (with working Range) come for free and the
    whole playback story -- ``resolve_playback``'s 302/stream split, the worker's
    ffmpeg input URL, nginx's ``/internal-object/`` proxy -- works unchanged. The
    native API would need its own download-authorization tokens and a token
    refresh loop for no gain.

    Credentials are mandatory here, unlike ``S3Storage``: B2 has no equivalent of
    boto's ambient credential chain, so a blank key would only ever surface as an
    opaque 403 on first upload. ``app/config.py`` rejects that at startup.
    """

    @staticmethod
    def _params_from_settings() -> _S3Params:
        if not settings.B2_BUCKET:
            raise StorageError("B2_BUCKET must be set for the 'b2' storage backend")
        return _S3Params(
            label="B2",
            bucket=settings.B2_BUCKET,
            endpoint=b2_endpoint_url(),
            public_base=settings.B2_PUBLIC_BASE_URL.rstrip("/"),
            region=settings.B2_REGION or None,
            access_key=settings.B2_KEY_ID or None,
            secret_key=settings.B2_APPLICATION_KEY or None,
            extra_config=_B2_CLIENT_CONFIG,
        )
