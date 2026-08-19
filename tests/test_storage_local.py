import pytest

from app.services.storage import LocalStorage, StorageError


async def test_upload_download_roundtrip(tmp_path) -> None:
    backend = LocalStorage(str(tmp_path), "")
    obj = await backend.upload(b"hello", "images/test.webp", "image/webp")
    assert obj.url == "images/test.webp"  # bare key when no public base url set
    assert await backend.download("images/test.webp") == b"hello"


async def test_upload_url_uses_public_base_when_set(tmp_path) -> None:
    backend = LocalStorage(str(tmp_path), "https://cdn.example.com")
    obj = await backend.upload(b"hello", "images/test.webp", "image/webp")
    assert obj.url == "https://cdn.example.com/images/test.webp"


async def test_download_missing_object_raises_storage_error(tmp_path) -> None:
    backend = LocalStorage(str(tmp_path), "")
    with pytest.raises(StorageError):
        await backend.download("does/not/exist.webp")


async def test_delete_is_idempotent(tmp_path) -> None:
    backend = LocalStorage(str(tmp_path), "")
    await backend.delete("never/existed.webp")  # must not raise


async def test_local_path_returns_none_for_a_missing_object(tmp_path) -> None:
    """local_path() used to hand back a path unconditionally -- for a key
    that was never uploaded (or whose raw object was already deleted, e.g. a
    race with the owner's DELETE), that meant handing ffmpeg a nonexistent
    path instead of the caller (_resolve_ffmpeg_input) getting a clean,
    early StorageError."""
    backend = LocalStorage(str(tmp_path), "")
    assert await backend.local_path("videos/never-uploaded.mp4") is None


async def test_local_path_returns_a_readable_path_for_an_existing_object(tmp_path) -> None:
    backend = LocalStorage(str(tmp_path), "")
    await backend.upload(b"bytes", "videos/real.mp4", "video/mp4")
    path = await backend.local_path("videos/real.mp4")
    assert path is not None
    with open(path, "rb") as f:
        assert f.read() == b"bytes"


@pytest.mark.parametrize(
    "key",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "../outside.txt",
        "a/../../b.txt",
    ],
)
async def test_path_traversal_is_blocked(tmp_path, key: str) -> None:
    backend = LocalStorage(str(tmp_path), "")
    with pytest.raises(StorageError):
        await backend.upload(b"x", key, "text/plain")
