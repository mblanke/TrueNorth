"""TrueNorth Range - Command & Control beacon injector.

Simulates C2 communication patterns for training scenarios:
  - HTTP C2 beacon (Cobalt Strike-like)
  - DNS C2 tunnel
  - HTTPS C2 with jitter
"""

from __future__ import annotations

import base64
import hashlib
import logging
import math
import random
import time
from typing import Any

from .base import BaseInjector

logger = logging.getLogger(__name__)


class C2BeaconInjector(BaseInjector):
    """Inject Command & Control beacon simulations into a range exercise."""

    name: str = "c2_beacon"
    description: str = (
        "Simulates C2 communication patterns: HTTP beaconing, DNS tunnelling, and HTTPS C2 with configurable jitter."
    )
    required_params: list[str] = ["c2_type", "target_host"]

    _C2_TYPES = ("http_beacon", "dns_tunnel", "https_jitter")

    # C2 profile defaults
    _DEFAULT_HTTP_C2 = "http://c2.evil-corp.io:8080"
    _DEFAULT_DNS_C2 = "data.evil-corp.io"
    _DEFAULT_HTTPS_C2 = "https://cdn-static.evil-corp.io:443"

    def validate_params(self) -> None:
        for p in self.required_params:
            assert p in self.params, f"Missing required param: {p}"
        c2_type = self.params["c2_type"]
        assert c2_type in self._C2_TYPES, f"Unknown c2_type '{c2_type}'. Valid: {list(self._C2_TYPES)}"

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        self.validate_params()
        c2_type = self.params["c2_type"]
        handler = {
            "http_beacon": self._http_beacon,
            "dns_tunnel": self._dns_tunnel,
            "https_jitter": self._https_jitter,
        }[c2_type]
        result = handler(context)
        result.update(
            {
                "injector": self.name,
                "c2_type": c2_type,
                "target_host": self.params["target_host"],
                "technique_id": self._technique_for(c2_type),
            }
        )
        logger.info(
            "C2 beacon '%s' simulated from %s",
            c2_type,
            self.params["target_host"],
        )
        return result

    @staticmethod
    def _technique_for(c2_type: str) -> str:
        return {
            "http_beacon": "T1071.001",
            "dns_tunnel": "T1071.004",
            "https_jitter": "T1071.001",
        }.get(c2_type, "T1071")

    # ------------------------------------------------------------------
    # C2 simulations
    # ------------------------------------------------------------------

    def _http_beacon(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate HTTP C2 beaconing (Cobalt Strike-style)."""
        c2_server = self.params.get("c2_server", self._DEFAULT_HTTP_C2)
        beacon_interval = int(self.params.get("interval_seconds", 60))
        num_callbacks = int(self.params.get("num_callbacks", 10))
        sleep_jitter = float(self.params.get("jitter_pct", 0.0))

        beacon_id = hashlib.sha256(f"{self.params['target_host']}:{random.random()}".encode()).hexdigest()[:16]

        callbacks: list[dict[str, Any]] = []
        ts = time.time()
        for i in range(num_callbacks):
            jitter = beacon_interval * sleep_jitter * (random.random() - 0.5) * 2
            actual_interval = max(1, beacon_interval + jitter)
            ts += actual_interval

            # Simulate different HTTP methods / URIs
            uri = random.choice(
                [
                    "/api/v1/status",
                    "/updates/check",
                    "/content/load",
                    "/__utm.gif",
                    "/pixel.png",
                    "/jquery-3.6.0.min.js",
                ]
            )
            method = random.choice(["GET", "GET", "GET", "POST"])
            response_code = random.choices([200, 204, 302], weights=[85, 10, 5])[0]

            payload_size = random.randint(64, 4096) if method == "POST" else 0
            response_size = random.randint(128, 8192)

            callbacks.append(
                {
                    "seq": i + 1,
                    "timestamp_epoch": round(ts, 2),
                    "method": method,
                    "uri": uri,
                    "status": response_code,
                    "request_bytes": payload_size,
                    "response_bytes": response_size,
                    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) rv:109.0",
                }
            )

        return {
            "c2_server": c2_server,
            "beacon_id": beacon_id,
            "beacon_interval_s": beacon_interval,
            "jitter_pct": sleep_jitter,
            "total_callbacks": num_callbacks,
            "callbacks": callbacks,
            "tool": "Cobalt Strike (simulated)",
        }

    def _dns_tunnel(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate DNS C2 tunnelling (iodine / dnscat2 style)."""
        c2_domain = self.params.get("c2_domain", self._DEFAULT_DNS_C2)
        num_queries = int(self.params.get("num_queries", 20))
        data_to_exfil = self.params.get("exfil_data", "Simulated credential dump output")

        # Encode data into DNS-safe subdomains
        encoded = base64.b32encode(data_to_exfil.encode()).decode().lower().rstrip("=")
        chunk_size = 63  # Max DNS label length
        chunks = [encoded[i : i + chunk_size] for i in range(0, len(encoded), chunk_size)]

        queries: list[dict[str, Any]] = []
        for i in range(num_queries):
            chunk = chunks[i % len(chunks)] if chunks else "ping"
            query_type = random.choice(["TXT", "CNAME", "A", "MX"])
            fqdn = f"{chunk}.{c2_domain}"
            queries.append(
                {
                    "seq": i + 1,
                    "query_type": query_type,
                    "fqdn": fqdn,
                    "response": self._fake_dns_response(query_type),
                    "data_bytes": len(chunk),
                }
            )

        total_data = sum(q["data_bytes"] for q in queries)
        return {
            "c2_domain": c2_domain,
            "total_queries": num_queries,
            "total_data_bytes": total_data,
            "queries": queries,
            "encoding": "base32",
            "tool": "iodine / dnscat2 (simulated)",
        }

    def _https_jitter(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate HTTPS C2 with randomised jitter to evade detection."""
        c2_server = self.params.get("c2_server", self._DEFAULT_HTTPS_C2)
        base_interval = int(self.params.get("interval_seconds", 300))
        jitter_pct = float(self.params.get("jitter_pct", 0.40))
        num_callbacks = int(self.params.get("num_callbacks", 15))

        beacon_id = hashlib.sha256(f"{self.params['target_host']}:https:{random.random()}".encode()).hexdigest()[:16]

        # TLS fingerprint simulation (JA3)
        ja3_hash = hashlib.md5(
            b"771,4865-4866-4867-49195-49199,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-17513,29-23-24,0"
        ).hexdigest()

        callbacks: list[dict[str, Any]] = []
        ts = time.time()
        for i in range(num_callbacks):
            # Apply jitter: interval * (1 +/- jitter_pct * random)
            jitter_factor = 1.0 + jitter_pct * (random.random() * 2 - 1)
            actual_interval = max(10, base_interval * jitter_factor)
            ts += actual_interval

            # Vary URIs to look like CDN/API traffic
            uri = random.choice(
                [
                    "/api/v2/config",
                    "/cdn/assets/main.js",
                    "/telemetry/collect",
                    "/health",
                    "/static/fonts/roboto.woff2",
                    "/media/thumb_placeholder.jpg",
                ]
            )

            encrypted_payload = base64.b64encode(f"task_response_{i}:{random.random()}".encode()).decode()

            callbacks.append(
                {
                    "seq": i + 1,
                    "timestamp_epoch": round(ts, 2),
                    "actual_interval_s": round(actual_interval, 1),
                    "uri": uri,
                    "tls_version": "TLSv1.3",
                    "ja3_hash": ja3_hash,
                    "encrypted_payload_b64": encrypted_payload[:32] + "...",
                    "response_bytes": random.randint(256, 16384),
                }
            )

        intervals = [c["actual_interval_s"] for c in callbacks]
        avg_interval = sum(intervals) / len(intervals) if intervals else 0
        std_dev = math.sqrt(sum((x - avg_interval) ** 2 for x in intervals) / len(intervals)) if intervals else 0

        return {
            "c2_server": c2_server,
            "beacon_id": beacon_id,
            "base_interval_s": base_interval,
            "jitter_pct": jitter_pct,
            "total_callbacks": num_callbacks,
            "avg_interval_s": round(avg_interval, 1),
            "interval_std_dev": round(std_dev, 1),
            "ja3_hash": ja3_hash,
            "callbacks": callbacks,
            "tool": "Custom HTTPS implant (simulated)",
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fake_dns_response(query_type: str) -> str:
        """Generate a plausible DNS response for tunnel simulation."""
        return {
            "A": f"127.0.0.{random.randint(1, 254)}",
            "CNAME": f"cdn-{random.randint(1, 99)}.cloudfront.net",
            "TXT": base64.b64encode(f"resp:{random.random()}".encode()).decode()[:40],
            "MX": f"mail{random.randint(1, 5)}.evil-corp.io",
        }.get(query_type, "NXDOMAIN")
