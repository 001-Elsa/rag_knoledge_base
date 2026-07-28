"""Object storage abstraction shared by API, workers, and cleanup jobs."""

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings


class StorageError(RuntimeError):
    pass


@dataclass
class StoredObject:
    key: str
    last_modified: datetime


class ObjectStorage:
    def put_file(self, object_key: str, source: Path) -> None:
        raise NotImplementedError

    def delete(self, object_key: str) -> None:
        raise NotImplementedError

    def list_objects(self) -> list[StoredObject]:
        raise NotImplementedError

    @contextmanager
    def materialize(self, object_key: str) -> Iterator[Path]:
        raise NotImplementedError


class LocalObjectStorage(ObjectStorage):
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, object_key: str) -> Path:
        legacy = Path(object_key)
        if legacy.is_absolute():
            return legacy.resolve()
        candidate = (self.root / object_key).resolve()
        if self.root not in candidate.parents:
            raise StorageError("invalid object key")
        return candidate

    def put_file(self, object_key: str, source: Path) -> None:
        destination = self._path(object_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)

    def delete(self, object_key: str) -> None:
        self._path(object_key).unlink(missing_ok=True)

    def list_objects(self) -> list[StoredObject]:
        objects = []
        for path in self.root.rglob("*"):
            if path.is_file() and ".staging" not in path.parts:
                objects.append(
                    StoredObject(
                        key=path.relative_to(self.root).as_posix(),
                        last_modified=datetime.fromtimestamp(
                            path.stat().st_mtime, timezone.utc  # noqa: UP017
                        ),
                    )
                )
        return objects

    @contextmanager
    def materialize(self, object_key: str) -> Iterator[Path]:
        path = self._path(object_key)
        if not path.is_file():
            raise StorageError(f"object does not exist: {object_key}")
        yield path


class S3ObjectStorage(ObjectStorage):
    def __init__(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise StorageError("boto3 is required when STORAGE_BACKEND=s3") from exc

        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            use_ssl=settings.s3_secure,
            config=Config(signature_version="s3v4"),
        )
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            self.client.create_bucket(Bucket=self.bucket)

    def put_file(self, object_key: str, source: Path) -> None:
        self.client.upload_file(str(source), self.bucket, object_key)
        source.unlink(missing_ok=True)

    def delete(self, object_key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=object_key)

    def list_objects(self) -> list[StoredObject]:
        objects = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket):
            objects.extend(
                StoredObject(
                    key=item["Key"],
                    last_modified=item["LastModified"],
                )
                for item in page.get("Contents", [])
            )
        return objects

    @contextmanager
    def materialize(self, object_key: str) -> Iterator[Path]:
        suffix = Path(object_key).suffix
        fd, raw_path = tempfile.mkstemp(prefix="rag-object-", suffix=suffix)
        os.close(fd)
        path = Path(raw_path)
        try:
            self.client.download_file(self.bucket, object_key, str(path))
            yield path
        finally:
            path.unlink(missing_ok=True)


_storage: ObjectStorage | None = None


def get_object_storage() -> ObjectStorage:
    global _storage
    if _storage is None:
        backend = settings.storage_backend.lower()
        if backend == "local":
            _storage = LocalObjectStorage(settings.upload_dir)
        elif backend in {"s3", "minio"}:
            _storage = S3ObjectStorage()
        else:
            raise StorageError(f"unsupported storage backend: {settings.storage_backend}")
    return _storage


def make_staging_file(suffix: str) -> Path:
    staging = Path(settings.upload_dir).resolve() / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=staging)
    os.close(fd)
    return Path(raw_path)


def copy_legacy_file_to_storage(filepath: str, object_key: str) -> None:
    """Migration helper for moving a legacy local file without modifying the source."""
    source = Path(filepath)
    staging = make_staging_file(source.suffix)
    shutil.copyfile(source, staging)
    get_object_storage().put_file(object_key, staging)
