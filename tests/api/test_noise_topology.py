"""Noise from a range template: which nodes get agents, what personas talk to, and the
deploy hand-off to the worker.

The agreement test is the one that matters most: the worker builds each agent VM's
management NIC (render.py) and the API registers the agent at that same address
(app/noise/topology.py). If they disagree, agents are unreachable.
"""

import copy
import os
import stat
from pathlib import Path

import pytest
import yaml
from app.noise import topology

REPO = Path(__file__).resolve().parents[2]
RVB = yaml.safe_load((REPO / "content/ranges/red-vs-blue/template.yaml").read_text())
DEV_TENANT = "00000000-0000-0000-0000-000000000001"


def _with_noise(template=RVB, **block):
    t = copy.deepcopy(template)
    t["noise"] = {"enabled": True, **block}
    return t


class TestAgentNodes:
    def test_off_unless_enabled(self):
        assert topology.agent_nodes(RVB) == []

    def test_workstations_and_traffic_generator_outside_excluded_vlans(self):
        nodes = {n["node"]: n for n in topology.agent_nodes(_with_noise())}
        assert {"ws01", "lnx01", "tgen01"} <= set(nodes)
        vlans = {n["zone"] for n in nodes.values()}
        assert not any(v in vlans for v in ("red_team", "blue_team", "management"))
        assert nodes["ws01"]["platform"] == "windows" and nodes["lnx01"]["platform"] == "linux"

    def test_node_level_opt_in_and_out(self):
        t = _with_noise()
        for node in t["nodes"]:
            if node["id"] == "ws01":
                node["noise"] = {"agent": False}
            if node["id"] == "db01":
                node["noise"] = {"agent": True}
        names = {n["node"] for n in topology.agent_nodes(t)}
        assert "ws01" not in names and "db01" in names

    def test_explicit_exclusions_replace_the_default(self):
        t = _with_noise()
        t["nodes"].append({"id": "analyst01", "role": "workstation", "vlan": "blue_team", "os": "ubuntu-2404"})
        assert "analyst01" not in {n["node"] for n in topology.agent_nodes(t)}
        t["noise"]["exclude_vlans"] = ["red_team"]
        assert "analyst01" in {n["node"] for n in topology.agent_nodes(t)}

    def test_mgmt_addresses_are_unique_and_inside_the_mgmt_net(self):
        import ipaddress

        nodes = topology.agent_nodes(_with_noise(mgmt={"vlan_id": 3999, "cidr": "172.31.9.0/24"}))
        ips = [n["mgmt_ip"] for n in nodes]
        assert len(set(ips)) == len(ips)
        assert all(ipaddress.ip_address(ip) in ipaddress.ip_network("172.31.9.0/24") for ip in ips)
        assert {n["mgmt_vlan"] for n in nodes} == {3999}


class TestTargets:
    def test_derived_from_the_templates_servers(self):
        t = topology.derive_targets(_with_noise())
        assert t["dc"] == ["10.30.0.10", "10.30.0.11"] and t["ntp"] == t["dc"]
        assert any(u.startswith("http://10.20.0.") for u in t["web"])
        assert t["mail"] and t["share"]
        assert all(h.endswith(".corp.local") for h in t["dns"])

    def test_nothing_from_excluded_vlans(self):
        t = topology.derive_targets(_with_noise())
        flat = [v for values in t.values() for v in values]
        assert not any("10.66.0." in v or "10.0.0." in v for v in flat), flat

    def test_every_derived_target_passes_validation(self):
        from app.noise import targets

        for pool, values in topology.derive_targets(_with_noise()).items():
            for v in values:
                assert targets.problem(pool, v) is None, (pool, v)


