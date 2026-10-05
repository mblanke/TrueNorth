"""A range state change the worker reports reaches a browser's WebSocket.

S0 defect "notifications unheard": the worker publishes range states on Redis channel
``truenorth:range``; the API's WebSocket bridge listened only on ``truenorth:ws:*`` (and
was never started), so nothing the worker said reached a browser.

Black box, so it runs unchanged against the code before the fix: the API runs as a real
server process (uvicorn) on a file database, the test connects a real WebSocket client
to ``/ws/ranges`` and publishes with the worker's own ``_notify_api`` on a real Redis.
Runs when TEST_REDIS_URL names a Redis the test may use (never the dev stack's);
skipped otherwise.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "control-plane" / "api"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def api_server(tmp_path):
    redis_url = os.getenv("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("set TEST_REDIS_URL to run against a real Redis")
    port = _free_port()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONPATH": str(API),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'api.db'}",
        "DB_AUTO_CREATE": "true",
        "SEED_DEV_DATA": "true",
        "AUTH_DISABLED": "true",
        "RATE_LIMIT_ENABLED": "false",
        "REDIS_URL": redis_url,
        "RANGE_OP_REDISPATCH_SECONDS": "0",
        "WS_EVENTS_ENABLED": "true",
    }
    log = open(tmp_path / "api.log", "w")  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=API,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise AssertionError((tmp_path / "api.log").read_text()[-3000:])
        yield base, redis_url
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()


def test_a_range_state_the_worker_reports_reaches_the_browser(api_server):
    from websockets.sync.client import connect
    from worker import tasks

    base, redis_url = api_server
    t = httpx.post(f"{base}/templates", json={"name": "WS", "version": "1.0", "yaml": "id: ws\n", "is_public": True})
    rid = httpx.post(f"{base}/ranges", json={"name": "ws", "template_id": t.json()["id"]}).json()["id"]

    tasks.REDIS_URL = redis_url  # the worker's own publisher, on the test's Redis
    got = None
    with connect(base.replace("http", "ws") + "/ws/ranges", open_timeout=5) as ws:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and got is None:
            tasks._notify_api("range", {"id": rid, "state": "ready"})
            try:
                while True:
                    msg = json.loads(ws.recv(timeout=0.5))
                    if msg.get("type") == "range_state":
                        got = msg
                        break
            except TimeoutError:
                continue
    assert got is not None, "the worker's range state never reached the WebSocket"
    assert got["data"]["id"] == rid and got["data"]["state"] == "ready"
