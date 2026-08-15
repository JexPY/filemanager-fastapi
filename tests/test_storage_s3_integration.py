"""S3 backend against a live S3-compatible server (the `garage` compose service).

The sibling `test_storage_s3_unit.py` covers URL construction and client-side
signing without a network. This module covers what only a real server can: that
boto3's managed transfer actually completes a *multipart* upload, that a
presigned SigV4 GET is honoured when fetched over plain HTTP, and that HTTP
Range works on the resulting URL -- the three properties video playback on the
`s3` backend depends on.

Start the fixture with `docker compose --profile s3-dev up -d garage garage-init`.
Without it these tests skip; set `S3_INTEGRATION_REQUIRED=1` (CI does) to turn
that skip into a failure, so a fixture that never came up cannot masquerade as
a green run.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.services.storage import S3Storage

pytestmark = pytest.mark.s3_integration

# Defaults match the compose `garage` service; overridable for a different
# S3-compatible target.
ENDPOINT_URL = os.environ.get("S3_TEST_ENDPOINT_URL", "http://garage:3900")
BUCKET = os.environ.get("S3_TEST_BUCKET", "filemanager-test")
ACCESS_KEY = os.environ.get("S3_TEST_ACCESS_KEY", "garageadmin")
SECRET_KEY = os.environ.get("S3_TEST_SECRET_KEY", "garageadminsecretkey")
# Must match the server's configured `s3_region`; SigV4 binds the region into
# the credential scope, so a mismatch is rejected before the request is served.
REGION = os.environ.get("S3_TEST_REGION", "garage")

# Comfortably over boto3's 8 MiB multipart threshold, so upload_file is forced
# down the CreateMultipartUpload/UploadPart/Complete path rather than a PUT.
MULTIPART_PAYLOAD = bytes(range(256)) * (9 * 1024 * 1024 // 256)

# A multipart object's ETag is `<hex>-<partcount>`; a single PUT's is a bare
# MD5. That suffix is the server telling us the transfer really was multipart.
_MULTIPART_ETAG = re.compile(r'"[0-9a-f]{32}-\d+"')


def _endpoint_is_live() -> bool:
    """Any HTTP answer proves the server is up. Garage refuses anonymous access
    (403), so this deliberately does not require a 2xx."""
    try:
        httpx.get(ENDPOINT_URL, timeout=2.0)
    except httpx.HTTPError:
        return False
    return True


@pytest.fixture(scope="module")
def _require_live_endpoint() -> None:
    if _endpoint_is_live():
        return
    message = (
        f"No S3-compatible server at {ENDPOINT_URL}; "
        "start it with `docker compose --profile s3-dev up -d garage garage-init`"
    )
    if os.environ.get("S3_INTEGRATION_REQUIRED"):
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture
async def s3(
    _require_live_endpoint: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[S3Storage]:
    monkeypatch.setattr(settings, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", ENDPOINT_URL)
    # Blank so _object_url() exercises the endpoint (path-style) branch, which
    # is the branch that matters for every non-AWS backend.
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", SECRET_KEY)
    monkeypatch.setattr(settings, "AWS_REGION", REGION)
    backend = S3Storage()
    try:
        yield backend
    finally:
        await backend.aclose()


@pytest.fixture
def key() -> str:
    """A per-test key so a shared bucket never causes cross-test interference."""
    return f"itest/{uuid.uuid4()}.bin"


async def test_streaming_upload_from_path_completes_a_multipart_upload(
    s3: S3Storage, key: str, tmp_path: Path
) -> None:
    src = tmp_path / "big.bin"
    src.write_bytes(MULTIPART_PAYLOAD)

    obj = await s3.upload_from_path(str(src), key, "video/mp4")
    try:
        assert obj.key == key
        assert obj.size == len(MULTIPART_PAYLOAD)
        assert obj.content_type == "video/mp4"

        client = await s3._get_client()
        head = await client.head_object(Bucket=BUCKET, Key=key)
        assert head["ContentLength"] == len(MULTIPART_PAYLOAD)
        # ExtraArgs={"ContentType": ...} must survive the multipart path, not
        # just the simple PUT -- otherwise compressed video is served as
        # application/octet-stream and browsers refuse to play it.
        assert head["ContentType"] == "video/mp4"
        assert _MULTIPART_ETAG.fullmatch(head["ETag"]), (
            f"expected a multipart ETag, got {head['ETag']!r} -- the managed "
            "transfer fell back to a single PUT"
        )

        assert await s3.download(key) == MULTIPART_PAYLOAD
    finally:
        await s3.delete(key)


async def test_upload_and_download_round_trip(s3: S3Storage, key: str) -> None:
    payload = b"garage round trip \x00\xff binary"
    obj = await s3.upload(payload, key, "application/octet-stream")
    try:
        assert obj.size == len(payload)
        # Path-style: {endpoint}/{bucket}/{key}, not virtual-hosted.
        assert obj.url == f"{ENDPOINT_URL}/{BUCKET}/{key}"
        assert await s3.download(key) == payload
    finally:
        await s3.delete(key)


async def test_presigned_get_url_is_fetchable_over_plain_http(s3: S3Storage, key: str) -> None:
    payload = b"presigned payload" * 64
    await s3.upload(payload, key, "text/plain")
    try:
        url = await s3.presigned_get_url(key, expires_in=300)
        assert url.startswith(f"{ENDPOINT_URL}/{BUCKET}/{key}")
        assert "X-Amz-Signature" in url

        async with httpx.AsyncClient() as http:
            resp = await http.get(url)
        assert resp.status_code == 200
        assert resp.content == payload
    finally:
        await s3.delete(key)


async def test_presigned_url_serves_range_requests(s3: S3Storage, key: str) -> None:
    """The property video playback rests on: the 302 target must honour Range,
    since the app is out of the byte path on the `s3` backend."""
    payload = bytes(range(256)) * 512  # 128 KiB, deterministic
    await s3.upload(payload, key, "video/mp4")
    try:
        url = await s3.presigned_get_url(key, expires_in=300)
        async with httpx.AsyncClient() as http:
            resp = await http.get(url, headers={"Range": "bytes=1000-1999"})
            suffix = await http.get(url, headers={"Range": "bytes=-100"})

        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 1000-1999/{len(payload)}"
        assert resp.content == payload[1000:2000]

        # Seeking backwards from the end -- what a player does to read a
        # trailing moov atom.
        assert suffix.status_code == 206
        assert suffix.content == payload[-100:]
    finally:
        await s3.delete(key)


async def test_unsigned_object_url_is_not_publicly_readable(s3: S3Storage, key: str) -> None:
    """Guards the assumption behind preferring a presigned URL for `private`
    playback: the plain object URL is not a way around ownership checks."""
    await s3.upload(b"not public", key, "text/plain")
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(s3.public_url(key))
        assert resp.status_code in (401, 403, 404)
    finally:
        await s3.delete(key)


async def test_delete_is_idempotent_on_a_missing_key(s3: S3Storage, key: str) -> None:
    await s3.upload(b"transient", key, "text/plain")
    await s3.delete(key)
    # A retried DELETE /files/{id} must not 502 just because the object is
    # already gone.
    await s3.delete(key)
    await s3.delete(f"itest/never-existed-{uuid.uuid4()}.bin")