def test_worker_and_api_agree_on_agent_nodes_and_addresses():
    from worker.render import render_topology

    t = _with_noise(mgmt={"vlan_id": 4001, "cidr": "10.255.0.0/24"})
    rendered = render_topology(t, "abcdef0123456789", lambda alias: alias)
    # VM names are "<first 8 of range id>-<hostname>".
    worker_side = {vm["name"][9:]: vm["mgmt"] for vm in rendered["vm_definitions"] if "mgmt" in vm}
    api_side = {
        n["node"]: {"vlan_id": n["mgmt_vlan"], "ip": n["mgmt_ip"], "prefix": n["mgmt_prefix"]}
        for n in topology.agent_nodes(t)
    }
    assert worker_side == api_side and api_side
    mgmt_nets = [n for n in rendered["network_definitions"] if n["name"] == "noise_mgmt"]
    assert len(mgmt_nets) == 1 and mgmt_nets[0]["gateway"] == ""


def test_worker_adds_no_mgmt_nic_when_noise_is_off():
    from worker.render import render_topology

    rendered = render_topology(RVB, "abcdef0123456789", lambda alias: alias)
    assert not any("mgmt" in vm for vm in rendered["vm_definitions"])
    assert not any(n["name"] == "noise_mgmt" for n in rendered["network_definitions"])


# ── Deploy endpoint ────────────────────────────────────────────────────
@pytest.fixture
def noisy_range(db_session):
    from app.models import Range, Template

    t = _with_noise(preset="busy", seed=11, personas=9, mgmt={"controller_url": "https://10.255.0.1/api"})
    tpl = Template(name="rvb-noise", version="1.0", yaml=yaml.safe_dump(t), tenant_id=DEV_TENANT)
    db_session.add(tpl)
    db_session.flush()
    r = Range(name="RvB noisy", template_id=tpl.id, tenant_id=DEV_TENANT, state="ready")
    db_session.add(r)
    db_session.commit()
    return r


@pytest.fixture
def dispatched(monkeypatch):
    sent = []

    def fake(task, *args):
        sent.append((task, args))
        return "task-123"

    monkeypatch.setattr("app.routers.noise._dispatch", fake)
    return sent


