"""Backblaze B2 backend: endpoint derivation, URL construction, presigning.

No live B2 account exists in this environment. That costs less coverage than it
looks like it should: B2 is reached through its S3-compatible API, so every verb
is the inherited ``S3Storage`` implementation and presigning is pure client-side
SigV4 with no network round trip -- exercised here for real against dummy
credentials, exactly like ``test_storage_s3_unit.py``. What these tests own is
the part that is genuinely B2-specific: which settings feed the client, how the
endpoint is derived, and the two checksum knobs B2 needs.

The shared S3 code path is additionally exercised over a real wire by the
``s3_integration`` tests, which run their whole suite through ``B2Storage``
against Garage.
"""

from urllib.parse import parse_qs, urlparse

import pytest

from app.config import settings
from app.services.storage import B2Storage
from app.services.storage.base import StorageError


@pytest.fixture
def b2_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "B2_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "B2_KEY_ID", "0123456789abcdef01234567")
    monkeypatch.setattr(settings, "B2_APPLICATION_KEY", "K004fakefakefakefakefakefakefake")
    monkeypatch.setattr(settings, "B2_REGION", "us-west-004")
    monkeypatch.setattr(settings, "B2_ENDPOINT_URL", "")
    monkeypatch.setattr(settings, "B2_PUBLIC_BASE_URL", "")


# --- endpoint derivation ---------------------------------------------------


def test_endpoint_is_derived_from_the_region(b2_settings: None) -> None:
    backend = B2Storage()
    assert backend._endpoint == "https://s3.us-west-004.backblazeb2.com"


def test_explicit_endpoint_overrides_the_derived_one(
    b2_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "B2_ENDPOINT_URL", "http://garage:3900/")
    backend = B2Storage()
    # Also asserts the trailing slash is stripped, so _object_url never doubles it.
    assert backend._endpoint == "http://garage:3900"


def test_region_is_passed_through_for_sigv4_scope(b2_settings: None) -> None:
    # SigV4 binds the region into the credential scope and B2 validates it, so a
    # dropped region is an authentication failure, not a cosmetic difference.
    backend = B2Storage()
    assert backend._region == "us-west-004"


def test_credentials_come_from_the_b2_namespace_not_aws(
    b2_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point of a separate namespace: a leftover AWS_* value from an
    # earlier s3 deployment must not leak into B2's client.
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "leftover-s3-key")
    monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "leftover-s3-secret")
    params = B2Storage._params_from_settings()

    assert params.access_key == "0123456789abcdef01234567"
    assert params.secret_key == "K004fakefakefakefakefakefakefake"


# --- B2 compatibility knobs ------------------------------------------------


def test_request_checksums_are_only_sent_when_required(b2_settings: None) -> None:
    # botocore >= 1.36 defaults this to "when_supported", which attaches an
    # AWS-specific CRC32 trailer that B2 does not model. Silent to catch by hand;
    # this is the regression guard.
    backend = B2Storage()
    assert backend._config.request_checksum_calculation == "when_required"


def test_response_checksum_validation_is_only_done_when_required(b2_settings: None) -> None:
    backend = B2Storage()
    assert backend._config.response_checksum_validation == "when_required"


def test_s3_backend_keeps_botocore_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The knobs above are B2's, not a global change: real AWS still gets the
    # SDK's own default behaviour.
    from app.services.storage import S3Storage

    monkeypatch.setattr(settings, "S3_BUCKET", "test-bucket")
    backend = S3Storage()
    assert backend._config.request_checksum_calculation is None


# --- URL construction ------------------------------------------------------


def test_object_url_uses_public_base_when_set(
    b2_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "B2_PUBLIC_BASE_URL", "https://cdn.example.com")
    backend = B2Storage()
    assert backend._object_url("images/x.webp") == "https://cdn.example.com/images/x.webp"


def test_object_url_falls_back_to_the_path_style_endpoint(b2_settings: None) -> None:
    backend = B2Storage()
    assert (
        backend._object_url("images/x.webp")
        == "https://s3.us-west-004.backblazeb2.com/test-bucket/images/x.webp"
    )


def test_object_url_is_independent_of_the_s3_public_base_url(
    b2_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same class of bug the GCS/local shared-field regression covers: switching
    # STORAGE_BACKEND must not silently reuse another backend's CDN domain.
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "https://s3-cdn.example.com")
    backend = B2Storage()
    assert "s3-cdn" not in backend._object_url("images/x.webp")


# --- presigning (real SigV4, no network) -----------------------------------


async def test_presigned_get_url_is_really_signed(b2_settings: None) -> None:
    backend = B2Storage()
    url = await backend.presigned_get_url("videos/v_compressed.mp4", expires_in=120)
    await backend.aclose()

    assert "videos/v_compressed.mp4" in url
    assert "X-Amz-Signature" in url


async def test_presigned_get_url_targets_the_b2_endpoint(b2_settings: None) -> None:
    backend = B2Storage()
    url = await backend.presigned_get_url("videos/v.mp4", expires_in=120)
    await backend.aclose()

    # Exact host, not a substring: a presign that pointed at
    # "backblazeb2.com.evil.example" would satisfy a containment check.
    assert urlparse(url).netloc == "s3.us-west-004.backblazeb2.com"


async def test_presigned_get_url_signs_the_response_header_overrides(b2_settings: None) -> None:
    # resolve_playback always passes both, so the record -- not the stored
    # object's metadata -- decides Content-Type and filename on playback.
    backend = B2Storage()
    url = await backend.presigned_get_url(
        "videos/v.mp4",
        expires_in=120,
        content_type="video/mp4",
        content_disposition='inline; filename="clip.mp4"',
    )
    await backend.aclose()
    query = parse_qs(urlparse(url).query)

    assert query["response-content-type"] == ["video/mp4"]
    assert query["response-content-disposition"] == ['inline; filename="clip.mp4"']
    # They are part of the signed canonical query string, not a hint a proxy
    # could strip -- so the signature covers them.
    assert "X-Amz-Signature" in query


async def test_presigned_get_url_honours_the_requested_expiry(b2_settings: None) -> None:
    backend = B2Storage()
    url = await backend.presigned_get_url("videos/v.mp4", expires_in=120)
    await backend.aclose()
    query = parse_qs(urlparse(url).query)

    assert query["X-Amz-Expires"] == ["120"]


# --- construction guard ----------------------------------------------------


def test_missing_bucket_raises_storage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "B2_BUCKET", "")

    with pytest.raises(StorageError):
        B2Storage()


def test_error_messages_are_labelled_b2_not_s3(b2_settings: None) -> None:
    # Shared implementation, but an operator reading a 502's server-side log
    # must be able to tell which backend actually failed.
    backend = B2Storage()
    assert "B2" in str(backend._fail("upload", "images/x.webp"))
