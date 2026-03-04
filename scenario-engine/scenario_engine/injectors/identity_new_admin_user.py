import logging
from typing import Any
from .base import BaseInjector

logger = logging.getLogger(__name__)


class IdentityNewAdminUserInjector(BaseInjector):
    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "injector": "identity_new_admin_user",
            "username": self.params.get("username", "rogue_admin"),
            "target": self.params.get("target", "dc-01"),
            "created": True,
        }