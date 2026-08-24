"""Per-backend startup validation and the public-base-url resolution map.

`Settings()` validates at import and the process dies on a bad config, which is
the point -- but it also means the only way to test the rules is to build a fresh
Settings with explicit values. `_env_file=None` keeps a developer's real `.env`
(and the compose service's env) out of these, so the assertions are about the
declared fields alone.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.storage import has_public_base_url

# Everything unrelated to storage that Settings insists on before it will build.
_BASE: dict[str, object] = {
    "_env_file": None,
    "FILE_MANAGER_BEARER_TOKENS": "test-token",
    "IMGPROXY_KEY": "aa" * 32,
    "IMGPROXY_SALT": "bb" * 32,
}

_B2_COMPLETE: dict[str, object] = {
    "STORAGE_BACKEND": "b2",
    "B2_BUCKET": "media",
    "B2_KEY_ID": "0123456789abcdef01234567",
    "B2_APPLICATION_KEY": "K004fakefakefakefakefakefakefake",
    "B2_REGION": "us-west-004",
}


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_BASE, **overrides})  # type: ignore[arg-type]


# --- backend selection -----------------------------------------------------


def test_b2_is_an_accepted_backend() -> None:
    assert _settings(**_B2_COMPLETE).STORAGE_BACKEND == "b2"


def test_backend_name_is_normalized() -> None:
    assert _settings(**{**_B2_COMPLETE, "STORAGE_BACKEND": "  B2 "}).STORAGE_BACKEND == "b2"


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(STORAGE_BACKEND="backblaze")


# --- fail-fast on an incomplete b2 config ----------------------------------
#
# B2 has no ambient credential chain (unlike boto's IAM-role/env fallback for
# s3), so a blank key is a misconfiguration rather than "resolve it later". Each
# of these would otherwise boot fine and then 403 on the first upload.


@pytest.mark.parametrize(
    "missing",
    ["B2_BUCKET", "B2_KEY_ID", "B2_APPLICATION_KEY", "B2_REGION"],
)
def test_b2_requires_every_credential_field(missing: str) -> None:
    incomplete = {**_B2_COMPLETE, missing: ""}

    with pytest.raises(ValidationError):
        _settings(**incomplete)


def test_b2_endpoint_url_stays_optional() -> None:
    # Derived from the region when blank -- an operator should not have to know
    # the endpoint template.
    assert _settings(**_B2_COMPLETE).B2_ENDPOINT_URL == ""


def test_s3_still_only_requires_its_bucket() -> None:
    # Unchanged behaviour: blank AWS_* means "use boto's credential chain".
    settings = _settings(STORAGE_BACKEND="s3", S3_BUCKET="media")
    assert settings.AWS_ACCESS_KEY_ID == ""


def test_s3_without_a_bucket_is_still_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(STORAGE_BACKEND="s3")


def test_gcp_without_a_bucket_is_still_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(STORAGE_BACKEND="gcp")


# --- the backend -> public base URL map ------------------------------------


def test_b2_public_base_url_is_resolved_for_the_active_backend() -> None:
    settings = _settings(**{**_B2_COMPLETE, "B2_PUBLIC_BASE_URL": "https://cdn.example.com"})
    assert settings.active_public_base_url == "https://cdn.example.com"


def test_b2_without_a_public_base_url_resolves_to_empty() -> None:
    assert _settings(**_B2_COMPLETE).active_public_base_url == ""


def test_b2_does_not_pick_up_another_backends_public_base_url() -> None:
    settings = _settings(**{**_B2_COMPLETE, "S3_PUBLIC_BASE_URL": "https://s3-cdn.example.com"})
    assert settings.active_public_base_url == ""


def test_local_reports_public_base_url_when_configured() -> None:
    # Local development with LOCAL_PUBLIC_BASE_URL resolves to the configured URL,
    # served directly by nginx for public prefixes.
    settings = _settings(STORAGE_BACKEND="local", LOCAL_PUBLIC_BASE_URL="http://localhost:9000")
    assert settings.active_public_base_url == "http://localhost:9000"


def test_local_without_public_base_url_resolves_to_empty() -> None:
    settings = _settings(STORAGE_BACKEND="local", LOCAL_PUBLIC_BASE_URL="")
    assert settings.active_public_base_url == ""


def test_b2_without_a_public_base_url_warns_about_unservable_images() -> None:
    assert _settings(**_B2_COMPLETE).public_images_unservable is True


def test_b2_with_a_public_base_url_is_servable() -> None:
    settings = _settings(**{**_B2_COMPLETE, "B2_PUBLIC_BASE_URL": "https://cdn.example.com"})
    assert settings.public_images_unservable is False


def test_local_is_never_flagged_unservable() -> None:
    assert _settings(STORAGE_BACKEND="local").public_images_unservable is False


# --- storage.has_public_base_url() reads the same map ----------------------


def test_has_public_base_url_is_true_for_b2_with_a_cdn(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "STORAGE_BACKEND", "b2")
    monkeypatch.setattr(live_settings, "B2_PUBLIC_BASE_URL", "https://cdn.example.com")
    assert has_public_base_url() is True


def test_has_public_base_url_is_false_for_b2_without_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "STORAGE_BACKEND", "b2")
    monkeypatch.setattr(live_settings, "B2_PUBLIC_BASE_URL", "")
    assert has_public_base_url() is False


def test_has_public_base_url_is_true_for_local_with_configured_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(live_settings, "LOCAL_PUBLIC_BASE_URL", "http://localhost:9000")
    assert has_public_base_url() is True


def test_has_public_base_url_is_false_for_local_without_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(live_settings, "LOCAL_PUBLIC_BASE_URL", "")
    assert has_public_base_url() is False
