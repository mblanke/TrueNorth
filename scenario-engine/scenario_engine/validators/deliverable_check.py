import logging
from typing import Any
from .base import BaseValidator

logger = logging.getLogger(__name__)


class DeliverableCheckValidator(BaseValidator):
    def validate(self, context: dict[str, Any]) -> bool:
        key = self.params.get("key", "")
        bucket = self.params.get("bucket", "truenorth-deliverables")
        minio_client = context.get("minio_client")
        if minio_client is None:
            logger.warning("No MinIO client in context")
            return False
        try:
            stat = minio_client.stat_object(bucket, key)
            return stat is not None
        except Exception:
            return False