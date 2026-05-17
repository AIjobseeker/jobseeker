"""MinIO document storage — upload generated files, return presigned URLs."""
from __future__ import annotations

import logging
from pathlib import Path

from minio import Minio
from minio.error import S3Error
from temporalio import activity

from shared.config import settings

log = logging.getLogger("worker.storage")

PRESIGN_EXPIRY_HOURS = 48


def _get_client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def _ensure_bucket(client: Minio) -> None:
    if not client.bucket_exists(settings.minio_bucket):
        client.make_bucket(settings.minio_bucket)
        log.info("Created MinIO bucket: %s", settings.minio_bucket)


@activity.defn
async def upload_document(local_path: str, object_name: str) -> str:
    """Upload a local file to MinIO. Returns a 48-hour presigned URL."""
    path = Path(local_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {local_path}")

    client = _get_client()
    _ensure_bucket(client)

    content_type = "application/octet-stream"
    suffix = path.suffix.lower()
    if suffix == ".docx":
        content_type = (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        )
    elif suffix == ".txt":
        content_type = "text/plain"
    elif suffix == ".pdf":
        content_type = "application/pdf"

    client.fput_object(
        settings.minio_bucket,
        object_name,
        str(path),
        content_type=content_type,
    )
    log.info("Uploaded %s → minio://%s/%s", path.name, settings.minio_bucket, object_name)

    from datetime import timedelta
    url = client.presigned_get_object(
        settings.minio_bucket,
        object_name,
        expires=timedelta(hours=PRESIGN_EXPIRY_HOURS),
    )
    return url
