import logging
from typing import Any

from .base import BaseInjector

logger = logging.getLogger(__name__)


class EmailPhishInjector(BaseInjector):
    name: str = "email_phish"
    description: str = "Delivers a simulated phishing email (sender, recipient, subject)."
    required_params: list[str] = []

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "injector": "email_phish",
            "sender": self.params.get("sender", "attacker@evil.com"),
            "recipient": self.params.get("recipient", "victim@corp.com"),
            "subject": self.params.get("subject", "Important Document"),
            "delivered": True,
        }
