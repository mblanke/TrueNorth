"""DNS spike injector — generates burst of DNS queries."""

from __future__ import annotations

import logging
import random
import time

from . import Injector, InjectResult, RangeContext, register_injector

logger = logging.getLogger(__name__)


@register_injector
class DnsSpikeInjector(Injector):
    action_name = "dns_spike"

    def execute(self, params: dict, ctx: RangeContext) -> InjectResult:
        domains = params.get("domains", ["evil.example.com", "c2.malware.test", "exfil.bad.actor"])
        count = params.get("count", 100)
        target_ip = params.get("target_ip", ctx.vms[0]["ip"] if ctx.vms else "10.0.3.10")

        logger.info(f"Generating DNS spike: {count} queries to {len(domains)} domains from {target_ip}")

        # In real mode: use scapy or dnspython to send queries
        # For mock: generate telemetry events
        events = []
        for _i in range(count):
            domain = random.choice(domains)
            events.append(
                {
                    "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "event_type": "dns_query",
                    "source_ip": target_ip,
                    "query": domain,
                    "query_type": "A",
                    "inject": True,
                }
            )

        # TODO: ship events to OpenSearch via telemetry API
        return InjectResult(
            success=True,
            action="dns_spike",
            detail=f"Generated {count} DNS queries across {len(domains)} domains",
        )
