import base64
import hashlib
import hmac
from urllib.parse import urlparse

import pytest

from app.config import Settings, settings
from app.services.imgproxy import build_source_url, generate_signed_url, sign_url


def _expected_signature(path: str) -> str:
    key = bytes.fromhex(settings.IMGPROXY_KEY)
    salt = bytes.fromhex(settings.IMGPROXY_SALT)
    mac = hmac.new(key, digestmod=hashlib.sha256)
    mac.update(salt)
    mac.update(path.encode())
    return base64.urlsafe_b64encode(mac.digest()).decode("utf-8").rstrip("=")


def test_sign_url_matches_hmac_computed_independently() -> None:
    path = "/rs:fill:300:300/aGVsbG8"
    result = sign_url(path)
    expected_sig = _expected_signature(path)
    assert result.endswith(f"/{expected_sig}{path}")


def test_sign_url_uses_base_url_prefix_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "IMGPROXY_BASE_URL", "http://imgproxy.internal:8080")
    result = sign_url("/rs:fill:300:300/aGVsbG8")
    parsed = urlparse(result)
    assert parsed.scheme == "http"
    assert parsed.netloc == "imgproxy.internal:8080"


def test_sign_url_is_path_only_when_base_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "IMGPROXY_BASE_URL", "")
    result = sign_url("/rs:fill:300:300/aGVsbG8")
    parsed = urlparse(result)
    assert parsed.scheme == ""
    assert parsed.netloc == ""
    assert result.startswith("/")


def test_generate_signed_url_base64_encodes_source_url() -> None:
    result = generate_signed_url("http://storage.example/images/abc.webp", "rs:auto")
    assert "/rs:auto/" in result
    assert result.endswith(".webp")

    # Explicit format overrides source URL extension
    png_result = generate_signed_url(
        "http://storage.example/images/abc.webp", "rs:auto", format="png"
    )
    assert png_result.endswith(".png")

    # Unrecognized or missing extension defaults to webp
    default_result = generate_signed_url("http://storage.example/images/noext", "rs:auto")
    assert default_result.endswith(".webp")


@pytest.mark.parametrize("field_name", ["IMGPROXY_KEY", "IMGPROXY_SALT"])
def test_blank_imgproxy_key_or_salt_fails_fast(field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        Settings(**{field_name: ""})


@pytest.mark.parametrize("field_name", ["IMGPROXY_KEY", "IMGPROXY_SALT"])
def test_invalid_hex_imgproxy_key_or_salt_fails_fast(field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        Settings(**{field_name: "not-hex-zzz"})


def test_build_source_url_uses_local_scheme_for_local_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    assert build_source_url("images/x.webp", "images/x.webp") == "local:///images/x.webp"


def test_build_source_url_passes_through_for_non_local_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    url = "https://bucket.s3.amazonaws.com/images/x.webp?X-Amz-Signature=abc"
    assert build_source_url("images/x.webp", url) == url
