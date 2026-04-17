"""Email phish inject — sends simulated phishing email."""

from __future__ import annotations

import logging

from . import Injector, InjectResult, RangeContext, register_injector

logger = logging.getLogger(__name__)


@register_injector
class EmailPhishInjector(Injector):
    action_name = "email_phish"

    def execute(self, params: dict, ctx: RangeContext) -> InjectResult:
        subject = params.get("subject", "Urgent: Password Reset Required")
        sender = params.get("sender", "helpdesk@corp.example.com")
        recipient = params.get("recipient", "user01@corp.example.com")
        params.get("payload_url", "http://evil.example.com/reset")

        logger.info(f"Sending phish: '{subject}' from {sender} to {recipient}")
        # Real mode: use SMTP to send email via range mail server
        return InjectResult(success=True, action="email_phish", detail=f"Phishing email sent to {recipient}")
