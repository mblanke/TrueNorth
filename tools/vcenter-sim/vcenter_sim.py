"""A read-only fake of the vCenter Automation REST API (/api), for dashboards on a site
with no vCenter. Lab and staging only.

govmomi's vcsim covers the SOAP SDK but not the /api endpoints TrueNorth's hypervisor
dashboards (control-plane/api/app/hypervisor_backends/vsphere.py) and the installer's
tn_vsphere role read, so this serves exactly those, from a fixed inventory:

    POST/DELETE /api/session                 any username/password
    GET  /api/vcenter/system/version
    GET  /api/vcenter/{host,vm,datacenter,cluster,datastore,network}  (?names= ?hosts= ?types=)
    POST /api/vcenter/vm                     403: it builds nothing

Standard library only. TLS with the certificate and key given on the command line:

    python3 vcenter_sim.py --cert cert.pem --key key.pem --port 8989
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = {"version": "8.0.3", "build": "24322831", "product": "VMware vCenter Server (simulated)"}

HOSTS = [
    {"host": f"host-{i}", "name": f"10.10.0.{10 + i}", "connection_state": state, "power_state": power}
    for i, state, power in [
        (1, "CONNECTED", "POWERED_ON"),
        (2, "CONNECTED", "POWERED_ON"),
        (3, "CONNECTED", "POWERED_ON"),
        (4, "DISCONNECTED", "POWERED_OFF"),
    ]
]

_VM_NAMES = {
    "host-1": ["tn-dc01", "tn-web01", "tn-sql01", "tn-ws01", "tn-ws02"],
    "host-2": ["tn-kali01", "tn-kali02", "tn-siem01", "tn-fw01"],
    "host-3": ["tn-ws03", "tn-ws04", "tn-file01"],
    "host-4": [],
}
VMS = [
    {
        "vm": f"vm-{n}",
        "name": name,
        "power_state": "POWERED_OFF" if name.endswith("04") else "POWERED_ON",
        "cpu_count": 2,
        "memory_size_MiB": 4096,
        "_host": host,
    }
    for n, (host, name) in enumerate(((h, v) for h, vs in _VM_NAMES.items() for v in vs), start=100)
]

OBJECTS = {
    "datacenter": [{"datacenter": "datacenter-1", "name": "DC0"}],
    "cluster": [{"cluster": "domain-c1", "name": "DC0_C0", "ha_enabled": True, "drs_enabled": True}],
    "datastore": [
        {
            "datastore": "datastore-1",
            "name": "LocalDS_0",
            "type": "VMFS",
            "capacity": 4 * 2**40,
            "free_space": 3 * 2**40,
        },
        {
            "datastore": "datastore-2",
            "name": "LocalDS_1",
            "type": "VMFS",
            "capacity": 4 * 2**40,
            "free_space": 2 * 2**40,
        },
    ],
    "network": [
        {"network": "network-1", "name": "VM Network", "type": "STANDARD_PORTGROUP"},
        {"network": "dvportgroup-1", "name": "dPG-Range-Uplink", "type": "DISTRIBUTED_PORTGROUP"},
    ],
}

SESSIONS: set[str] = set()


class Handler(BaseHTTPRequestHandler):
    server_version = "vcenter-sim"

    def _send(self, status: int, body=None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        return self.headers.get("vmware-api-session-id", "") in SESSIONS

    def do_POST(self) -> None:  # noqa: N802 (http.server's naming)
        path = urlparse(self.path).path
        if path == "/api/session":
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic ") or ":" not in base64.b64decode(auth[6:]).decode(errors="replace"):
                return self._send(401, {"error_type": "UNAUTHENTICATED"})
            token = secrets.token_hex(16)
            SESSIONS.add(token)
            return self._send(201, token)
        if not self._authed():
            return self._send(401, {"error_type": "UNAUTHENTICATED"})
        if path == "/api/vcenter/vm":
            return self._send(403, {"error_type": "UNAUTHORIZED", "messages": ["simulated vCenter: read-only"]})
        return self._send(404, {"error_type": "NOT_FOUND"})

    def do_DELETE(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/api/session":
            SESSIONS.discard(self.headers.get("vmware-api-session-id", ""))
            return self._send(204)
        return self._send(404, {"error_type": "NOT_FOUND"})

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/about":
            return self._send(200, VERSION)
        if not self._authed():
            return self._send(401, {"error_type": "UNAUTHENTICATED"})
        query = {k: ",".join(v).split(",") for k, v in parse_qs(url.query).items()}
        kind = url.path.removeprefix("/api/vcenter/").rstrip("/")
        if kind == "system/version":
            return self._send(200, VERSION)
        if kind == "host":
            return self._send(200, _filter(HOSTS, query))
        if kind == "vm":
            vms = [v for v in VMS if "hosts" not in query or v["_host"] in query["hosts"]]
            return self._send(
                200, [{k: v for k, v in vm.items() if not k.startswith("_")} for vm in _filter(vms, query)]
            )
        if kind in OBJECTS:
            return self._send(200, _filter(OBJECTS[kind], query))
        return self._send(404, {"error_type": "NOT_FOUND"})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def _filter(items: list[dict], query: dict[str, list[str]]) -> list[dict]:
    if "names" in query:
        items = [i for i in items if i.get("name") in query["names"]]
    if "types" in query:
        items = [i for i in items if i.get("type") in query["types"]]
    return items


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--bind", default="0.0.0.0")  # noqa: S104 (reached by the stack's containers)
    ap.add_argument("--port", type=int, default=8989)
    args = ap.parse_args()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(args.cert, args.key)
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"vcenter-sim on https://{args.bind}:{args.port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
