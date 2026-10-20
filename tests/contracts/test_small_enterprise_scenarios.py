"""Every scenario that runs on small-enterprise names hosts that template builds.

Scenarios find their hosts three ways: by tag (``host_tag``, ``src_tag``,
``target_group``), by hostname (``target`` and its spellings, and "host X" in a prompt a
Student reads) and by address (an IPv4 literal anywhere in a parameter, such as
insider-threat's ``http://10.0.2.10/...``). No code resolves these at run time yet (the
injectors are simulated and take them as strings), so this test is the contract: each
reference must resolve against the template as the worker renders it.

The template was converted from the 0.1 ``assets`` sketch, which built hosts ``jump``,
``dc``, ``user-0..2`` on no network; none of ``dc-01``, ``ws-01``, ``ws-02``, ``10.0.2.10``
resolved against it (test_the_old_assets_sketch_did_not_resolve). The standalone runner's
mock range (``runner/run.py``) uses the same names and addresses, and is checked too.
"""

from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))

from runner.run import build_range_context  # noqa: E402
from worker.render import render_topology  # noqa: E402

TEMPLATE = ROOT / "content/ranges/small-enterprise/template.yaml"
RANGE_ID = "abcdef12-3456-7890-abcd-ef1234567890"
SCENARIOS = {
    "ransomware-lite": ROOT / "content/scenarios/ransomware-lite/scenario.yaml",
    "apt-breach": ROOT / "scenario-engine/examples/apt-breach.yaml",
    "insider-threat": ROOT / "scenario-engine/examples/insider-threat.yaml",
    "supply-chain": ROOT / "scenario-engine/examples/supply-chain.yaml",
}
# What each scenario references, so a change to the extraction cannot pass by finding nothing.
EXPECTED = {
    "ransomware-lite": {("tag", "users"), ("tag", "user"), ("host", "ws-02")},
    "apt-breach": {("host", "ws-01"), ("host", "dc-01")},
    "insider-threat": {("host", "dc-01"), ("address", "10.0.2.10")},
    "supply-chain": {("host", "ws-01"), ("host", "ws-02")},
}

TAG_KEYS = ("host_tag", "src_tag", "dst_tag", "target_group")
HOST_KEYS = ("target", "target_host", "target_dc", "host", "hostname")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
PROMPT_HOST = re.compile(r"\bhost\s+([A-Za-z0-9][A-Za-z0-9-]*)", re.I)


def references(scenario: dict) -> set[tuple[str, str]]:
    """(kind, value) for every host reference in the timeline and objectives."""
    refs: set[tuple[str, str]] = set()

    def walk(key: str, value) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(str(k), v)
        elif isinstance(value, list):
            for v in value:
                walk(key, v)
        elif isinstance(value, str):
            if key in TAG_KEYS:
                refs.add(("tag", value.lower()))
            elif key in HOST_KEYS:
                refs.add(("host", value.lower()))
            elif key == "prompt":
                refs.update(("host", m.lower()) for m in PROMPT_HOST.findall(value))
            refs.update(("address", str(ipaddress.ip_address(m))) for m in IPV4.findall(value))

    for item in (scenario.get("timeline") or []) + (scenario.get("objectives") or []):
        walk("", (item or {}).get("params") or {})
    return refs


def inventory(template: dict) -> dict[str, set[str]]:
    """Hostnames, tags and addresses of what the worker builds from ``template``."""
    built = render_topology(template, RANGE_ID, lambda alias: alias)["vm_definitions"]
    hosts = {vm["name"][len(RANGE_ID[:8]) + 1:].lower() for vm in built}
    addresses = {nic["ip"] for vm in built for nic in vm["nics"] if nic.get("ip")}
    tags: set[str] = set()
    nodes = template.get("nodes") or template.get("assets") or []
    for node in nodes:
        tags.update(str(t).lower() for t in node.get("tags") or [])
    return {"host": hosts, "tag": tags, "address": addresses}


