"""HTTP burst injector — generates burst of HTTP requests."""

from __future__ import annotations

import logging

from . import Injector, InjectResult, RangeContext, register_injector

logger = logging.getLogger(__name__)


@register_injector
class HttpBurstInjector(Injector):
    action_name = "http_burst"

    def execute(self, params: dict, ctx: RangeContext) -> InjectResult:
        target_url = params.get("url", "http://10.0.2.10/admin")
        count = params.get("count", 50)
        method = params.get("method", "GET")

        logger.info(f"HTTP burst: {count} {method} requests to {target_url}")

        # In real mode: use httpx/aiohttp to send actual requests
        # Mock mode: log and generate events
        return InjectResult(
            success=True,
            action="http_burst",
            detail=f"Sent {count} {method} requests to {target_url}",
        )
