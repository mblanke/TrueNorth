import logging
from typing import Any

from .base import BaseInjector

logger = logging.getLogger(__name__)


class EmailPhishInjector(BaseInjector):
    name: str = "email_phish"
    description: str = "Builds a synthetic phishing-email record (sender, recipient, subject); sends no mail."
    required_params: list[str] = []

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "injector": "email_phish",
            "sender": self.params.get("sender", "attacker@evil.com"),
            "recipient": self.params.get("recipient", "victim@corp.com"),
            "subject": self.params.get("subject", "Important Document"),
            # Simulated: no mail server was contacted, so nothing was delivered.
            "delivered": False,
        }
