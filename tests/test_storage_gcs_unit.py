"""GCS backend URL construction only.

No live GCP credentials/project are available in this environment -- these
are unit tests against a freshly-constructed backend (GCSStorage.__init__
never touches the network; only _get_client() does, which none of these
call), not a live round-trip against a real bucket.
"""

import pytest

from app.config import settings
from app.services.storage import GCSStorage


def test_object_url_uses_public_base_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "GCS_PUBLIC_BASE_URL", "https://cdn.example.com")
    backend = GCSStorage()
    assert backend._object_url("images/x.webp") == "https://cdn.example.com/images/x.webp"


def test_object_url_falls_back_to_storage_googleapis_com(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "GCS_PUBLIC_BASE_URL", "")
    backend = GCSStorage()
    assert (
        backend._object_url("images/x.webp")
        == "https://storage.googleapis.com/test-bucket/images/x.webp"
    )


def test_object_url_is_independent_of_local_public_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression check for the bug this commit fixes: GCS and Local used to
    # share a single PUBLIC_BASE_URL setting.
    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "GCS_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(settings, "LOCAL_PUBLIC_BASE_URL", "https://local-cdn.example.com")
    backend = GCSStorage()
    assert "local-cdn" not in backend._object_url("images/x.webp")
