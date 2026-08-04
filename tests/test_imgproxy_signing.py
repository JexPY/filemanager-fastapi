import base64
import hashlib
import hmac

import pytest

from app.config import Settings, settings
from app.services.imgproxy import generate_signed_url, sign_url


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
    assert result.startswith("http://imgproxy.internal:8080/")


def test_sign_url_is_path_only_when_base_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "IMGPROXY_BASE_URL", "")
    result = sign_url("/rs:fill:300:300/aGVsbG8")
    assert result.startswith("/")
    assert "http" not in result


def test_generate_signed_url_base64_encodes_source_url() -> None:
    result = generate_signed_url("http://storage.example/images/abc.webp", "rs:auto")
    assert "/rs:auto/" in result


@pytest.mark.parametrize("field_name", ["IMGPROXY_KEY", "IMGPROXY_SALT"])
def test_blank_imgproxy_key_or_salt_fails_fast(field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        Settings(**{field_name: ""})


@pytest.mark.parametrize("field_name", ["IMGPROXY_KEY", "IMGPROXY_SALT"])
def test_invalid_hex_imgproxy_key_or_salt_fails_fast(field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        Settings(**{field_name: "not-hex-zzz"})
