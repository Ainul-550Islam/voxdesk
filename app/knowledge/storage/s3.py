"""
S3-compatible object storage (AWS S3, MinIO, Cloudflare R2, Spaces).

boto3 is imported lazily and only when this backend is selected, so a local
development install does not need it.

Credentials come from the environment or the instance role -- they are never
read from tenant data, never returned by the API, and never logged. There is
deliberately no method here that hands a browser a bucket URL: if pre-signed
downloads are added later they must be minted per request, scoped to one
authenticated tenant's own key.
"""
from __future__ import annotations

import asyncio

import structlog

from app.core.config import settings
from app.knowledge.storage.base import StorageError

log = structlog.get_logger()


class S3Storage:
    def __init__(
        self,
        bucket: str | None = None,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
    ):
        self.bucket = bucket or settings.knowledge_s3_bucket
        self.region = region or settings.knowledge_s3_region or None
        self.endpoint_url = endpoint_url or settings.knowledge_s3_endpoint_url or None
        if not self.bucket:
            raise StorageError("no S3 bucket configured")
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:      # pragma: no cover - deployment issue
                raise StorageError(
                    "boto3 is required for the s3 storage backend"
                ) from exc
            self._client = boto3.client(
                "s3", region_name=self.region, endpoint_url=self.endpoint_url
            )
        return self._client

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        extra = {"ContentType": content_type} if content_type else {}

        def _put() -> None:
            self._get_client().put_object(
                Bucket=self.bucket, Key=key, Body=data, **extra
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            # The message may name the bucket, never a credential.
            log.error("storage.s3_put_failed", key=key, error=str(exc))
            raise StorageError("could not store the object") from exc
        return key

    async def get(self, key: str) -> bytes:
        def _get() -> bytes:
            response = self._get_client().get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:
            raise StorageError(f"object not found: {key}") from exc

    async def delete(self, key: str) -> None:
        def _delete() -> None:
            self._get_client().delete_object(Bucket=self.bucket, Key=key)

        try:
            await asyncio.to_thread(_delete)
        except Exception as exc:
            # Deletion is best-effort by contract.
            log.warning("storage.s3_delete_failed", key=key, error=str(exc))

    async def exists(self, key: str) -> bool:
        def _head() -> bool:
            try:
                self._get_client().head_object(Bucket=self.bucket, Key=key)
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_head)
