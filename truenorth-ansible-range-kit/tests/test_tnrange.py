"""Compiler tests. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "tools"))

from tnrange import drift, gates  # noqa: E402
from tnrange.allocate import address_instance, assign_slots  # noqa: E402
from tnrange.common import RangeError  # noqa: E402
from tnrange.manifest import build_manifest  # noqa: E402
from tnrange.plan import build_plan, verify_plan, write_plan  # noqa: E402
from tnrange.scenario import scenario_to_range  # noqa: E402


class Base(unittest.TestCase):
    """Each test gets a private copy of the kit with site/templates filled in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for d in ("config", "blueprints", "ranges"):
            shutil.copytree(KIT / d, self.root / d)
        (self.root / "ranges" / ".allocations.json").unlink(missing_ok=True)
        site = self.load("config/site.yml")
        s = site["site"]
        s["vcenter"].update(datacenter="DC0", cluster="C0", dvs="RANGE-DVS", datastores_allowed=["DS0"])
        s["isolation_mode"] = "vlan_trunk_no_l3"
        s["isolation_probe_targets"] = [{"host": "192.0.2.1", "port": 443}]
        self.dump("config/site.yml", site)
        t = self.load("config/templates.yml")
        for a, e in t["templates"].items():
            e.update(vcenter_name=f"tmpl-{a}", build_id=f"b-{a}", build_date="2026-10-01",
                     source_sha256="a" * 64, validated=True)
        self.dump("config/templates.yml", t)
        self.edit_range("CANARY", lambda d: d["range"].update(owner="test"))
        self.edit_range("EXCOURSE", lambda d: d["range"].update(owner="test", expires="2099-01-01"))

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, rel):
        return yaml.safe_load((self.root / rel).read_text())

    def dump(self, rel, data):
        (self.root / rel).write_text(yaml.safe_dump(data, sort_keys=False))

    def edit_range(self, rid, fn):
        d = self.load(f"ranges/{rid}.yml")
        fn(d)
        self.dump(f"ranges/{rid}.yml", d)

    def edit_site(self, fn):
        d = self.load("config/site.yml")
        fn(d["site"])
        self.dump("config/site.yml", d)


