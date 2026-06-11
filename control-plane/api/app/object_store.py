"""TrueNorth Range — MinIO/S3 object storage helper.

Used by Curriculum Forge for uploaded courseware. Configuration:
    MINIO_ENDPOINT    — e.g. http://minio:9000 (default)
    MINIO_ACCESS_KEY  — default: minioadmin
    MINIO_SECRET_KEY  — default: minioadmin
    MINIO_SECURE      — "true" to use TLS (default false)
"""

from __future__ import annotations

import io
import logging
import os
from urllib.parse import urlparse

from minio import Minio

logger = logging.getLogger("truenorth.api.object_store")

CURRICULUM_BUCKET = "curriculum"


def _client() -> Minio:
    endpoint = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
    parsed = urlparse(endpoint if "//" in endpoint else f"http://{endpoint}")
    secure = parsed.scheme == "https" or os.getenv("MINIO_SECURE", "false").lower() in ("1", "true")
    return Minio(
        parsed.netloc or parsed.path,
        access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
        secure=secure,
    )


def ensure_bucket(bucket: str = CURRICULUM_BUCKET) -> None:
    client = _client()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        logger.info("Created MinIO bucket %s", bucket)


def put_object(key: str, data: bytes, content_type: str, bucket: str = CURRICULUM_BUCKET) -> None:
    ensure_bucket(bucket)
    _client().put_object(
        bucket,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type or "application/octet-stream",
    )


def get_object(key: str, bucket: str = CURRICULUM_BUCKET) -> bytes:
    response = _client().get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def delete_object(key: str, bucket: str = CURRICULUM_BUCKET) -> None:
    _client().remove_object(bucket, key)
