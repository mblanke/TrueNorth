"""Register the installer's vCenter as a hypervisor connection, from inside the api image.

The installer renders the worker's vCenter (VSPHERE_URL/USERNAME/PASSWORD/DATACENTER/
VERIFY_SSL) but, until 2026-10-15, never registered it through the API, so
``/infrastructure`` was empty after an install and the acceptance suite's provisioners
check found "no vSphere connection registered" (TN-MGMT01). This registers it as the
bootstrap administrator, in-process (scripts/_inprocess_api.py): the API's routes,
permissions, CSRF and validation all apply, and no token is used.

Run by the installer (roles/tn_seed, tn_register_vcenter_connection) as a one-off api
container:

    docker compose run --rm --no-deps -T -w /app -e PYTHONPATH=/app -e TN_VCENTER_PASSWORD \\
      -v <app checkout>:/srcapp:ro --entrypoint python api \\
      /srcapp/scripts/register_vcenter_inprocess.py --admin <upn> --name "Range vCenter" \\
      --host <h> --port <p> --username <u> --datacenter <dc> [--insecure] [--update-password]

The vCenter password comes ONLY from the environment variable ``TN_VCENTER_PASSWORD``
(the installer passes it through ``docker compose -e NAME`` from a ``no_log`` task), never
from the command line. It is never printed.

Idempotent. A vsphere connection of the administrator's tenant with the same host is
reused, never duplicated; its ``username``, ``datacenter`` and ``verify_ssl`` are patched
when they differ from the installer's. Its password is left alone (an operator may have
rotated it on the Infrastructure page) unless ``--update-password``. When the tenant has no
primary vsphere connection this one becomes primary. Then the connection is tested and
discovered; a failed test or discovery is a WARNING, not a failure: the connection is
registered either way.

Exit: 0 registered (test/discover may have warned); 1 could not create or update it,
or no password to create it with; 2 the administrator does not exist.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

SRC = Path("/srcapp")
PASSWORD_ENV = "TN_VCENTER_PASSWORD"
HYPERVISOR_TYPE = "vsphere"
DEFAULT_NAME = "Range vCenter"


def _ok(status: Any) -> bool:
    return isinstance(status, int) and 200 <= status < 300


def _brief(body: Any, limit: int = 200) -> str:
    if isinstance(body, dict):
        body = body.get("message") or body.get("detail") or body
    return " ".join(str(body).split())[:limit]


def _vsphere_connections(api: Any) -> list[dict] | None:
    status, rows = api.get("/hypervisors/connections")
    if not _ok(status) or not isinstance(rows, list):
        print(f"vcenter connection: FAIL listing connections: {status} {_brief(rows)}")
        return None
    return [r for r in rows if isinstance(r, dict) and r.get("hypervisor_type") == HYPERVISOR_TYPE]


def register(
    api: Any,
    *,
    name: str,
    host: str,
    port: int,
    username: str,
    datacenter: str | None,
    verify_ssl: bool,
    password: str,
    update_password: bool = False,
) -> int:
    """Create or reconcile the connection, make it primary when none is, test and discover.
    ``api`` has ``get``/``post``/``patch`` returning ``(status, body)``. Returns the exit code."""
    conns = _vsphere_connections(api)
    if conns is None:
        return 1
    has_primary = any(c.get("is_primary") for c in conns)
    want_host = host.strip().lower()
    existing = next((c for c in conns if str(c.get("host") or "").strip().lower() == want_host), None)
    datacenter = datacenter or None

    if existing is None:
        if not password:
            print(f"vcenter connection: FAIL not registered: {PASSWORD_ENV} is empty (vault_vsphere_password)")
            return 1
        payload = {
            "name": name,
            "hypervisor_type": HYPERVISOR_TYPE,
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "verify_ssl": verify_ssl,
            "is_primary": not has_primary,
            "datacenter": datacenter,
            "notes": "Registered by the installer (80-seed).",
        }
        status, conn = api.post("/hypervisors/connections", json=payload)
        if not _ok(status) or not isinstance(conn, dict):
            print(f"vcenter connection: FAIL create {host}:{port}: {status} {_brief(conn)}")
            return 1
        role = "primary" if conn.get("is_primary") else "not primary"
        print(f"vcenter connection: created '{conn.get('name')}' ({host}:{port}, {role})")
    else:
        conn = existing
        drift = {
            field: want
            for field, want in (("username", username), ("datacenter", datacenter), ("verify_ssl", verify_ssl))
            if conn.get(field) != want
        }
        if update_password and password:
            drift["password"] = password
        if drift:
            status, out = api.patch(f"/hypervisors/connections/{conn['id']}", json=drift)
            if not _ok(status) or not isinstance(out, dict):
                print(f"vcenter connection: FAIL update '{conn.get('name')}': {status} {_brief(out)}")
                return 1
            conn = out
            print(f"vcenter connection: updated '{conn.get('name')}' ({', '.join(sorted(drift))})")
        else:
            print(f"vcenter connection: unchanged '{conn.get('name')}' ({conn.get('host')}:{conn.get('port')})")
        if int(conn.get("port") or 0) != int(port):
            print(f"vcenter connection: WARNING registered port {conn.get('port')} differs from {port}; not changed")
        if not has_primary:
            status, out = api.post(f"/hypervisors/connections/{conn['id']}/set-primary")
            if _ok(status):
                print(f"vcenter connection: set primary '{conn.get('name')}'")
            else:
                print(f"vcenter connection: WARNING could not set primary: {status} {_brief(out)}")

    conn_id = conn["id"]
    status, result = api.post(f"/hypervisors/connections/{conn_id}/test")
    if _ok(status) and isinstance(result, dict) and result.get("success"):
        print(f"vcenter test: ok ({result.get('nodes_found', 0)} hosts, version {result.get('version') or 'n/a'})")
    else:
        print(f"vcenter test: WARNING failed: {status} {_brief(result)}")
    status, result = api.post(f"/hypervisors/connections/{conn_id}/discover")
    # The vSphere backend answers a failed discovery with 200 and "... discovery failed: ...".
    if _ok(status) and "failed" not in _brief(result).lower():
        print(f"vcenter discover: ok ({_brief(result, 120)})")
    else:
        print(f"vcenter discover: WARNING failed: {status} {_brief(result)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--admin", required=True, help="email/UPN of an existing administrator")
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--username", required=True)
    ap.add_argument("--datacenter", default="")
    ap.add_argument("--insecure", action="store_true", help="verify_ssl=false (a vCenter on the VMCA's chain)")
    ap.add_argument(
        "--update-password",
        action="store_true",
        help=f"also set an existing connection's password from {PASSWORD_ENV} (default: leave it)",
    )
    args = ap.parse_args(argv)
    password = os.environ.get(PASSWORD_ENV, "")  # the only variable read; never argv, never printed

    sys.path.insert(0, str(SRC / "scripts"))
    from _inprocess_api import connect  # the shared harness: identity override, fresh CSRF, 429 backoff

    connected = connect(args.admin)
    if connected is None:
        return 2
    _client, api = connected
    return register(
        api,
        name=args.name,
        host=args.host,
        port=args.port,
        username=args.username,
        datacenter=args.datacenter,
        verify_ssl=not args.insecure,
        password=password,
        update_password=args.update_password,
    )


if __name__ == "__main__":
    sys.exit(main())
