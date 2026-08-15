"""S3 backend: URL construction + presigning.

Presigned URL generation is pure client-side signing (no network round
trip), so this exercises the real boto3/aioboto3 signing path against
dummy credentials -- not mocked -- without needing a real bucket or
network access.
"""

import pytest

from app.config import settings
from app.services.storage import S3Storage


@pytest.fixture
def s3_backend(monkeypatch: pytest.MonkeyPatch) -> S3Storage:
    monkeypatch.setattr(settings, "S3_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "fake-access-key")
    monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "fake-secret-key")
    monkeypatch.setattr(settings, "AWS_REGION", "us-east-1")
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "")
    return S3Storage()


async def test_presigned_get_url_produces_a_signed_url(s3_backend: S3Storage) -> None:
    url = await s3_backend.presigned_get_url("images/test.webp", expires_in=120)
    assert url is not None
    assert "images/test.webp" in url
    assert "test-bucket" in url
    # Signed (v2 or v4, depending on boto3's region defaults) -- either way
    # it must carry some form of signature/expiry, proving real signing ran.
    assert "Signature" in url or "X-Amz-Signature" in url
    await s3_backend.aclose()


def test_object_url_uses_endpoint_for_minio_r2_style(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "S3_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "")
    backend = S3Storage()
    assert backend._object_url("x.webp") == "http://minio:9000/test-bucket/x.webp"


def test_object_url_uses_public_base_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "S3_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "https://cdn.example.com")
    backend = S3Storage()
    assert backend._object_url("x.webp") == "https://cdn.example.com/x.webp"


def test_object_url_falls_back_to_virtual_hosted_aws_style(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "S3_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(settings, "AWS_REGION", "eu-central-1")
    backend = S3Storage()
    assert (
        backend._object_url("x.webp") == "https://test-bucket.s3.eu-central-1.amazonaws.com/x.webp"
    )
