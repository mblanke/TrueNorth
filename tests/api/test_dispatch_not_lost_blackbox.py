"""A provision or destroy accepted while the broker is down still reaches the worker.

S0 defect "dispatch ignored": the range routes ignored ``dispatch`` returning None (broker
down). The API answered success, moved the range to ``provisioning``/``destroying``, and
the task was never sent: the range sat there for good. #17 (S3a) made the operation row
an outbox that the API re-sends when the broker is back.

Black box, so it runs unchanged against the code before #17: the API runs in a child
process with its real lifespan, on a file database, and is driven only over HTTP. The
broker is faked at ``app.celery_client.dispatch``, which both versions call. Nothing here
imports code that #17 added.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"

DRIVER = textwrap.dedent(
    """
    import json, sys, time
    import app.celery_client as cc

    broker = {"up": False}
    sent = []

    def dispatch(task, *args):
        if not broker["up"]:
            return None
        sent.append([task, *[str(a) for a in args]])
        return f"task-{len(sent)}"

    cc.dispatch = dispatch
    from fastapi.testclient import TestClient
    from app.main import app

    action = sys.argv[1]
    out = {}
    with TestClient(app) as c:
        t = c.post("/templates", json={"name": "BB", "version": "1.0", "is_public": True, "yaml": "id: bb\\n"}).json()
        rid = c.post("/ranges", json={"name": "bb", "template_id": t["id"]}).json()["id"]
        if action == "destroy":
            broker["up"] = True
            c.post(f"/ranges/{rid}/provision")
            # the worker's report, by the only route both versions share: the database
            from app.db import SessionLocal
            from app.models import Range, RangeState
            import uuid
            with SessionLocal() as s:
                s.get(Range, uuid.UUID(rid)).state = RangeState.ready
                s.commit()
            sent.clear()
            broker["up"] = False
        r = c.post(f"/ranges/{rid}/{action}")
        out["status"] = r.status_code
        out["state_while_down"] = c.get(f"/ranges/{rid}").json()["state"]
        broker["up"] = True
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not sent:
            time.sleep(0.2)
        out["sent"] = sent
        out["range_id"] = rid
    print("RESULT " + json.dumps(out))
    """
)


def _run(tmp_path: Path, action: str) -> dict:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONPATH": str(API),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'api.db'}",
        "DB_AUTO_CREATE": "true",
        "SEED_DEV_DATA": "true",
        "AUTH_DISABLED": "true",
        "RATE_LIMIT_ENABLED": "false",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "RANGE_OP_REDISPATCH_SECONDS": "0.5",
        "RANGE_OP_REDISPATCH_MIN_AGE_SECONDS": "1",
        "OTEL_ENABLED": "false",
    }
    done = subprocess.run(
        [sys.executable, "-c", DRIVER, action], cwd=API, env=env, capture_output=True, text=True, timeout=120
    )
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT ")]
    assert done.returncode == 0 and lines, f"driver failed:\n{done.stdout[-2000:]}\n{done.stderr[-3000:]}"
    return json.loads(lines[-1][len("RESULT ") :])


@pytest.mark.parametrize(
    ("action", "task", "in_progress"),
    [
        ("provision", "provision_range", "provisioning"),
        ("destroy", "destroy_range", "destroying"),
    ],
)
def test_accepted_while_the_broker_is_down_and_sent_when_it_is_back(tmp_path, action, task, in_progress):
    got = _run(tmp_path, action)
    assert got["status"] in (200, 202), got
    assert got["state_while_down"] == in_progress, got
    assert [task, got["range_id"]] in got["sent"], f"the {task} task never reached the broker: {got}"