def unresolved(scenario: dict, template: dict) -> list[str]:
    inv = inventory(template)
    out = []
    for kind, value in sorted(references(scenario)):
        # A host tag may also name a host (insider-threat-advanced: host_tag "ws03").
        ok = value in inv[kind] or (kind == "tag" and value in inv["host"])
        if not ok:
            out.append(f"{kind} {value}")
    return out


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_targets_resolve_against_the_template(name):
    scenario = _yaml(SCENARIOS[name])
    assert scenario["range_template"] == "small-enterprise"
    assert references(scenario) == EXPECTED[name]
    assert unresolved(scenario, _yaml(TEMPLATE)) == []


def test_these_are_all_the_scenarios_on_small_enterprise():
    found = {p for p in list((ROOT / "content/scenarios").glob("*/scenario.yaml"))
             + list((ROOT / "scenario-engine/examples").glob("*.yaml"))
             if (_yaml(p) or {}).get("range_template") == "small-enterprise"}
    assert found == set(SCENARIOS.values())


def test_tags_hosts_and_addresses_scenarios_rely_on():
    inv = inventory(_yaml(TEMPLATE))
    assert {"mgmt", "srv", "user", "users"} <= inv["tag"]
    assert {"dc-01", "ws-01", "ws-02", "ws-03", "jump-box"} <= inv["host"]
    assert "10.0.2.10" in inv["address"]


def test_the_runner_mock_range_is_this_template():
    """runner/run.py's standalone RangeContext names hosts and networks the template builds."""
    template = _yaml(TEMPLATE)
    built = render_topology(template, RANGE_ID, lambda alias: alias)
    vms = {vm["name"][len(RANGE_ID[:8]) + 1:]: vm["ip"] for vm in built["vm_definitions"]}
    nets = {n["name"]: n["cidr"] for n in built["network_definitions"]}
    ctx = build_range_context("standalone")
    for vm in ctx["vms"]:
        assert vms.get(vm["name"]) == vm["ip"], vm
    for net in ctx["networks"]:
        assert nets.get(net["name"]) == net["cidr"], net


# ── the check catches what it is for ───────────────────────────────────

OLD_ASSETS_SKETCH = """
name: small-enterprise
version: "0.1"
inputs: {range_id: string, cidr_mgmt: 10.10.0.0/24, cidr_user: 10.20.0.0/24, cidr_srv: 10.30.0.0/24}
assets:
  - {role: jump, type: vm, os: ubuntu-2404, count: 1, tags: [mgmt]}
  - {role: dc, type: vm, os: windows-server-2022, count: 1, tags: [srv]}
  - {role: user, type: vm, os: windows-11, count: 3, tags: [user]}
"""


def test_the_old_assets_sketch_did_not_resolve():
    old = yaml.safe_load(OLD_ASSETS_SKETCH)
    assert unresolved(_yaml(SCENARIOS["apt-breach"]), old) == ["host dc-01", "host ws-01"]
    assert unresolved(_yaml(SCENARIOS["insider-threat"]), old) == ["address 10.0.2.10", "host dc-01"]
    assert unresolved(_yaml(SCENARIOS["ransomware-lite"]), old) == ["host ws-02", "tag users"]


def test_a_missing_host_tag_or_address_is_reported():
    template = _yaml(TEMPLATE)
    scenario = {"timeline": [
        {"action": "simulated_execution", "params": {"target": "ws-09"}},
        {"action": "dns_spike", "params": {"src_tag": "finance"}},
        {"action": "http_burst", "params": {"url": "http://10.0.9.9/x"}},
    ], "objectives": [{"id": "o", "params": {"prompt": "Isolate host DB-01 now"}}]}
    assert unresolved(scenario, template) == ["address 10.0.9.9", "host db-01", "host ws-09", "tag finance"]
    # A tag that names a host resolves (the house convention elsewhere: host_tag "ws03").
    assert unresolved({"timeline": [{"params": {"host_tag": "ws-01"}}]}, template) == []