class PlanTests(Base):
    def test_canary_is_buildable_and_deterministic(self):
        p1 = build_plan(self.root, "CANARY", write_allocation=True)
        p2 = build_plan(self.root, "CANARY")
        self.assertTrue(p1["buildable"], p1["blockers"])
        self.assertEqual(p1["plan_sha256"], p2["plan_sha256"])
        inst = p1["instances"][0]
        self.assertEqual([n["vlan"] for n in inst["networks"]], [2000, 2001])
        rtr = next(v for v in inst["vms"] if v["name"] == "core-rtr01")
        self.assertEqual(sorted(n["ip"] for n in rtr["nics"]), ["10.200.0.1", "10.200.1.1"])
        self.assertTrue(all(n["gateway"] is None for n in rtr["nics"]))
        dc = next(v for v in inst["vms"] if v["name"] == "corp-dc01")
        self.assertEqual(dc["nics"][0]["gateway"], "10.200.0.1")
        self.assertIn("TN_VAR_DOMAIN=corp.local", dc["guest_env"])

    def test_tbd_site_blocks_build(self):
        self.edit_site(lambda s: s["vcenter"].update(dvs="TBD"))
        p = build_plan(self.root, "CANARY", write_allocation=True)
        self.assertFalse(p["buildable"])
        self.assertIn("site.vcenter.dvs is TBD", p["blockers"])

    def test_unvalidated_template_blocks_build(self):
        t = self.load("config/templates.yml")
        t["templates"]["kali"]["validated"] = False
        self.dump("config/templates.yml", t)
        p = build_plan(self.root, "CANARY", write_allocation=True)
        self.assertFalse(p["buildable"])
        self.assertTrue(any("kali" in b for b in p["blockers"]))

    def test_unrecorded_allocation_blocks_build(self):
        p = build_plan(self.root, "CANARY", write_allocation=False)
        self.assertFalse(p["buildable"])

    def test_stale_plan_detected(self):
        write_plan(self.root, build_plan(self.root, "CANARY", write_allocation=True))
        verify_plan(self.root, "CANARY", require_buildable=True)
        self.edit_range("CANARY", lambda d: d["use"][0]["params"].update(workstations=3))
        with self.assertRaises(RangeError):
            verify_plan(self.root, "CANARY")

    def test_students_get_distinct_stable_slots(self):
        p = build_plan(self.root, "EXCOURSE", write_allocation=True)
        ids = [i["id"] for i in p["instances"]]
        self.assertEqual(ids, [f"EXCOURSE-S0{n}" for n in range(1, 5)])
        vlans = [n["vlan"] for i in p["instances"] for n in i["networks"]]
        self.assertEqual(len(vlans), len(set(vlans)))
        build_plan(self.root, "CANARY", write_allocation=True)  # allocates after EXCOURSE
        p2 = build_plan(self.root, "EXCOURSE")
        self.assertEqual(p["plan_sha256"], p2["plan_sha256"])

    def test_reserved_vlans_and_cidrs_are_skipped(self):
        self.edit_site(lambda s: s.update(reserved_vlans=[2005], reserved_cidrs=["10.200.16.0/24"]))
        p = build_plan(self.root, "CANARY", write_allocation=True)
        inst = p["instances"][0]
        # slot 0 holds 2005 -> skipped; slot 1 overlaps 10.200.16.0/24 -> skipped; slot 2 used
        self.assertEqual(inst["slot"], 2)
        self.assertNotIn(2005, [n["vlan"] for n in inst["networks"]])

    def test_live_collision_is_refused_for_recorded_slot(self):
        build_plan(self.root, "CANARY", write_allocation=True)
        (self.root / "discovery").mkdir()
        (self.root / "discovery" / "live-network.json").write_text(json.dumps(
            {"vlans_in_use": [{"vlan": 2000, "portgroup": "Prod-Web"}]}))
        with self.assertRaises(RangeError) as cm:
            build_plan(self.root, "CANARY")
        self.assertIn("Prod-Web", str(cm.exception))

    def test_own_portgroup_is_not_a_collision(self):
        build_plan(self.root, "CANARY", write_allocation=True)
        (self.root / "discovery").mkdir()
        (self.root / "discovery" / "live-network.json").write_text(json.dumps(
            {"vlans_in_use": [{"vlan": 2000, "portgroup": "TN-CANARY-corp-lan"}]}))
        self.assertTrue(build_plan(self.root, "CANARY")["buildable"])

    def test_unknown_network_reference(self):
        self.edit_range("CANARY", lambda d: d["use"][2]["params"].update(routed_networks=["@nope-net"]))
        with self.assertRaises(RangeError) as cm:
            build_plan(self.root, "CANARY")
        self.assertIn("unknown network 'nope-net'", str(cm.exception))

    def test_probe_must_exist_and_be_linux(self):
        self.edit_range("CANARY", lambda d: d["validation"].update(probe_vm="corp-dc01"))
        with self.assertRaises(RangeError) as cm:
            build_plan(self.root, "CANARY")
        self.assertIn("must be a linux template", str(cm.exception))

    def test_windows_hostname_limit(self):
        self.edit_range("CANARY", lambda d: d.update(vms=[{
            "name": "workstation12345", "template": "win11", "role": "ws", "cpu": 1, "memory_gb": 1,
            "disk_gb": 10, "nics": ["corp-lan"]}]))
        with self.assertRaises(RangeError) as cm:
            build_plan(self.root, "CANARY")
        self.assertIn("15 characters", str(cm.exception))

    def test_blueprint_param_bounds(self):
        self.edit_range("CANARY", lambda d: d["use"][0]["params"].update(domain_controllers=9))
        with self.assertRaises(RangeError) as cm:
            build_plan(self.root, "CANARY")
        self.assertIn("above maximum", str(cm.exception))

    def test_isolation_always_checked_and_selftest_present(self):
        self.edit_range("CANARY", lambda d: d["validation"].update(checks=["services"]))
        p = build_plan(self.root, "CANARY", write_allocation=True)
        self.assertIn("isolation", p["validation"]["checks"])
        kinds = [x["check"] for x in p["instances"][0]["probes"]]
        self.assertEqual(kinds[0], "selftest")
        self.assertIn("isolation", kinds)

    def test_expired_range_blocks(self):
        self.edit_range("CANARY", lambda d: d["range"].update(expires="2020-01-01"))
        p = build_plan(self.root, "CANARY", write_allocation=True)
        self.assertFalse(p["buildable"])

    def test_file_name_must_match_id(self):
        self.edit_range("CANARY", lambda d: d["range"].update(id="OTHER"))
        with self.assertRaises(RangeError):
            build_plan(self.root, "CANARY")


