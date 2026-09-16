"""Object storage client wrapper, key conventions, and bucket bootstrapping.

Requirements:
- R5.8: Store large blobs (raw MIME, attachments, source documents) in MinIO/S3-compatible
  storage and reference them by object key.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from minio import Minio
from minio.error import S3Error

from packages.core.settings import AppSettings, ObjectStorageSettings

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Base exception for object storage operations."""


class ObjectNotFoundError(StorageError):
    """Raised when the requested object key does not exist."""


class BucketBootstrapError(StorageError):
    """Raised when bucket bootstrapping fails."""


class ObjectKeyBuilder:
    """Standardized deterministic object key generator enforcing tenant isolation."""

    @staticmethod
    def _clean_filename(filename: str) -> str:
        """Sanitize filename to prevent directory traversal and illegal characters."""
        base_name = Path(filename).name.strip()
        # Fallback to 'file' if empty
        clean = base_name if base_name else "file"
        # Prevent any path separators
        return clean.replace("/", "_").replace("\\", "_")

    @classmethod
    def raw_mime(
        cls,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        message_id: UUID | str,
    ) -> str:
        """Construct object key for raw inbound/outbound MIME payloads.

        Convention: raw/{organization_id}/{mailbox_id}/{message_id}.eml
        """
        return f"raw/{organization_id}/{mailbox_id}/{message_id}.eml"

    @classmethod
    def html_body(
        cls,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        message_id: UUID | str,
    ) -> str:
        """Construct object key for parsed HTML email body.

        Convention: html/{organization_id}/{mailbox_id}/{message_id}.html
        """
        return f"html/{organization_id}/{mailbox_id}/{message_id}.html"

    @classmethod
    def attachment(
        cls,
        organization_id: UUID | str,
        message_id: UUID | str,
        attachment_id: UUID | str,
        filename: str,
    ) -> str:
        """Construct object key for an email attachment.

        Convention: attachments/{organization_id}/{message_id}/{attachment_id}/{clean_filename}
        """
        clean_name = cls._clean_filename(filename)
        return f"attachments/{organization_id}/{message_id}/{attachment_id}/{clean_name}"

    @classmethod
    def knowledge_doc(
        cls,
        organization_id: UUID | str,
        document_id: UUID | str,
        version: int,
        filename: str,
    ) -> str:
        """Construct object key for an uploaded source knowledge document.

        Convention: knowledge/{organization_id}/{document_id}/v{version}/{clean_filename}
        """
        clean_name = cls._clean_filename(filename)
        return f"knowledge/{organization_id}/{document_id}/v{version}/{clean_name}"


@runtime_checkable
class StorageProtocol(Protocol):
    """Protocol defining asynchronous object storage operations."""

    async def bootstrap_buckets(self) -> list[str]:
        """Ensure all required buckets exist. Return list of bootstrapped buckets."""
        ...

    async def put_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        """Upload bytes to bucket with key and return object key."""
        ...

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        """Download object content as bytes. Raise ObjectNotFoundError if missing."""
        ...

    async def delete_object(self, bucket: str, key: str) -> None:
        """Delete object from bucket."""
        ...

    async def object_exists(self, bucket: str, key: str) -> bool:
        """Return True if object exists in bucket, False otherwise."""
        ...

    async def get_object_metadata(self, bucket: str, key: str) -> dict[str, Any]:
        """Retrieve metadata for object. Raise ObjectNotFoundError if missing."""
        ...

    async def get_presigned_url(
        self,
        bucket: str,
        key: str,
        expires_seconds: int = 3600,
    ) -> str:
        """Generate a presigned GET URL for downloading an object."""
        ...


