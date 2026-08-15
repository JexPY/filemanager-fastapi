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
