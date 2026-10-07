"""Background-noise deployment: install the agent on a range's agent nodes.

The API decides what to deploy (``POST /noise/ranges/{id}/deploy``) and hands this task
an inventory: management addresses and one fresh token per node. This task only runs
Ansible over the management network. It touches no database, so the noise section
stays out of the worker's raw-SQL surface.

Tokens arrive as task arguments and leave only as 0600 files in a private temp dir that
is removed afterwards. Nothing returned from here contains them.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .celery_app import app

logger = logging.getLogger("worker.noise")

ANSIBLE_DIR = Path(os.getenv("ANSIBLE_DIR", "/opt/truenorth/infra/ansible"))
NOISE_AGENT_SRC = os.getenv("NOISE_AGENT_SRC", "/opt/truenorth/noise-agent/tn_noise_agent")
PLAYBOOK = "playbooks/deploy-noise-agents.yml"
TIMEOUT = int(os.getenv("NOISE_DEPLOY_TIMEOUT", "1800"))


def _mock_mode() -> bool:
    return os.getenv("NOISE_DEPLOY_MODE", os.getenv("PROVISIONER_BACKEND", "mock")) == "mock"


def write_inventory(workdir: Path, inventory: dict) -> Path:
    """INI inventory by management address; each host's token in its own 0600 file."""
    hv = workdir / "host_vars"
    hv.mkdir(mode=0o700)
    lines = ["[noise_agents]"]
    for a in inventory["agents"]:
        lines.append(f"{a['node']} ansible_host={a['mgmt_ip']}")
        path = hv / f"{a['node']}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"noise_agent_token": a["token"]}, f)
    inv = workdir / "inventory.ini"
    inv.write_text("\n".join(lines) + "\n")
    return inv


def playbook_vars(inventory: dict) -> dict:
    return {
        "noise_controller_url": inventory["controller_url"],
        "noise_mgmt_cidr": inventory["mgmt_cidr"],
        "noise_domain": inventory.get("domain", "corp.local"),
        "noise_agent_src": NOISE_AGENT_SRC,
        "range_id": inventory["range_id"],
    }


@app.task(bind=True, name="worker.tasks.deploy_noise_agents", ignore_result=False)
def deploy_noise_agents(self, inventory: dict) -> dict:
    nodes = [a["node"] for a in inventory.get("agents", [])]
    rid = inventory.get("range_id", "?")
    if _mock_mode():
        logger.info("[noise] mock deploy for range %s: %s", rid, nodes)
        return {"status": "mock", "range_id": rid, "nodes": nodes}
    if shutil.which("ansible-playbook") is None:
        return {"status": "failed", "range_id": rid, "error": "ansible-playbook is not installed on this worker"}

    workdir = Path(tempfile.mkdtemp(prefix="tn-noise-"))
    try:
        inv = write_inventory(workdir, inventory)
        extra = workdir / "vars.json"
        extra.write_text(json.dumps(playbook_vars(inventory)))
        proc = subprocess.run(
            ["ansible-playbook", "-i", str(inv), PLAYBOOK, "-e", f"@{extra}"],
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env={**os.environ, "ANSIBLE_STDOUT_CALLBACK": "default", "ANSIBLE_NOCOLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        return {"status": "failed", "range_id": rid, "error": f"ansible timed out after {TIMEOUT}s"}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    tail = proc.stdout[-4000:]
    if proc.returncode != 0:
        logger.error(
            "[noise] deploy failed for range %s (rc=%s)\n%s\n%s", rid, proc.returncode, tail, proc.stderr[-2000:]
        )
        return {"status": "failed", "range_id": rid, "rc": proc.returncode, "log": tail}
    logger.info("[noise] deployed agents on %d node(s) in range %s", len(nodes), rid)
    return {"status": "ok", "range_id": rid, "nodes": nodes}
