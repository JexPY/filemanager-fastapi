"""Test doubles for app.services.storage.StorageBackend."""

from __future__ import annotations

from app.services.storage import StorageBackend, StorageError, StorageObject


class InMemoryStorageBackend(StorageBackend):
    """Dict-backed fake. Can simulate either an S3-like (presigning-capable)
    or a local/GCS-like (non-presigning) backend via `presign_capable`.
    """

    def __init__(
        self, base_url: str = "http://fake-storage", presign_capable: bool = False
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._presign_capable = presign_capable
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.deleted_keys: list[str] = []
        self.closed = False

    async def upload(self, data: bytes, key: str, content_type: str) -> StorageObject:
        self.objects[key] = data
        self.content_types[key] = content_type
        return StorageObject(
            key=key, url=f"{self._base_url}/{key}", size=len(data), content_type=content_type
        )

    async def download(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise StorageError(f"Object not found: {key!r}") from exc

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.deleted_keys.append(key)

    async def presigned_get_url(self, key: str, expires_in: int = 3600) -> str | None:
        if not self._presign_capable:
            return None
        return f"{self._base_url}/{key}?X-Amz-Signature=fake&expires={expires_in}"

    async def aclose(self) -> None:
        self.closed = True
