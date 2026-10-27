"""File storage behind a two-method interface: put() and get().

Local disk in development; S3 when deployed, because container disks are
thrown away on every deploy. Nothing outside this file knows the difference.
Keys are prefixed by tenant, so a bug can't easily read another tenant's
files, and per-tenant policies are possible later.
"""

from pathlib import Path
from typing import Protocol

from app.config import settings


class Storage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Invalid storage key")  # no ../ escapes
        return path


class S3Storage:
    def __init__(self, bucket: str, region: str | None = None, client=None):
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=region)
        self.bucket = bucket
        self.client = client

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        # Encryption at rest is also enforced by the bucket's default settings;
        # asking for it here keeps it explicit.
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, ServerSideEncryption="AES256"
        )

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise


def document_key(tenant_id: str, sha256: str, extension: str) -> str:
    return f"tenants/{tenant_id}/documents/{sha256}{extension}"


def build_storage() -> Storage:
    if settings.storage_backend == "s3":
        return S3Storage(settings.s3_bucket or "", settings.aws_region)
    return LocalStorage(settings.storage_dir)


storage: Storage = build_storage()