class ScenarioTests(Base):
    def test_scenario_to_range_compiles(self):
        sc = self.root / "scn.yml"
        sc.write_text(yaml.safe_dump({"scenario": {
            "id": "SCN-0001", "title": "Phish to DA", "techniques": ["T1566.001", "T1003.001"],
            "assets": [{"role": "domain_controller", "count": 1}, {"role": "workstation", "count": 3},
                       {"role": "web_server", "count": 1}, {"role": "attacker", "count": 1}]}}))
        doc = scenario_to_range(self.root, sc, "SCN1", "2099-01-01", owner="test")
        self.dump("ranges/SCN1.yml", doc)
        p = build_plan(self.root, "SCN1", write_allocation=True)
        self.assertTrue(p["buildable"], p["blockers"])
        names = [v["name"] for v in p["instances"][0]["vms"]]
        self.assertIn("core-rtr01", names)
        self.assertEqual(sum(1 for n in names if n.startswith("corp-ws")), 3)
        self.assertEqual(p["source_scenario"]["techniques"], ["T1566.001", "T1003.001"])

    def test_unmapped_role(self):
        sc = self.root / "scn.yml"
        sc.write_text(yaml.safe_dump({"scenario": {"assets": [{"role": "scada_plc"}, {"role": "attacker"}]}}))
        with self.assertRaises(RangeError) as cm:
            scenario_to_range(self.root, sc, "SCN2", "2099-01-01")
        self.assertIn("scada_plc", str(cm.exception))


class EvidenceTests(Base):
    def _plan(self):
        p = build_plan(self.root, "CANARY", write_allocation=True)
        write_plan(self.root, p)
        return p

    def _results(self, p, isolation_pass=True):
        checks = [{"check": "selftest", "pass": True}, {"check": "isolation", "pass": isolation_pass}]
        (self.root / "build" / "CANARY" / "results.json").write_text(json.dumps({
            "action": "build", "plan_sha256": p["plan_sha256"],
            "instances": {"CANARY": {"validation": checks}}}))

    def test_manifest_pass_and_fail(self):
        p = self._plan()
        self._results(p, True)
        m = json.loads(build_manifest(self.root, "CANARY").read_text())
        self.assertEqual(m["verdict"], "PASS")
        self._results(p, False)
        m = json.loads(build_manifest(self.root, "CANARY").read_text())
        self.assertEqual(m["verdict"], "FAIL")

    def test_manifest_fails_without_isolation(self):
        p = self._plan()
        (self.root / "build" / "CANARY" / "results.json").write_text(json.dumps({
            "plan_sha256": p["plan_sha256"], "instances": {"CANARY": {"validation": [{"check": "selftest", "pass": True}]}}}))
        m = json.loads(build_manifest(self.root, "CANARY").read_text())
        self.assertEqual(m["verdict"], "FAIL")
        self.assertIn("no isolation checks were recorded", m["reasons"])

    def test_drift(self):
        p = self._plan()
        inst = p["instances"][0]
        live = {"instances": {"CANARY": {
            "vms": [{"name": v["vm_name"], "cpu": v["cpu"], "memory_mb": v["memory_mb"],
                     "portgroups": [n["portgroup"] for n in v["nics"]]} for v in inst["vms"]],
            "portgroups": [{"name": n["portgroup"], "vlan": n["vlan"]} for n in inst["networks"]]}}}
        self.assertEqual(drift.compare(p, live), [])
        live["instances"]["CANARY"]["vms"][0]["cpu"] = 99
        live["instances"]["CANARY"]["vms"].append({"name": "CANARY-rogue", "cpu": 1, "memory_mb": 1, "portgroups": []})
        live["untracked"] = [{"type": "VirtualMachine", "name": "someone-else"}]
        kinds = sorted(f["kind"] for f in drift.compare(p, live))
        self.assertEqual(kinds, ["cpu_mismatch", "tagged_untracked", "vm_unexpected"])