class MinioObjectStorageClient:
    """MinIO / S3 compatible asynchronous storage client (R5.8)."""

    def __init__(self, settings: ObjectStorageSettings | None = None) -> None:
        self.settings = settings or AppSettings().object_storage
        self.endpoint = self._normalize_endpoint(self.settings.endpoint)
        self._client = Minio(
            endpoint=self.endpoint,
            access_key=self.settings.access_key,
            secret_key=self.settings.secret_key,
            secure=self.settings.secure,
            region=self.settings.region,
        )

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        """Strip http:// or https:// prefix if present."""
        if endpoint.startswith("http://"):
            return endpoint[len("http://") :]
        if endpoint.startswith("https://"):
            return endpoint[len("https://") :]
        return endpoint

    @property
    def configured_buckets(self) -> list[str]:
        """Return the list of core application buckets defined in settings."""
        return [
            self.settings.bucket_raw_mime,
            self.settings.bucket_attachments,
            self.settings.bucket_knowledge,
        ]

    async def bootstrap_buckets(self) -> list[str]:
        """Ensure all configured buckets exist, creating any that are missing."""
        created: list[str] = []

        def _sync_bootstrap() -> list[str]:
            for bucket in self.configured_buckets:
                try:
                    if not self._client.bucket_exists(bucket):
                        logger.info("Creating bucket: %s", bucket)
                        self._client.make_bucket(bucket)
                        created.append(bucket)
                    else:
                        logger.debug("Bucket already exists: %s", bucket)
                except Exception as err:
                    raise BucketBootstrapError(
                        f"Failed to bootstrap bucket '{bucket}': {err}"
                    ) from err
            return created

        return await asyncio.to_thread(_sync_bootstrap)

    async def put_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        """Upload raw bytes to an object key asynchronously."""
        data_stream = io.BytesIO(data)
        length = len(data)

        def _sync_put() -> str:
            try:
                self._client.put_object(
                    bucket_name=bucket,
                    object_name=key,
                    data=data_stream,
                    length=length,
                    content_type=content_type,
                    metadata=dict(metadata) if metadata else None,
                )
                logger.debug("Uploaded object %s/%s (%d bytes)", bucket, key, length)
                return key
            except Exception as err:
                raise StorageError(f"Failed to upload object {bucket}/{key}: {err}") from err

        return await asyncio.to_thread(_sync_put)

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        """Download an object's complete payload as bytes."""

        def _sync_get() -> bytes:
            try:
                response = self._client.get_object(bucket, key)
                try:
                    return response.read()
                finally:
                    response.close()
                    response.release_conn()
            except S3Error as err:
                if err.code in ("NoSuchKey", "ResourceNotFound", "NoSuchBucket"):
                    raise ObjectNotFoundError(
                        f"Object '{key}' not found in bucket '{bucket}'"
                    ) from err
                raise StorageError(f"Error retrieving object {bucket}/{key}: {err}") from err
            except Exception as err:
                raise StorageError(f"Unexpected error retrieving {bucket}/{key}: {err}") from err

        return await asyncio.to_thread(_sync_get)

    async def delete_object(self, bucket: str, key: str) -> None:
        """Remove an object from the specified bucket."""

        def _sync_delete() -> None:
            try:
                self._client.remove_object(bucket, key)
                logger.debug("Deleted object %s/%s", bucket, key)
            except Exception as err:
                raise StorageError(f"Failed to delete object {bucket}/{key}: {err}") from err

        await asyncio.to_thread(_sync_delete)

    async def object_exists(self, bucket: str, key: str) -> bool:
        """Check if an object exists in the specified bucket."""

        def _sync_exists() -> bool:
            try:
                self._client.stat_object(bucket, key)
                return True
            except S3Error as err:
                if err.code in ("NoSuchKey", "ResourceNotFound", "NoSuchBucket"):
                    return False
                raise StorageError(f"Error checking existence for {bucket}/{key}: {err}") from err
            except Exception as err:
                raise StorageError(f"Unexpected error checking {bucket}/{key}: {err}") from err

        return await asyncio.to_thread(_sync_exists)

    async def get_object_metadata(self, bucket: str, key: str) -> dict[str, Any]:
        """Fetch metadata attributes for an object."""

        def _sync_stat() -> dict[str, Any]:
            try:
                stat = self._client.stat_object(bucket, key)
                return {
                    "size": stat.size,
                    "content_type": stat.content_type,
                    "etag": stat.etag,
                    "last_modified": stat.last_modified,
                    "metadata": stat.metadata,
                }
            except S3Error as err:
                if err.code in ("NoSuchKey", "ResourceNotFound", "NoSuchBucket"):
                    raise ObjectNotFoundError(
                        f"Object '{key}' not found in bucket '{bucket}'"
                    ) from err
                raise StorageError(f"Failed to stat object {bucket}/{key}: {err}") from err
            except Exception as err:
                raise StorageError(f"Unexpected error stating {bucket}/{key}: {err}") from err

        return await asyncio.to_thread(_sync_stat)

    async def get_presigned_url(
        self,
        bucket: str,
        key: str,
        expires_seconds: int = 3600,
    ) -> str:
        """Generate presigned GET URL for secure direct client download."""

        def _sync_presign() -> str:
            try:
                return self._client.get_presigned_url(
                    method="GET",
                    bucket_name=bucket,
                    object_name=key,
                    expires=timedelta(seconds=expires_seconds),
                )
            except Exception as err:
                raise StorageError(
                    f"Failed to generate presigned URL for {bucket}/{key}: {err}"
                ) from err

        return await asyncio.to_thread(_sync_presign)


