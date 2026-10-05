"""Rehearse a code rollback on an upgraded, populated database (PostgreSQL).

1. migrate a scratch database to NEW's head and populate it through NEW's API code;
2. run OLD's API code against that same database (the code rollback; schema kept);
3. run NEW's code again (the roll forward) and check that nothing was lost.

Each step runs that ref's own control-plane/api, taken with `git archive`, in a separate
process. The broker is deliberately unreachable. The scratch database is dropped at the
end. Prints one JSON object per step; exits 1 if a check fails.

Usage::

    TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://USER:PASS@127.0.0.1:5433/postgres \\
        .venv/bin/python scripts/rehearse_rollback.py --old main --new hardening/integration-candidate
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STEP = r"""
import json, sys
from fastapi.testclient import TestClient
from app.main import app
phase, ids = sys.argv[1], json.loads(sys.argv[2])
out = {}
with TestClient(app) as c:
    if phase == "populate":
        t = c.post("/templates", json={"name": "RB", "version": "1.0", "is_public": True, "yaml": "nodes: []\n"}).json()
        a = c.post("/ranges", json={"name": "rb-a", "template_id": t["id"]}).json()
        b = c.post("/ranges", json={"name": "rb-b", "template_id": t["id"]}).json()
        out.update(template_id=t["id"], a=a["id"], b=b["id"],
                   provision_a=c.post(f"/ranges/{a['id']}/provision").status_code,
                   op_a=c.get(f"/ranges/{a['id']}/operations").json()[0]["status"],
                   wiki=c.post("/wiki/spaces", json={"name": "Runbooks", "slug": "runbooks", "visibility": "all"}).status_code)
    elif phase == "old":
        out.update(health=c.get("/health").status_code,
                   ranges=sorted(r["name"] for r in c.get("/ranges").json()),
                   a_state=c.get(f"/ranges/{ids['a']}").json()["state"],
                   create=c.post("/ranges", json={"name": "rb-c", "template_id": ids["template_id"]}).status_code,
                   provision_b=c.post(f"/ranges/{ids['b']}/provision").status_code)
    else:
        out.update(ops_a=[o["status"] for o in c.get(f"/ranges/{ids['a']}/operations").json()],
                   b_state=c.get(f"/ranges/{ids['b']}").json()["state"],
                   wiki=[s["slug"] for s in c.get("/wiki/spaces").json()])
print(json.dumps(out))
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="main")
    ap.add_argument("--new", default="HEAD")
    args = ap.parse_args()
    admin_url = os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        print("set TEST_POSTGRES_ADMIN_URL", file=sys.stderr)
        return 2
    import sqlalchemy as sa

    name = f"tn_rollback_{uuid.uuid4().hex[:10]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = sa.engine.make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "DATABASE_URL": url,
        "AUTH_DISABLED": "true",
        "DB_AUTO_CREATE": "false",
        "SEED_DEV_DATA": "true",
        "RATE_LIMIT_ENABLED": "false",
        "RANGE_OP_REDISPATCH_SECONDS": "0",
        "REDIS_URL": "redis://127.0.0.1:1/0",
    }
    failures = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            trees = {}
            for label, ref in (("old", args.old), ("new", args.new)):
                dest = Path(tmp) / label
                dest.mkdir()
                archive = subprocess.run(
                    ["git", "archive", ref, "control-plane/api"], cwd=ROOT, capture_output=True, check=True
                ).stdout
                subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
                trees[label] = dest / "control-plane" / "api"
            (Path(tmp) / "step.py").write_text(STEP)

            def run(tree, *argv):
                done = subprocess.run(
                    [sys.executable, str(Path(tmp) / "step.py"), *argv],
                    cwd=tree,
                    env={**env, "PYTHONPATH": str(tree)},
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                lines = [ln for ln in done.stdout.splitlines() if ln.startswith("{")]
                if done.returncode or not lines:
                    raise RuntimeError(f"{argv[0]} failed:\n{done.stderr[-2000:]}")
                return json.loads(lines[-1])

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=trees["new"],
                check=True,
                env={**env, "PYTHONPATH": str(trees["new"])},
                capture_output=True,
            )
            ids = run(trees["new"], "populate", "{}")
            print(json.dumps({"step": "new populates", **ids}))
            old = run(trees["old"], "old", json.dumps(ids))
            print(json.dumps({"step": "old code on upgraded db", **old}))
            back = run(trees["new"], "forward", json.dumps(ids))
            print(json.dumps({"step": "new again", **back}))
            checks = {
                "old: health 200": old["health"] == 200,
                "old: existing ranges readable": {"rb-a", "rb-b"} <= set(old["ranges"]),
                "old: create works": old["create"] in (200, 201),
                "new again: accepted operation kept": back["ops_a"] == [ids["op_a"]],
                "new again: wiki data kept": back["wiki"] == ["runbooks"],
            }
            failures = [k for k, ok in checks.items() if not ok]
            print(json.dumps({"step": "checks", "failed": failures}))
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