class TestDeploy:
    def test_dry_run_changes_nothing(self, client, noisy_range, dispatched):
        body = client.post(f"/noise/ranges/{noisy_range.id}/deploy", json={"dry_run": True}).json()
        assert body["dry_run"] and {a["node"] for a in body["agents"]} >= {"lnx01", "tgen01"}
        assert {s["node"] for s in body["skipped"]} >= {"ws01"}  # Windows, for now
        assert not dispatched
        assert client.get(f"/noise/ranges/{noisy_range.id}/agents").json() == []

    def test_deploy_registers_agents_roster_and_targets_and_hands_off(self, client, noisy_range, dispatched):
        r = client.post(f"/noise/ranges/{noisy_range.id}/deploy", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["task_id"] == "task-123" and "token" not in str(body)
        prof = client.get(f"/noise/ranges/{noisy_range.id}").json()
        assert prof["enabled"] and prof["level"] == 70 and prof["seed"] == 11
        assert prof["targets"] == body["targets"]
        assert len(client.get(f"/noise/ranges/{noisy_range.id}/personas").json()) == 9

        ((task, (inventory,)),) = dispatched
        assert task == "deploy_noise_agents"
        assert inventory["controller_url"] == "https://10.255.0.1/api"
        by_node = {a["node"]: a for a in inventory["agents"]}
        assert set(by_node) == {a["node"] for a in body["agents"]}
        # The handed-off tokens are the live ones.
        tok = by_node["lnx01"]["token"]
        assert client.get("/noise/agent/plan", headers={"X-Noise-Agent-Token": tok}).status_code == 200

    def test_redeploy_rotates_tokens_and_keeps_the_roster(self, client, noisy_range, dispatched):
        client.post(f"/noise/ranges/{noisy_range.id}/deploy", json={})
        old = dispatched[0][1][0]["agents"][0]["token"]
        roster = client.get(f"/noise/ranges/{noisy_range.id}/personas").json()
        client.post(f"/noise/ranges/{noisy_range.id}/deploy", json={})
        assert client.get("/noise/agent/plan", headers={"X-Noise-Agent-Token": old}).status_code == 401
        assert client.get(f"/noise/ranges/{noisy_range.id}/personas").json() == roster

    def test_queue_down_changes_nothing(self, client, noisy_range, monkeypatch):
        monkeypatch.setattr("app.routers.noise._dispatch", lambda *a: None)
        assert client.post(f"/noise/ranges/{noisy_range.id}/deploy", json={}).status_code == 503
        assert client.get(f"/noise/ranges/{noisy_range.id}/agents").json() == []

    def test_range_must_be_ready(self, client, noisy_range, db_session, dispatched):
        noisy_range.state = "provisioning"
        db_session.commit()
        assert client.post(f"/noise/ranges/{noisy_range.id}/deploy", json={}).status_code == 409
        assert client.post(f"/noise/ranges/{noisy_range.id}/deploy", json={"dry_run": True}).status_code == 200

    def test_template_without_noise_is_refused(self, client, db_session, dispatched):
        from app.models import Range, Template

        tpl = Template(name="plain", version="1.0", yaml=yaml.safe_dump(RVB), tenant_id=DEV_TENANT)
        db_session.add(tpl)
        db_session.flush()
        r = Range(name="plain", template_id=tpl.id, tenant_id=DEV_TENANT, state="ready")
        db_session.add(r)
        db_session.commit()
        assert client.post(f"/noise/ranges/{r.id}/deploy", json={}).status_code == 409

    def test_controller_url_must_be_https(self, client, db_session, dispatched, monkeypatch):
        from app.models import Range, Template

        monkeypatch.delenv("NOISE_CONTROLLER_URL", raising=False)
        tpl = Template(name="nourl", version="1.0", yaml=yaml.safe_dump(_with_noise()), tenant_id=DEV_TENANT)
        db_session.add(tpl)
        db_session.flush()
        r = Range(name="nourl", template_id=tpl.id, tenant_id=DEV_TENANT, state="ready")
        db_session.add(r)
        db_session.commit()
        assert client.post(f"/noise/ranges/{r.id}/deploy", json={}).status_code == 422


# ── Worker task ────────────────────────────────────────────────────────
INVENTORY = {
    "range_id": "r1",
    "controller_url": "https://10.255.0.1/api",
    "mgmt_cidr": "10.255.0.0/24",
    "agents": [{"node": "lnx01", "mgmt_ip": "10.255.0.10", "token": "s3cret-token-value-xxxxxxxx"}],
}


def test_inventory_keeps_tokens_out_of_the_inventory_and_private(tmp_path):
    from worker.noise_tasks import write_inventory

    inv = write_inventory(tmp_path, INVENTORY)
    assert "s3cret" not in inv.read_text() and "ansible_host=10.255.0.10" in inv.read_text()
    hv = tmp_path / "host_vars" / "lnx01.json"
    assert "s3cret" in hv.read_text()
    if os.name == "posix":
        assert stat.S_IMODE(hv.stat().st_mode) == 0o600


def test_mock_mode_runs_no_ansible(monkeypatch):
    from worker.noise_tasks import deploy_noise_agents

    monkeypatch.setenv("NOISE_DEPLOY_MODE", "mock")
    out = deploy_noise_agents.run(INVENTORY)
    assert out == {"status": "mock", "range_id": "r1", "nodes": ["lnx01"]}
    assert "s3cret" not in str(out)


def test_ansible_role_and_playbook_exist():
    role = REPO / "infra/ansible/roles/noise_agent"
    assert (role / "tasks/main.yml").exists() and (role / "templates/tn-noise.service.j2").exists()
    tasks = yaml.safe_load((role / "tasks/main.yml").read_text())
    token_task = next(t for t in tasks if "token" in t["name"].lower() and "copy" in str(t))
    assert token_task.get("no_log") is True
    play = yaml.safe_load((REPO / "infra/ansible/playbooks/deploy-noise-agents.yml").read_text())
    assert play[0]["hosts"] == "noise_agents" and "noise_agent" in play[0]["roles"]