class GateTests(Base):
    def setUp(self):
        super().setUp()
        self._orig_confirm = gates._confirm_human
        gates._confirm_human = lambda rid, actions: None  # tests stand in for the typed confirmation
        self._orig_key = gates.KEY_FILE
        gates.KEY_FILE = self.root / "no-such-key"
        shutil.copytree(KIT / "playbooks", self.root / "playbooks")
        shutil.copytree(KIT / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        gates._confirm_human = self._orig_confirm
        gates.KEY_FILE = self._orig_key
        super().tearDown()

    def test_approve_refuses_without_terminal(self):
        gates._confirm_human = self._orig_confirm
        write_plan(self.root, build_plan(self.root, "CANARY", write_allocation=True))
        with self.assertRaises(RangeError) as cm:
            gates.approve(self.root, "CANARY", ["build"], 1, "x")
        self.assertIn("interactive terminal", str(cm.exception))

    def test_signed_approvals(self):
        key = self.root / "approval.key"
        key.write_text("s3cret-test-key")
        gates.KEY_FILE = key
        write_plan(self.root, build_plan(self.root, "CANARY", write_allocation=True))
        p = gates.approve(self.root, "CANARY", ["build"], 1, "x")
        gates.check_approval(self.root, "CANARY", "build")
        rec = json.loads(p.read_text())
        rec["actions"] = ["build", "teardown"]          # forged widening
        p.write_text(json.dumps(rec))
        with self.assertRaises(RangeError):
            gates.check_approval(self.root, "CANARY", "teardown")

    def test_unreadable_key_blocks_real_runs(self):
        (self.root / "config" / "security.json").write_text('{"require_signed_approvals": true}')
        write_plan(self.root, build_plan(self.root, "CANARY", write_allocation=True))
        with self.assertRaises(RangeError):            # cannot sign
            gates.approve(self.root, "CANARY", ["build"], 1, "x")
        now = dt.datetime.now(dt.timezone.utc)          # forged unsigned approval
        ap = gates.approval_path(self.root, "CANARY")
        ap.parent.mkdir(parents=True, exist_ok=True)
        ap.write_text(json.dumps({"range_id": "CANARY", "actions": ["build"],
                                  "expires": (now + dt.timedelta(hours=1)).isoformat()}))
        with self.assertRaises(RangeError) as cm:
            gates.check_approval(self.root, "CANARY", "build")
        self.assertIn("cannot read", str(cm.exception))

    def test_code_change_invalidates_approval(self):
        write_plan(self.root, build_plan(self.root, "CANARY", write_allocation=True))
        gates.approve(self.root, "CANARY", ["build"], 1, "x")
        f = self.root / "playbooks" / "roles" / "range_load" / "tasks" / "main.yml"
        f.write_text(f.read_text() + "\n# changed\n")
        with self.assertRaises(RangeError) as cm:
            gates.check_approval(self.root, "CANARY", "build")
        self.assertIn("different playbook/tool code", str(cm.exception))

    def test_approval_lifecycle(self):
        with self.assertRaises(RangeError):
            gates.check_approval(self.root, "CANARY", "build")
        with self.assertRaises(RangeError):  # no compiled plan yet
            gates.approve(self.root, "CANARY", ["build"], 1, "tester")
        write_plan(self.root, build_plan(self.root, "CANARY", write_allocation=True))
        gates.approve(self.root, "CANARY", ["build"], 1, "tester")
        gates.check_approval(self.root, "CANARY", "build")
        with self.assertRaises(RangeError):
            gates.check_approval(self.root, "CANARY", "teardown")
        with self.assertRaises(RangeError):
            gates.approve(self.root, "CANARY", ["build"], 48, "tester")
        # A recompile that changes the plan invalidates the approval.
        self.edit_range("CANARY", lambda d: d["use"][0]["params"].update(workstations=2))
        write_plan(self.root, build_plan(self.root, "CANARY", write_allocation=True))
        with self.assertRaises(RangeError) as cm:
            gates.check_approval(self.root, "CANARY", "build")
        self.assertIn("Re-approve", str(cm.exception))

    def test_edited_plan_file_is_rejected(self):
        write_plan(self.root, build_plan(self.root, "CANARY", write_allocation=True))
        pf = self.root / "build" / "CANARY" / "plan.json"
        p = json.loads(pf.read_text())
        p["instances"][0]["networks"][0]["portgroup"] = "PROD-Management"
        pf.write_text(json.dumps(p))
        with self.assertRaises(RangeError) as cm:
            verify_plan(self.root, "CANARY")
        self.assertIn("was edited", str(cm.exception))

    def test_expiry_does_not_make_plan_stale(self):
        write_plan(self.root, build_plan(self.root, "CANARY", write_allocation=True))
        self.edit_range("CANARY", lambda d: d["range"].update(expires="2099-12-31"))  # same as file
        verify_plan(self.root, "CANARY")

    def test_failed_compile_records_no_slot(self):
        self.edit_range("CANARY", lambda d: d["validation"].update(probe_vm="corp-dc01"))  # invalid probe
        with self.assertRaises(RangeError):
            build_plan(self.root, "CANARY", write_allocation=True)
        self.assertFalse((self.root / "ranges" / ".allocations.json").exists())

    def test_single_default_route_and_trunk_collision(self):
        from tnrange.preflight import check
        p = build_plan(self.root, "EXCOURSE", write_allocation=True)
        for v in p["instances"][0]["vms"]:
            self.assertLessEqual(sum(1 for n in v["nics"] if n["gateway"]), 1, v["vm_name"])
        own = p["instances"][0]["networks"][0]
        live = {"DVS": [{"portgroup_name": own["portgroup"], "vlan_info": {"trunk": False, "vlan_id": str(own["vlan"])}},
                        {"portgroup_name": "DVS-DVUplinks-12", "vlan_info": {"trunk": True, "vlan_id": ["0-4094"]}}]}
        self.assertEqual(check(p, live), [])
        live["DVS"].append({"portgroup_name": "Sniffer", "vlan_info": {"trunk": True, "vlan_id": ["1990-2010"]}})
        self.assertTrue(any("Sniffer" in e for e in check(p, live)))
        live["DVS"].append({"portgroup_name": "TN-EXCOURSE-S010-x", "vlan_info": {"trunk": False, "vlan_id": str(own["vlan"])}})
        self.assertTrue(any("TN-EXCOURSE-S010-x" in e for e in check(p, live)))

    def test_blackout(self):
        now = dt.datetime.now(dt.timezone.utc)
        (self.root / "config" / "blackout.json").write_text(json.dumps({"windows": [{
            "start": (now - dt.timedelta(minutes=5)).isoformat(),
            "end": (now + dt.timedelta(minutes=5)).isoformat(), "reason": "EX"}]}))
        with self.assertRaises(RangeError):
            gates.check_blackout(self.root)


if __name__ == "__main__":
    unittest.main()
