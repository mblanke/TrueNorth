"""S7 interruption exercise: range operations against a real broker and a real worker.

Runs the API (in this process, with its lifespan and redispatch loop), a real Celery
worker (a separate process, mock provisioner), a throwaway Redis container and a
scratch PostgreSQL database, then interrupts them on purpose:

1. journey: provision -> ready, stop -> stopped, start -> ready, stop, destroy ->
   destroyed (reservations released)
2. broker downtime: Redis stopped; a provision is still accepted (202), shows
   ``broker_unavailable``; Redis back; the API's own loop sends it; the range reaches ready
3. worker killed mid-provision: the task never reports; the operation shows
   ``no_outcome``; an operator abandons it; a new provision succeeds
4. partial provisioning: the provisioner fails every attempt; retries keep the range
   in ``provisioning``; the last attempt marks it failed with the provisioner's message;
   with a healthy provisioner a new provision succeeds

Every step is recorded (operations, states, timings; never credentials) to a JSON log,
and a summary table is printed. Nothing touches the dev stack's Redis, queues or
database: the broker is a separate container on its own port and the database is a
scratch one, dropped at the end.

Usage (needs Docker, the dev-stack Postgres and its superuser URL)::

    TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://USER:PASS@127.0.0.1:5433/postgres \\
        .venv/bin/python scripts/exercise_range_interruptions.py --out build/s7-exercise.json
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "control-plane" / "api"
WORKER = ROOT / "control-plane" / "worker"
REDIS_PORT = 6390
REDIS_NAME = "tn-s7-exercise-redis"
LOG: list[dict] = []


def note(step: str, **facts) -> None:
    entry = {"t": datetime.now(UTC).isoformat(timespec="seconds"), "step": step, **facts}
    LOG.append(entry)
    print(json.dumps(entry), flush=True)


def redis(up: bool) -> None:
    subprocess.run(["docker", "rm", "-f", REDIS_NAME], capture_output=True)
    if up:
        subprocess.run(["docker", "run", "-d", "--rm", "--name", REDIS_NAME, "-p", f"127.0.0.1:{REDIS_PORT}:6379",
                        "redis:7-alpine"], check=True, capture_output=True)
        for _ in range(50):
            done = subprocess.run(["docker", "exec", REDIS_NAME, "redis-cli", "ping"], capture_output=True, text=True)
            if done.stdout.strip() == "PONG":
                return
            time.sleep(0.2)
        raise RuntimeError("redis did not start")


def start_worker(env: dict, **extra) -> subprocess.Popen:
    wenv = {**env, **{k: str(v) for k, v in extra.items()}}
    return subprocess.Popen(
        [sys.executable, "-m", "celery", "-A", "worker.celery_app", "worker", "--loglevel=WARNING",
         "--concurrency=1", "--pool=solo", "-n", f"s7-{uuid.uuid4().hex[:6]}@%h"],
        cwd=WORKER, env=wenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def stop_worker(proc: subprocess.Popen, sig=signal.SIGTERM) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):  # macOS: EPERM when only a zombie is left
        proc.wait()
        return
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "s7-exercise.json")
    args = ap.parse_args()
    admin_url = os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        print("set TEST_POSTGRES_ADMIN_URL", file=sys.stderr)
        return 2

    import sqlalchemy as sa

    dbname = f"tn_s7_{uuid.uuid4().hex[:10]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{dbname}"'))
    db_url = sa.engine.make_url(admin_url).set(database=dbname).render_as_string(hide_password=False)
    env = {
        "PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"),
        "DATABASE_URL": db_url, "REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
        "PYTHONPATH": os.pathsep.join([str(WORKER), str(ROOT / "scenario-engine"), str(API)]),
        "PROVISIONER_BACKEND": "mock", "MOCK_PROVISION_DELAY": "1", "MOCK_FAILURE_RATE": "0",
        "AUTH_DISABLED": "true",
    }
    worker = None
    try:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API, check=True, capture_output=True,
                       env={**env, "PYTHONPATH": str(API)})
        os.environ.update({
            "DATABASE_URL": db_url, "REDIS_URL": env["REDIS_URL"], "AUTH_DISABLED": "true", "DB_AUTO_CREATE": "false",
            "SEED_DEV_DATA": "true", "RATE_LIMIT_ENABLED": "false", "RANGE_OP_REDISPATCH_SECONDS": "2",
            "RANGE_OP_STALE_AFTER_SECONDS": "15",
        })
        sys.path.insert(0, str(API))
        from app.main import app
        from fastapi.testclient import TestClient

        redis(True)
        worker = start_worker(env)
        with TestClient(app) as client:
            def wait(rid, states, timeout=90):
                end = time.monotonic() + timeout
                while time.monotonic() < end:
                    state = client.get(f"/ranges/{rid}").json()["state"]
                    if state in states:
                        return state
                    time.sleep(0.5)
                raise AssertionError(f"range {rid} never reached {states}")

            def latest_op(rid):
                return client.get(f"/ranges/{rid}/operations").json()[0]

            tmpl = client.post("/templates", json={"name": "S7", "version": "1.0", "is_public": True,
                                                   "yaml": "nodes:\n  - id: ws01\n    os: ubuntu\n"}).json()

            def new_range(name):
                return client.post("/ranges", json={"name": name, "template_id": tmpl["id"]}).json()["id"]

            # 1. Journey
            rid = new_range("journey")
            t0 = time.monotonic()
            r = client.post(f"/ranges/{rid}/provision")
            note("journey.provision.accepted", status=r.status_code, op=latest_op(rid)["status"])
            note("journey.provision.done", state=wait(rid, {"ready", "failed"}), secs=round(time.monotonic() - t0, 1),
                 op=latest_op(rid)["status"])
            # Power: stop and start reach the worker and come back (S0's /stop defect)
            for action, doing, done in (("stop", "stopping", "stopped"), ("start", "starting", "ready"),
                                        ("stop", "stopping", "stopped")):
                r = client.post(f"/ranges/{rid}/{action}")
                note(f"journey.{action}.accepted", status=r.status_code, state=r.json()["state"])
                assert r.json()["state"] == doing
                note(f"journey.{action}.done", state=wait(rid, {done, "failed"}), op=latest_op(rid)["status"])
            r = client.post(f"/ranges/{rid}/destroy")  # from stopped
            note("journey.destroy.accepted", status=r.status_code)
            note("journey.destroy.done", state=wait(rid, {"destroyed", "failed"}), op=latest_op(rid)["status"])

            # 2. Broker downtime
            rid = new_range("broker-down")
            redis(False)
            r = client.post(f"/ranges/{rid}/provision")
            op = latest_op(rid)
            note("broker.down.accepted", status=r.status_code, op=op["status"], error=(op["error"] or {}).get("code"))
            time.sleep(4)
            note("broker.down.still_pending", op=latest_op(rid)["status"], state=client.get(f"/ranges/{rid}").json()["state"])
            stop_worker(worker)
            redis(True)
            worker = start_worker(env)
            t0 = time.monotonic()
            note("broker.back.done", state=wait(rid, {"ready", "failed"}), op=latest_op(rid)["status"],
                 attempts=latest_op(rid)["dispatch_attempts"], secs_after_broker_back=round(time.monotonic() - t0, 1))

            # 3. Worker killed mid-provision
            stop_worker(worker)
            worker = start_worker(env, MOCK_PROVISION_DELAY=20)
            rid = new_range("worker-killed")
            client.post(f"/ranges/{rid}/provision")
            time.sleep(4)
            stop_worker(worker, signal.SIGKILL)
            note("worker.killed", state=client.get(f"/ranges/{rid}").json()["state"], op=latest_op(rid)["status"])
            worker = start_worker(env)
            time.sleep(17)  # past RANGE_OP_STALE_AFTER_SECONDS
            op = latest_op(rid)
            note("worker.killed.no_outcome", op=op["status"], error=(op["error"] or {}).get("code"),
                 conflicting_destroy=client.post(f"/ranges/{rid}/destroy").status_code)
            gone = client.post(f"/ranges/{rid}/operations/{op['id']}/abandon").json()
            note("worker.killed.abandoned", op=gone["status"], state=client.get(f"/ranges/{rid}").json()["state"])
            client.post(f"/ranges/{rid}/provision")
            note("worker.killed.reprovisioned", state=wait(rid, {"ready", "failed"}), op=latest_op(rid)["status"],
                 generations=[o["generation"] for o in client.get(f"/ranges/{rid}/operations").json()])

            # 4. Partial provisioning: every attempt fails, then a healthy retry
            stop_worker(worker)
            worker = start_worker(env, MOCK_FAILURE_RATE=1, MOCK_PROVISION_DELAY=0.2)
            rid = new_range("partial")
            client.post(f"/ranges/{rid}/provision")
            time.sleep(2)
            note("partial.retrying", state=client.get(f"/ranges/{rid}").json()["state"])
            state = wait(rid, {"failed", "ready"}, timeout=180)
            op = latest_op(rid)
            note("partial.last_attempt", state=state, op=op["status"], error=(op["error"] or {}).get("code"),
                 message=((op["error"] or {}).get("message") or "")[:80])
            stop_worker(worker)
            worker = start_worker(env)
            client.post(f"/ranges/{rid}/provision")
            note("partial.healthy_retry", state=wait(rid, {"ready", "failed"}), op=latest_op(rid)["status"])
    finally:  # each cleanup on its own, so one failure cannot leave the others undone
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(LOG, indent=2))
        for cleanup in (lambda: worker and stop_worker(worker, signal.SIGKILL), lambda: redis(False)):
            try:
                cleanup()
            except Exception as exc:  # noqa: BLE001
                print(f"cleanup: {exc!r}", file=sys.stderr)
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
    return 0


if __name__ == "__main__":
    sys.exit(main())
