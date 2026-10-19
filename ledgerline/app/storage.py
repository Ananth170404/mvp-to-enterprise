"""File storage behind a tiny interface.

v0 writes to local disk. Chapter 4 adds an S3 implementation with the same
two methods; nothing else in the app changes. Keys are prefixed by tenant so
a bug can't easily read another tenant's files, and per-tenant policies are
possible later.
"""

from pathlib import Path

from app.config import settings


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Invalid storage key")  # no ../ escapes
        return path


def document_key(tenant_id: str, sha256: str, extension: str) -> str:
    return f"tenants/{tenant_id}/documents/{sha256}{extension}"


storage = LocalStorage(settings.storage_dir)
