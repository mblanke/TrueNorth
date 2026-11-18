# Author: Marc-Andre Blanke
# Created: 2026-03-04T22:24:49Z
import logging
from typing import Any

from .base import BaseValidator

logger = logging.getLogger(__name__)


class ManualAckValidator(BaseValidator):
    def validate(self, context: dict[str, Any]) -> bool:
        return bool(context.get("manual_ack", False))
