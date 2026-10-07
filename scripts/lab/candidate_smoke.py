"""Runbook §5 smoke test against the real vCenter, on this checkout (the S8 lab evidence).

Carved from hardening/handoff (#40) onto main of 2026-10-07. **Needs a live lab** for its
real purpose (``--apply``): vCenter credentials, the ``TrueNorth-Provision`` role and an
Ubuntu template. ``--apply --mock`` rehearses the script itself with the mock provisioner
and no vCenter, so its wiring can be checked against the current API without a lab.

Plan-only by default: checks the settings and that vCenter is reachable, changes nothing.
With ``--apply`` it runs this checkout's API and a real Celery worker (vsphere_api
backend) on a scratch PostgreSQL database and a throwaway Redis, and drives one range
through the range operations:

1. golden images: import the catalogue, point ``ubuntu-lts`` at ``$LAB_TEMPLATE``
2. a one-VM Ubuntu range: provision -> ready, its VLAN/uplink reservations listed
3. stop -> stopping -> stopped; start -> starting -> running (range operations)
4. destroy -> destroyed; its reservations released
5. (``--concurrent``) two ranges provisioned at once get different VLANs

Whatever happens, a range it provisioned is destroyed at the end. Every step is written
to ``docs/hardening/evidence/lab-smoke-<UTC time>.json`` (no credentials: the password
is read from the environment, never printed or written). Every ``VSPHERE_*`` variable
and ``TN_DEPOT_URL`` are passed to the worker as they are.

Usage (your terminal, so the password never reaches a log)::

    export VSPHERE_URL=https://192.168.1.10 VSPHERE_USERNAME=svc-truenorth@vsphere.local
    export VSPHERE_DATACENTER=... VSPHERE_CLUSTER=...        # as in .env.production
    read -rs VSPHERE_PASSWORD && export VSPHERE_PASSWORD
    export TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://USER:PASS@127.0.0.1:5433/postgres
    .venv/bin/python scripts/lab/candidate_smoke.py              # plan
    .venv/bin/python scripts/lab/candidate_smoke.py --apply      # run it
    .venv/bin/python scripts/lab/candidate_smoke.py --apply --mock   # no lab: rehearse the script
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API, WORKER = ROOT / "control-plane" / "api", ROOT / "control-plane" / "worker"
PY = sys.executable
REQUIRED = ("VSPHERE_URL", "VSPHERE_USERNAME", "VSPHERE_PASSWORD", "VSPHERE_DATACENTER", "TEST_POSTGRES_ADMIN_URL")
API_PORT, REDIS_PORT, REDIS_NAME = 8098, 6394, "tn-lab-smoke-redis"
LOG: list[dict] = []


def note(step: str, **facts) -> None:
    entry = {"t": datetime.now(UTC).isoformat(timespec="seconds"), "step": step, **facts}
    LOG.append(entry)
    print(json.dumps(entry), flush=True)


def passed_through() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if (k.startswith("VSPHERE_") or k == "TN_DEPOT_URL") and v}


def plan(mock: bool) -> int:
    required = ("TEST_POSTGRES_ADMIN_URL",) if mock else REQUIRED
    missing = [k for k in required if not os.environ.get(k)]
    print("settings:", {k: ("set" if os.environ.get(k) else "MISSING") for k in required})
    if missing:
        print("missing:", ", ".join(missing), "(see the usage at the top of this file)")
        return 2
    if mock:
        print("mock provisioner: no vCenter is contacted")
        return 0
    import httpx

    r = httpx.get(os.environ["VSPHERE_URL"].rstrip("/") + "/api/vcenter/vm", verify=False, timeout=10)  # noqa: S501
    print(f"vCenter reachable: HTTP {r.status_code} without credentials (401 expected)")
    print("template:", os.environ.get("LAB_TEMPLATE", "tmpl-ubuntu-2404"), "| add --apply to run the smoke test")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="provision on vCenter (otherwise plan only)")
    ap.add_argument("--mock", action="store_true", help="use the mock provisioner (no vCenter): rehearse the script")
    ap.add_argument("--concurrent", action="store_true", help="also provision two ranges at once")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds to wait for each step")
    args = ap.parse_args()
    if not args.apply:
        return plan(args.mock)
    if plan(args.mock):
        return 2

    import httpx
    import sqlalchemy as sa

    admin_url = os.environ["TEST_POSTGRES_ADMIN_URL"]
    dbname = f"tn_lab_{uuid.uuid4().hex[:10]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{dbname}"'))
    db_url = sa.engine.make_url(admin_url).set(database=dbname).render_as_string(hide_password=False)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "DATABASE_URL": db_url,
        "REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
        "AUTH_DISABLED": "true",
        "PROVISIONER_BACKEND": "mock" if args.mock else "vsphere_api",
        **({"MOCK_PROVISION_DELAY": "0.5"} if args.mock else passed_through()),
    }
    procs: list[subprocess.Popen] = []
    base = f"http://127.0.0.1:{API_PORT}"
    built: list[str] = []
    kind = "mock-" if args.mock else ""
    out = ROOT / "docs" / "hardening" / "evidence" / f"lab-smoke-{kind}{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    try:
        subprocess.run(["docker", "rm", "-f", REDIS_NAME], capture_output=True)
        port = f"127.0.0.1:{REDIS_PORT}:6379"
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", REDIS_NAME, "-p", port, "redis:7-alpine"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [PY, "-m", "alembic", "upgrade", "head"],
            cwd=API,
            check=True,
            capture_output=True,
            env={**env, "PYTHONPATH": str(API)},
        )
        logs = out.with_suffix("")
        out.parent.mkdir(parents=True, exist_ok=True)
        api_log = open(f"{logs}-api.log", "w")  # noqa: SIM115 — lives as long as the process
        worker_log = open(f"{logs}-worker.log", "w")  # noqa: SIM115
        procs.append(
            subprocess.Popen(
                [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)],
                cwd=API,
                env={
                    **env,
                    "PYTHONPATH": str(API),
                    "DB_AUTO_CREATE": "false",
                    "SEED_DEV_DATA": "true",
                    "RATE_LIMIT_ENABLED": "false",
                    "RANGE_OP_REDISPATCH_SECONDS": "5",
                },
                stdout=api_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        )
        procs.append(
            subprocess.Popen(
                [
                    PY,
                    "-m",
                    "celery",
                    "-A",
                    "worker.celery_app",
                    "worker",
                    "--loglevel=INFO",
                    "--concurrency=2",
                    # macOS starts prefork children with spawn, where Celery's fast trace
                    # path fails every task ("not enough values to unpack"); use threads.
                    *(["--pool=threads"] if sys.platform == "darwin" else []),
                    "-n",
                    f"lab-{uuid.uuid4().hex[:6]}@%h",
                ],
                cwd=WORKER,
                env={**env, "PYTHONPATH": f"{WORKER}{os.pathsep}{ROOT / 'scenario-engine'}"},
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        )
        c = httpx.Client(base_url=base, timeout=60)
        for _ in range(100):
            try:
                if c.get("/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)

        def state(rid):
            return c.get(f"/ranges/{rid}").json()["state"]

        def wait(rid, targets):
            end = time.monotonic() + args.timeout
            while time.monotonic() < end:
                s = state(rid)
                if s in targets:
                    return s
                time.sleep(1 if args.mock else 5)
            raise TimeoutError(f"range {rid} never reached {targets} (last: {state(rid)})")

        def last_op(rid):
            ops = c.get(f"/ranges/{rid}/operations").json()
            return {k: ops[0].get(k) for k in ("action", "status", "error")} if ops else None

        def reservations(rid):
            return [(r["kind"], r["value"]) for r in c.get(f"/ranges/{rid}/network-reservations").json()]

        # 1. golden images (the endpoint takes the catalogue CSV as an upload)
        catalogue = ROOT / "content" / "catalogue" / "vm_iso_catalogue.csv"
        r = c.post(
            "/golden-images/import-catalogue",
            params={"hypervisor": "vsphere"},
            files={"file": (catalogue.name, catalogue.read_bytes(), "text/csv")},
        )
        note("golden_images.import", status=r.status_code)
        imgs = [
            i
            for i in c.get("/golden-images", params={"hypervisor": "vsphere"}).json()
            if i["catalogue_id"] == "ubuntu-lts"
        ]
        if imgs:
            p = c.patch(
                f"/golden-images/{imgs[0]['id']}",
                json={"template_name": os.environ.get("LAB_TEMPLATE", "tmpl-ubuntu-2404"), "build_status": "built"},
            )
            note("golden_images.ubuntu_lts", status=p.status_code)
        tmpl = c.post(
            "/templates",
            json={
                "name": "lab smoke",
                "version": "1.0",
                "is_public": True,
                "yaml": "nodes:\n  - id: smoke01\n    os: ubuntu-lts\n    vlan: lan\n"
                "network:\n  vlans:\n    - name: lan\n      cidr: 10.99.0.0/24\n",
            },
        ).json()

        def new_range(name):
            return c.post("/ranges", json={"name": name, "template_id": tmpl["id"]}).json()["id"]

        # 2. provision
        rid = new_range("lab-smoke")
        built.append(rid)
        t0 = time.monotonic()
        note("provision.accepted", status=c.post(f"/ranges/{rid}/provision").status_code)
        note(
            "provision.done",
            state=wait(rid, {"ready", "failed"}),
            secs=round(time.monotonic() - t0),
            op=last_op(rid),
            reservations=reservations(rid),
            vms=[
                {k: v for k, v in vm.items() if k in ("name", "vm_id", "host", "datastore", "ip", "status")}
                for vm in json.loads(c.get(f"/ranges/{rid}").json().get("provisioner_output") or "{}").get("vms", [])
            ],
        )
        # 3. power
        for action, doing, done in (("stop", "stopping", "stopped"), ("start", "starting", "running")):
            r = c.post(f"/ranges/{rid}/{action}")
            note(f"{action}.accepted", status=r.status_code, state=r.json().get("state"), expected=doing)
            note(f"{action}.done", state=wait(rid, {done, "failed"}), op=last_op(rid))
        # 5. concurrent VLANs
        if args.concurrent:
            pair = [new_range("lab-smoke-a"), new_range("lab-smoke-b")]
            built.extend(pair)
            for p_ in pair:
                c.post(f"/ranges/{p_}/provision")
            states = [wait(p_, {"ready", "failed"}) for p_ in pair]
            vlans = [{v for k, v in reservations(p_) if k == "vlan"} for p_ in pair]
            note("concurrent.done", states=states, vlans=[sorted(v) for v in vlans], disjoint=not (vlans[0] & vlans[1]))
        return 0
    except Exception as exc:
        note("error", detail=str(exc)[:500])
        return 1
    finally:
        # 4. destroy whatever was built, and check its reservations went with it
        try:
            c = httpx.Client(base_url=base, timeout=60)
            for rid in built:
                if c.get(f"/ranges/{rid}").json()["state"] in ("ready", "running", "stopped", "failed"):
                    c.post(f"/ranges/{rid}/destroy")
                    end = time.monotonic() + args.timeout
                    while time.monotonic() < end and c.get(f"/ranges/{rid}").json()["state"] not in (
                        "destroyed",
                        "failed",
                    ):
                        time.sleep(1 if args.mock else 5)
                note(
                    "destroy.done",
                    range=rid,
                    state=c.get(f"/ranges/{rid}").json()["state"],
                    op=c.get(f"/ranges/{rid}/operations").json()[0]["status"],
                    reservations_left=len(c.get(f"/ranges/{rid}/network-reservations").json()),
                )
        except Exception as exc:  # noqa: BLE001 — still record and clean up the rest
            note("destroy.error", detail=str(exc)[:500], ranges=built)
        for p in procs:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(p.pid, 15)
        subprocess.run(["docker", "rm", "-f", REDIS_NAME], capture_output=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "checkout": subprocess.run(
                        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
                    ).stdout.strip(),
                    "provisioner": env["PROVISIONER_BACKEND"],
                    "steps": LOG,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"evidence: {out.relative_to(ROOT)} (logs next to it, *.log, not committed)")
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    sys.exit(main())
