"""TrueNorth Range - Network scanning injector.

Simulates network reconnaissance activities:
  - Port scanning (Nmap-like)
  - Network discovery (ARP scan)
  - Service enumeration
"""
from __future__ import annotations

import logging
import random
from typing import Any

from .base import BaseInjector

logger = logging.getLogger(__name__)

# Common service banners for realism
_SERVICE_BANNERS: dict[int, dict[str, str]] = {
    21: {"service": "ftp", "banner": "220 ProFTPD 1.3.7 Server"},
    22: {"service": "ssh", "banner": "SSH-2.0-OpenSSH_8.9p1 Ubuntu-3"},
    25: {"service": "smtp", "banner": "220 mail.corp.truenorth.local ESMTP Postfix"},
    53: {"service": "dns", "banner": ""},
    80: {"service": "http", "banner": "HTTP/1.1 200 OK\r\nServer: nginx/1.22.0"},
    88: {"service": "kerberos", "banner": ""},
    110: {"service": "pop3", "banner": "+OK POP3 server ready"},
    135: {"service": "msrpc", "banner": ""},
    139: {"service": "netbios-ssn", "banner": ""},
    143: {"service": "imap", "banner": "* OK IMAP4rev1 Server"},
    389: {"service": "ldap", "banner": ""},
    443: {"service": "https", "banner": "HTTP/1.1 200 OK\r\nServer: Apache/2.4.54"},
    445: {"service": "microsoft-ds", "banner": ""},
    636: {"service": "ldaps", "banner": ""},
    1433: {"service": "ms-sql-s", "banner": ""},
    3306: {"service": "mysql", "banner": "5.7.42-log"},
    3389: {"service": "ms-wbt-server", "banner": ""},
    5432: {"service": "postgresql", "banner": ""},
    5985: {"service": "wsman", "banner": ""},
    8080: {"service": "http-proxy", "banner": "HTTP/1.1 200 OK"},
    8443: {"service": "https-alt", "banner": ""},
}


class NetworkScanInjector(BaseInjector):
    """Inject network scanning simulations into a range exercise."""

    name: str = "network_scan"
    description: str = (
        "Simulates network reconnaissance: port scanning, ARP discovery, "
        "and service enumeration."
    )
    required_params: list[str] = ["scan_type", "target_network"]

    _SCAN_TYPES = ("port_scan", "arp_discovery", "service_enum")

    def validate_params(self) -> None:
        for p in self.required_params:
            assert p in self.params, f"Missing required param: {p}"
        scan_type = self.params["scan_type"]
        assert scan_type in self._SCAN_TYPES, (
            f"Unknown scan_type '{scan_type}'. Valid: {list(self._SCAN_TYPES)}"
        )

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        self.validate_params()
        scan_type = self.params["scan_type"]
        handler = {
            "port_scan": self._port_scan,
            "arp_discovery": self._arp_discovery,
            "service_enum": self._service_enum,
        }[scan_type]
        result = handler(context)
        result.update({
            "injector": self.name,
            "scan_type": scan_type,
            "target_network": self.params["target_network"],
        })
        logger.info(
            "Network scan '%s' executed against %s",
            scan_type, self.params["target_network"],
        )
        return result

    # ------------------------------------------------------------------
    # Scan simulations
    # ------------------------------------------------------------------

    def _port_scan(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate an Nmap-style port scan."""
        target_ip = self.params.get("target_ip", "10.50.101.10")
        ports = self.params.get("ports", list(_SERVICE_BANNERS.keys()))
        scan_technique = self.params.get("technique", "SYN")

        open_ports: list[dict[str, Any]] = []
        closed = 0
        filtered = 0

        for port in ports:
            roll = random.random()
            if port in _SERVICE_BANNERS and roll < 0.7:
                svc = _SERVICE_BANNERS[port]
                open_ports.append({
                    "port": port,
                    "state": "open",
                    "service": svc["service"],
                    "protocol": "tcp",
                })
            elif roll < 0.85:
                filtered += 1
            else:
                closed += 1

        return {
            "target_ip": target_ip,
            "technique": f"{scan_technique} scan",
            "ports_scanned": len(ports),
            "open": open_ports,
            "closed_count": closed,
            "filtered_count": filtered,
            "tool": "Nmap",
            "technique_id": "T1046",
        }

    def _arp_discovery(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate ARP-based network host discovery."""
        network = self.params["target_network"]
        # Parse base from CIDR (e.g., "10.50.101.0/24")
        base = network.rsplit(".", 1)[0]
        host_count = int(self.params.get("expected_hosts", 20))

        hosts: list[dict[str, str]] = []
        for i in range(1, host_count + 1):
            ip = f"{base}.{i + 9}"
            mac = ":".join(f"{random.randint(0, 255):02x}" for _ in range(6))
            vendor = random.choice(["VMware", "Microsoft", "Dell", "Intel", "Proxmox"])
            hosts.append({"ip": ip, "mac": mac, "vendor": vendor})

        return {
            "hosts_discovered": len(hosts),
            "hosts": hosts,
            "tool": "arp-scan / nmap -sn",
            "technique_id": "T1018",
        }

    def _service_enum(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate detailed service version enumeration."""
        target_ip = self.params.get("target_ip", "10.50.101.10")
        ports = self.params.get("ports", [22, 80, 443, 445, 3389])

        services: list[dict[str, Any]] = []
        for port in ports:
            svc_info = _SERVICE_BANNERS.get(port, {"service": "unknown", "banner": ""})
            version = f"{svc_info['service']}/{random.randint(1, 9)}.{random.randint(0, 9)}"
            services.append({
                "port": port,
                "service": svc_info["service"],
                "version": version,
                "banner": svc_info["banner"] or "(no banner)",
                "cpe": f"cpe:/a:{svc_info['service']}:{svc_info['service']}",
            })

        return {
            "target_ip": target_ip,
            "services_enumerated": len(services),
            "services": services,
            "tool": "Nmap -sV",
            "technique_id": "T1046",
        }