class FakeObjectStorageClient:
    """In-memory object storage client for zero-network unit tests (GEMINI.md §8)."""

    def __init__(self, settings: ObjectStorageSettings | None = None) -> None:
        self.settings = settings or AppSettings().object_storage
        self.buckets: dict[str, dict[str, tuple[bytes, str, dict[str, str]]]] = {
            self.settings.bucket_raw_mime: {},
            self.settings.bucket_attachments: {},
            self.settings.bucket_knowledge: {},
        }

    async def bootstrap_buckets(self) -> list[str]:
        """Bootstrap configured buckets in-memory."""
        created: list[str] = []
        for b in [
            self.settings.bucket_raw_mime,
            self.settings.bucket_attachments,
            self.settings.bucket_knowledge,
        ]:
            if b not in self.buckets:
                self.buckets[b] = {}
                created.append(b)
        return created

    async def put_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        """Save bytes in memory."""
        if bucket not in self.buckets:
            self.buckets[bucket] = {}
        self.buckets[bucket][key] = (data, content_type, dict(metadata) if metadata else {})
        return key

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        """Retrieve bytes from memory."""
        if bucket not in self.buckets or key not in self.buckets[bucket]:
            raise ObjectNotFoundError(f"Object '{key}' not found in bucket '{bucket}'")
        return self.buckets[bucket][key][0]

    async def delete_object(self, bucket: str, key: str) -> None:
        """Delete object from memory."""
        if bucket in self.buckets and key in self.buckets[bucket]:
            del self.buckets[bucket][key]

    async def object_exists(self, bucket: str, key: str) -> bool:
        """Check if object exists in memory."""
        return bucket in self.buckets and key in self.buckets[bucket]

    async def get_object_metadata(self, bucket: str, key: str) -> dict[str, Any]:
        """Return stored object metadata."""
        if bucket not in self.buckets or key not in self.buckets[bucket]:
            raise ObjectNotFoundError(f"Object '{key}' not found in bucket '{bucket}'")
        data, content_type, metadata = self.buckets[bucket][key]
        return {
            "size": len(data),
            "content_type": content_type,
            "etag": "fake-etag",
            "last_modified": None,
            "metadata": metadata,
        }

    async def get_presigned_url(
        self,
        bucket: str,
        key: str,
        expires_seconds: int = 3600,
    ) -> str:
        """Return a simulated presigned URL."""
        if not await self.object_exists(bucket, key):
            raise ObjectNotFoundError(f"Object '{key}' not found in bucket '{bucket}'")
        return f"http://fake-storage/{bucket}/{key}?expires={expires_seconds}"


def get_storage_client(
    settings: ObjectStorageSettings | None = None,
    fake: bool = False,
) -> StorageProtocol:
    """Factory creating configured real or fake storage client."""
    if fake:
        return FakeObjectStorageClient(settings)
    return MinioObjectStorageClient(settings)


def main() -> None:
    """CLI tool for object storage administration."""
    parser = argparse.ArgumentParser(description="Object Storage Management CLI (R5.8)")
    parser.add_argument("command", choices=["bootstrap", "list-buckets"])
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    client = MinioObjectStorageClient()

    if args.command == "bootstrap":
        created = asyncio.run(client.bootstrap_buckets())
        if created:
            print(f"Bootstrapped new buckets: {', '.join(created)}")
        else:
            print("All configured buckets already exist.")
        print(f"Active buckets: {', '.join(client.configured_buckets)}")

    elif args.command == "list-buckets":
        print(f"Configured buckets: {', '.join(client.configured_buckets)}")


if __name__ == "__main__":
    main()
