"""Background-noise deployment: install the agent on a range's agent nodes.

The API decides what to deploy (``POST /noise/ranges/{id}/deploy``) and hands this task
an inventory: management addresses and one fresh token per node. This task only runs
Ansible over the management network. It touches no database, so the noise section
stays out of the worker's raw-SQL surface.

Tokens arrive as task arguments and leave only as 0600 files in a private temp dir that
is removed afterwards. Nothing returned from here contains them.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .celery_app import app

logger = logging.getLogger("worker.noise")

ANSIBLE_DIR = Path(os.getenv("ANSIBLE_DIR", "/opt/truenorth/infra/ansible"))
NOISE_AGENT_SRC = os.getenv("NOISE_AGENT_SRC", "/opt/truenorth/noise-agent/tn_noise_agent")
PLAYBOOK = "playbooks/deploy-noise-agents.yml"
TIMEOUT = int(os.getenv("NOISE_DEPLOY_TIMEOUT", "1800"))


def _mock_mode() -> bool:
    return os.getenv("NOISE_DEPLOY_MODE", os.getenv("PROVISIONER_BACKEND", "mock")) == "mock"


# Everything below reaches Ansible: node ids become INI inventory lines and host_vars file
# names, and every string is a Jinja template to Ansible unless marked !unsafe. Security
# sweep M5 (2026-10-08): a node id like "x ansible_connection=local" or "../../etc" and a
# controller_url holding "{{ lookup('pipe', ...) }}" went straight in.
NODE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
RANGE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
DOMAIN = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_URL_PATH = re.compile(r"^[A-Za-z0-9/._~-]*$")


class InventoryError(ValueError):
    """The inventory is not safe to hand to Ansible. The message never echoes values."""


def _controller_url(url: object) -> str:
    if not isinstance(url, str) or len(url) > 512:
        raise InventoryError("controller_url is not a URL")
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port_ok = parts.port is None or 0 < parts.port < 65536
    except ValueError:
        port_ok = False
    valid_host = bool(DOMAIN.match(host))
    if not valid_host:
        try:
            ipaddress.ip_address(host)
            valid_host = True
        except ValueError:
            pass
    if (
        parts.scheme != "https" or not valid_host or not port_ok or parts.username or parts.password
        or parts.query or parts.fragment or not _URL_PATH.match(parts.path)
    ):
        raise InventoryError("controller_url must be a plain https://host[:port]/path URL")
    return url


def validate_inventory(inventory: dict) -> dict:
    """``inventory`` if every value is what it claims to be; ``InventoryError`` otherwise."""
    if not RANGE_ID.match(str(inventory.get("range_id", ""))):
        raise InventoryError("range_id is not an id")
    _controller_url(inventory.get("controller_url"))
    try:
        ipaddress.ip_network(str(inventory.get("mgmt_cidr", "")), strict=False)
    except ValueError as exc:
        raise InventoryError("mgmt_cidr is not a network") from exc
    if not DOMAIN.match(str(inventory.get("domain", "corp.local"))):
        raise InventoryError("domain is not a DNS name")
    agents = inventory.get("agents")
    if not isinstance(agents, list):
        raise InventoryError("agents is not a list")
    seen: set[str] = set()
    for a in agents:
        node = a.get("node") if isinstance(a, dict) else None
        if not isinstance(node, str) or not NODE_ID.match(node) or node in seen:
            raise InventoryError("a node id is not [a-z0-9][a-z0-9-]{0,62} (or repeats)")
        seen.add(node)
        try:
            ipaddress.ip_address(str(a.get("mgmt_ip", "")))
        except ValueError as exc:
            raise InventoryError(f"node {node}: mgmt_ip is not an address") from exc
        if not isinstance(a.get("token"), str) or not TOKEN.match(a["token"]):
            raise InventoryError(f"node {node}: the token is malformed")
    return inventory


def write_inventory(workdir: Path, inventory: dict) -> Path:
    """INI inventory by management address; each host's token in its own 0600 file."""
    validate_inventory(inventory)
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


def extra_vars_yaml(variables: dict) -> str:
    """The extra-vars file: every value a ``!unsafe`` string, so Ansible never renders it as
    a Jinja template (a second line of defence behind ``validate_inventory``). Each value is
    a JSON string, which is also a valid YAML double-quoted scalar."""
    return "".join(f"{key}: !unsafe {json.dumps(str(value))}\n" for key, value in variables.items())


@app.task(bind=True, name="worker.tasks.deploy_noise_agents", ignore_result=False)
def deploy_noise_agents(self, inventory: dict) -> dict:
    rid = inventory.get("range_id", "?")
    try:
        validate_inventory(inventory)
    except InventoryError as exc:
        logger.error("[noise] refused the inventory for range %s: %s", rid, exc)
        return {"status": "failed", "range_id": rid, "error": f"invalid inventory: {exc}"}
    nodes = [a["node"] for a in inventory.get("agents", [])]
    if _mock_mode():
        logger.info("[noise] mock deploy for range %s: %s", rid, nodes)
        return {"status": "mock", "range_id": rid, "nodes": nodes}
    if shutil.which("ansible-playbook") is None:
        return {"status": "failed", "range_id": rid, "error": "ansible-playbook is not installed on this worker"}

    workdir = Path(tempfile.mkdtemp(prefix="tn-noise-"))
    try:
        inv = write_inventory(workdir, inventory)
        extra = workdir / "vars.yml"
        extra.write_text(extra_vars_yaml(playbook_vars(inventory)))
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
