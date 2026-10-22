"""Range certification S5 (ADR 0013 §5, issue #243): teardown by independently identified
inventory, and drift. Worker side, against fakes and SQLite; the same paths against vcsim
are in tests/integration/test_vsphere_sim.py (``test_s5_*``).

* drift: VM id, CPU/RAM, NIC port group / MAC, port group VLAN and snapshot changes are
  each found, graded (isolation/identity -> fail, resources/snapshots -> incomplete), and a
  clean range passes; a range built before S5 is never called clean;
* teardown grading: zero recorded ids remaining is pass, any remaining is fail, anything
  unidentified, unchecked or legacy is incomplete;
* the ownership rule: a recorded instance UUID decides, whatever the VM is named or
  annotated now; a reused moid with another UUID is never the range's;
* the drift beat task stores the check, notifies on change and appends to the
  certification record only when the result changes; a failed read records nothing;
* the confirm task passes on only moids still listed, only while the range is torn down.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
from worker import range_drift, range_inventory, range_inventory_db, range_teardown, tables
from worker.provisioners import vsphere_api
from worker.provisioners.results import DestroyResult

UUID1, UUID2 = "4211aaaa-0000-0000-0000-000000000001", "4211aaaa-0000-0000-0000-000000000002"


def _identity(moid: str, uuid_: str, *, cpu=2, mem=4096, pg="dvportgroup-1", mac="00:50:56:3c:00:00") -> dict:
    return {"moid": moid, "instance_uuid": uuid_, "folder": "group-v9", "datastores": ["datastore-1"],
            "disks": [{"path": f"[ds1] {moid}/{moid}.vmdk", "datastore": "datastore-1"}],
            "num_cpu": cpu, "memory_mb": mem, "nics": [{"portgroup_key": pg, "network": None, "mac": mac}]}


def _output(**over) -> dict:
    out = {
        "vms": [{"vm_id": "vm-1", "name": "r-web", "identity": _identity("vm-1", UUID1)},
                {"vm_id": "vm-2", "name": "r-dc", "identity": _identity("vm-2", UUID2, pg="dvportgroup-2",
                                                                         mac="00:50:56:3c:00:01")}],
        "networks": [{"name": "lan", "vlan_id": 10, "physical_vlan": 100, "portgroup": "tn-r-v100",
                      "moid": "dvportgroup-1", "key": "dvportgroup-1", "switch_mode": "vds"},
                     {"name": "dmz", "vlan_id": 20, "physical_vlan": 101, "portgroup": "tn-r-v101",
                      "moid": "dvportgroup-2", "key": "dvportgroup-2", "switch_mode": "vds"}],
        "inventory": {"folder": {"moid": "group-v9", "name": "r-abc"}, "vlans": {"10": 100, "20": 101}},
    }
    out.update(over)
    return out


def _live(output: dict) -> dict:
    """What vCenter would answer for an untouched range."""
    vms = {}
    for vm in output["vms"]:
        ident = vm["identity"]
        vms[vm["vm_id"]] = {"exists": True, "name": vm["name"], "instance_uuid": ident["instance_uuid"],
                            "num_cpu": ident["num_cpu"], "memory_mb": ident["memory_mb"],
                            "nics": [dict(n) for n in ident["nics"]], "snapshots": []}
    pgs = {n["moid"]: {"exists": True, "name": n["portgroup"], "vlan": n["physical_vlan"]} for n in output["networks"]}
    return {"vms": vms, "portgroups": pgs, "errors": []}


# ── drift ───────────────────────────────────────────────────────────────────
class TestDrift:
    def test_an_untouched_range_passes(self):
        out = _output()
        result = range_inventory.compare(out, _live(out))
        assert (result["status"], result["findings"], result["checked"]) == ("pass", [], {"vms": 2, "portgroups": 2})

    @pytest.mark.parametrize(("change", "kind", "status"), [
        (lambda live: live["vms"]["vm-1"].update(instance_uuid="4211ffff-0000-0000-0000-000000000009"),
         "vm_replaced", "fail"),
        (lambda live: live["vms"]["vm-1"].update(exists=False), "vm_missing", "incomplete"),
        (lambda live: live["vms"]["vm-1"].update(num_cpu=8), "cpu_changed", "incomplete"),
        (lambda live: live["vms"]["vm-2"].update(memory_mb=8192), "memory_changed", "incomplete"),
        (lambda live: live["vms"]["vm-1"]["nics"][0].update(portgroup_key="dvportgroup-77"),
         "nic_portgroup_changed", "fail"),
        (lambda live: live["vms"]["vm-1"]["nics"][0].update(mac="00:50:56:aa:bb:cc"), "nic_mac_changed", "fail"),
        (lambda live: live["vms"]["vm-1"]["nics"].append({"portgroup_key": "dvportgroup-1", "mac": "00:50:56:01:01:01"}),
         "nic_added", "fail"),
        (lambda live: live["vms"]["vm-1"].update(nics=[]), "nic_removed", "fail"),
        (lambda live: live["portgroups"]["dvportgroup-1"].update(vlan=3999), "portgroup_vlan_changed", "fail"),
        (lambda live: live["portgroups"]["dvportgroup-2"].update(exists=False), "portgroup_missing", "fail"),
        (lambda live: live["vms"]["vm-2"]["snapshots"].append({"moid": "snapshot-9", "name": "by-hand"}),
         "snapshot_unexpected", "incomplete"),
    ])
    def test_each_change_is_found_and_graded(self, change, kind, status):
        out = _output()
        live = _live(out)
        change(live)
        result = range_inventory.compare(out, live)
        assert [f["kind"] for f in result["findings"]] == [kind]
        assert result["status"] == status

    def test_ram_change_names_both_values(self):
        out = _output()
        live = _live(out)
        live["vms"]["vm-1"]["memory_mb"] = 8192
        [finding] = range_inventory.compare(out, live)["findings"]
        assert finding == {"kind": "memory_changed", "object": "r-web", "expected": 4096, "actual": 8192}

    def test_snapshots_truenorth_took_are_known_by_id_or_name_and_a_lost_one_is_drift(self):
        out = _output()
        live = _live(out)
        live["vms"]["vm-1"]["snapshots"] = [{"moid": "snapshot-1", "name": "tnabc"}]
        live["vms"]["vm-2"]["snapshots"] = [{"moid": "snapshot-7", "name": "tnold"}]  # an older row: name only
        rows = [{"state": "ready", "backend_name": "tnabc",
                 "data": {"snapshot_name": "tnabc", "snapshot_ids": {"vm-1": "snapshot-1", "vm-2": "snapshot-2"}}},
                {"state": "ready", "backend_name": "tnold", "data": {"snapshot_name": "tnold"}},
                {"state": "deleted", "backend_name": "tngone", "data": {"snapshot_ids": {"vm-1": "snapshot-0"}}}]
        result = range_inventory.compare(out, live, rows)
        assert [(f["kind"], f["object"], f["expected"]) for f in result["findings"]] == [
            ("snapshot_missing", "r-dc", "snapshot-2", )]

    def test_a_range_built_before_s5_is_never_called_clean(self):
        out = _output(vms=[{"vm_id": "vm-1", "name": "r-web"}], networks=[{"portgroup": "tn-r-v100"}], inventory=None)
        result = range_inventory.compare(out, {"vms": {"vm-1": {"exists": True}}, "portgroups": {}})
        assert result["status"] == "incomplete" and result["gaps"]

    def test_a_vm_re_registered_under_another_moid_is_present_but_reported(self):
        out = _output()
        live = _live(out)
        live["vms"]["vm-1"].update(moved_to="vm-77", memory_mb=8192)  # found by its instance UUID
        result = range_inventory.compare(out, live)
        assert [(f["kind"], f.get("actual")) for f in result["findings"]] == [
            ("memory_changed", 8192), ("vm_reregistered", "vm-77")]  # still compared where it is now
        assert result["status"] == "incomplete"

    def test_a_vlan_that_could_not_be_read_is_a_gap_never_a_pass(self):
        out = _output()
        live = _live(out)
        live["portgroups"]["dvportgroup-1"] = {"exists": True, "name": "tn-r-v100", "vlan": None,
                                               "vlan_unreadable": True}
        result = range_inventory.compare(out, live)
        assert result["findings"] == [] and result["status"] == "incomplete"
        assert "could not be read" in result["gaps"][0]

    def test_unrecorded_items_carrying_the_mark_are_drift(self):
        out = _output()
        live = {**_live(out), "unidentified": [
            {"kind": "vm", "moid": "vm-90", "name": "re-created", "marks": ["annotation"]},
            {"kind": "portgroup", "moid": "dvportgroup-9", "name": "tn-r-v199", "marks": ["owner_mark"]}]}
        result = range_inventory.compare(out, live)
        assert [f["kind"] for f in result["findings"]] == ["unidentified_portgroup", "unidentified_vm"]
        assert result["status"] == "incomplete"

    def test_a_backup_tools_snapshots_are_not_drift(self):
        out = _output()
        live = _live(out)
        live["vms"]["vm-1"]["snapshots"] = [{"moid": "snapshot-3", "name": "VEEAM BACKUP TEMPORARY SNAPSHOT"},
                                            {"moid": "snapshot-4", "name": "before-patch"}]
        result = range_inventory.compare(out, live, (), ("VEEAM", "Veeam"))
        assert [(f["kind"], f.get("name")) for f in result["findings"]] == [("snapshot_unexpected", "before-patch")]

    def test_the_result_is_identical_run_to_run(self):
        out = _output()
        live = _live(out)
        live["vms"]["vm-1"].update(num_cpu=4, memory_mb=1)
        assert range_inventory.compare(out, live) == range_inventory.compare(json.loads(json.dumps(out)),
                                                                           json.loads(json.dumps(live)))


# ── teardown grading ────────────────────────────────────────────────────────
class TestTeardownStatus:
    def test_zero_remaining_and_nothing_unidentified_is_pass(self):
        assert range_inventory.teardown_status({"remaining": [], "unidentified": [], "not_checked": []})[0] == "pass"

    def test_remaining_ids_are_fail_and_named(self):
        status, summary = range_inventory.teardown_status(
            {"remaining": [{"kind": "disk", "id": "[ds1] r/r.vmdk"}, {"kind": "portgroup", "id": "dvportgroup-1"}]})
        assert status == "fail" and "2 recorded item(s)" in summary and "[ds1] r/r.vmdk" in summary

    @pytest.mark.parametrize("report", [
        {"unidentified": [{"kind": "vm", "moid": "vm-9"}]},
        {"not_checked": ["disk [ds1] r/r.vmdk: datastore unreadable"]},
        {"legacy": True},
    ])
    def test_unidentified_unchecked_or_legacy_is_incomplete(self, report):
        assert range_inventory.teardown_status({"remaining": [], **report})[0] == "incomplete"

    def test_no_report_is_incomplete(self):
        assert range_inventory.teardown_status(None)[0] == "incomplete"


# ── ownership: the recorded instance UUID decides ──────────────────────────────
class TestOwnership:
    VM = {"vm_id": "vm-1", "name": "abcd1234-web", "identity": {"instance_uuid": UUID1}}

    def test_a_renamed_re_annotated_vm_with_its_uuid_is_the_ranges(self):
        seen = {"name": "someone-renamed-me", "config.annotation": "not ours", "config.instanceUuid": UUID1}
        assert vsphere_api._not_ours("abcd1234-0000", self.VM, seen, "vc") is None

    def test_a_reused_moid_with_another_uuid_is_never_the_ranges_even_with_the_annotation(self):
        seen = {"name": "abcd1234-web", "config.annotation": vsphere_api.range_annotation("abcd1234-0000"),
                "config.instanceUuid": UUID2}
        note = vsphere_api._not_ours("abcd1234-0000", self.VM, seen, "vc")
        assert note and UUID1 in note and UUID2 in note

    def test_without_a_recorded_uuid_the_older_rule_still_applies(self):
        vm = {"vm_id": "vm-1", "name": "abcd1234-web"}
        ours = {"name": "x", "config.annotation": vsphere_api.range_annotation("abcd1234-0000")}
        assert vsphere_api._not_ours("abcd1234-0000", vm, ours, "vc") is None
        assert vsphere_api._not_ours("abcd1234-0000", vm, {"name": "x", "config.annotation": ""}, "vc")


    def test_every_operation_screens_with_the_recorded_identity(self):
        vm = {"vm_id": "vm-1", "name": "r-web", "identity": {"instance_uuid": UUID1}, "role_setup": {}}
        assert vsphere_api._screened(vm) == {"vm_id": "vm-1", "name": "r-web", "identity": {"instance_uuid": UUID1}}
        assert vsphere_api._screened({"vm_id": "vm-1", "name": "old"}) == {"vm_id": "vm-1", "name": "old"}


# ── the vSphere readings (pyVmomi calls stubbed at read_props) ─────────────────
class _Ref:
    def __init__(self, moid):
        self._moId = moid


class TestVsphereReadings:
    def test_a_standard_port_groups_vlan_is_read_on_every_host(self, monkeypatch):
        from worker.provisioners import vsphere_inventory as inv

        def pg(name, vlan):
            return SimpleNamespace(spec=SimpleNamespace(name=name, vlanId=vlan))

        hosts = {"host-1": {"config.network.portgroup": [pg("tn-r-v100", 100)]},
                 "host-2": {"config.network.portgroup": [pg("tn-r-v100", 3999)]},
                 "host-3": {"config.network.portgroup": None}}  # unreadable

        def read_props(si, vimtype, ids, paths):
            if "host" in paths:
                return {"network-1": {"name": "tn-r-v100", "host": [_Ref("host-1"), _Ref("host-2")]},
                        "network-2": {"name": "tn-r-v100", "host": [_Ref("host-1"), _Ref("host-3")]},
                        "network-3": {"name": "tn-r-v100", "host": [_Ref("host-1")]}}, set()
            return {h: hosts[h] for h in ids}, set()

        monkeypatch.setattr(inv, "read_props", read_props)
        got = inv._vss_live(None, ["network-1", "network-2", "network-3"])
        assert got["network-1"]["vlan"] == [100, 3999]  # hosts disagree: never the record
        assert got["network-2"]["vlan_unreadable"] is True
        assert got["network-3"]["vlan"] == 100

    def test_a_missing_moid_is_looked_up_by_its_instance_uuid(self, monkeypatch):
        from worker.provisioners import vsphere_inventory as inv

        def read_props(si, vimtype, ids, paths):
            props = {"vm-77": {"name": "r-web", "config.instanceUuid": UUID1, "config.hardware.memoryMB": 4096}}
            return {i: props[i] for i in ids if i in props}, {i for i in ids if i not in props}

        monkeypatch.setattr(inv, "read_props", read_props)
        si = SimpleNamespace(content=SimpleNamespace(searchIndex=SimpleNamespace(
            FindByUuid=lambda uuid, vmSearch, instanceUuid: _Ref("vm-77") if uuid == UUID1 else None)))  # noqa: N803
        got = inv.live(si, [{"moid": "vm-1", "instance_uuid": UUID1}, {"moid": "vm-2", "instance_uuid": UUID2}], [])
        assert got["vms"]["vm-1"]["moved_to"] == "vm-77" and got["vms"]["vm-1"]["instance_uuid"] == UUID1
        assert got["vms"]["vm-2"] == {"exists": False}

    def test_a_sweep_reads_all_its_ranges_in_one_call_each(self, monkeypatch):
        from worker.provisioners import vsphere_inventory as inv

        calls = {"live": 0, "vm_marks": 0, "pg_marks": 0}

        def live(si, vms, portgroups):
            calls["live"] += 1
            return {"vms": {v["moid"]: {"exists": True, "instance_uuid": v["instance_uuid"]} for v in vms},
                    "portgroups": {}, "errors": []}

        def count(key, value):
            def f(*a):
                calls[key] += 1
                return value
            return f

        monkeypatch.setattr(inv, "live", live)
        monkeypatch.setattr(inv, "all_vm_marks", count("vm_marks", {}))
        monkeypatch.setattr(inv, "portgroup_marks", count("pg_marks", {}))
        prov = vsphere_api.VsphereAPIProvisioner()
        monkeypatch.setattr(prov, "_find", lambda *a: None)
        items = [(f"r{i}", _output(vms=[{"vm_id": f"vm-{i}", "name": f"r{i}",
                                          "identity": _identity(f"vm-{i}", f"uuid-{i}")}])) for i in range(3)]
        got = prov._live_many_sync(object(), items, {})
        assert calls == {"live": 1, "vm_marks": 1, "pg_marks": 1}
        assert {rid: list(r["vms"]) for rid, r in got.items()} == {"r0": ["vm-0"], "r1": ["vm-1"], "r2": ["vm-2"]}


# ── the database, and the two tasks ─────────────────────────────────────────────
TENANT = str(uuid.uuid4())


@pytest.fixture
def s5db(db):
    """The worker test database (conftest ``db``) with what S5 reads and writes."""
    with db.begin() as conn:
        conn.execute(sa.text("ALTER TABLE ranges ADD COLUMN tenant_id TEXT"))
        conn.execute(sa.text("ALTER TABLE ranges ADD COLUMN deleted_at TIMESTAMP"))
    for t in (tables.range_inventory_checks, tables.range_certification_records, tables.range_certification_entries,
              tables.range_operations):
        t.create(db)
    return db


def _range(db, state: str, output: dict | None) -> str:
    rid = str(uuid.uuid4())
    with db.begin() as conn:
        conn.execute(sa.text("INSERT INTO ranges (id, name, state, provisioner_backend, provisioner_output, tenant_id) "
                             "VALUES (:id, 'S5 range', :s, 'vsphere_api', :o, :t)"),
                     {"id": rid, "s": state, "o": json.dumps(output) if output is not None else None, "t": TENANT})
        conn.execute(tables.range_certification_records.insert().values(
            id=uuid.uuid4(), deployment_id=uuid.uuid4(), generation=1, tenant_id=TENANT, range_id=rid,
            range_name="S5 range", template_id=None, template_name="t", template_version="1", plan_hash="h",
            plan_hash_basis="b", prior_output_sha256="", opened_by_id=None, opened_by="x",
            opened_at=datetime.now(UTC)))
    return rid


def _entries(db, rid: str) -> list:
    e, r = tables.range_certification_entries, tables.range_certification_records
    with db.connect() as conn:
        return conn.execute(sa.select(e.c.seq, e.c.section, e.c.status, e.c.source, e.c.actor)
                            .join(r, r.c.id == e.c.record_id).where(r.c.range_id == rid).order_by(e.c.seq)).all()


def _check(db, rid: str, kind: str):
    with db.connect() as conn:
        return range_inventory_db.load_check(conn, rid, kind)


class _Prov:
    """A backend for the sweep: one endpoint, readings handed out per range; counts its reads."""

    supports_inventory = True

    def __init__(self, readings: dict, during=None):
        self.readings, self.during, self.calls = readings, during, []

    def connection_record(self):
        return {"id": "c1", "host": "https://vc"}

    async def live_inventories(self, items):
        self.calls.append([rid for rid, _ in items])
        if self.during:
            self.during()  # something happening while vCenter is being read
        out = {}
        for rid, _ in items:
            got = self.readings.get(rid)
            if isinstance(got, Exception):
                raise got
            out[rid] = got
        return out


class TestDriftTask:
    def _run(self, monkeypatch, readings: dict, during=None) -> dict:
        self.prov = _Prov(readings, during)
        monkeypatch.setattr(range_drift.range_sweep.base_tasks, "_get_backend", lambda *a, **k: self.prov)
        return range_drift.check_range_drift()

    def test_a_change_is_stored_notified_and_recorded_once(self, s5db, monkeypatch):
        out = _output()
        rid = _range(s5db, "ready", out)
        notes = []
        monkeypatch.setattr(range_drift, "notify_api", lambda ch, msg: notes.append(msg))
        live = _live(out)
        self._run(monkeypatch, {rid: live})
        self._run(monkeypatch, {rid: live})  # unchanged: no second entry, no second note
        assert _check(s5db, rid, "drift")["status"] == "pass"
        assert [tuple(e)[1:] for e in _entries(s5db, rid)] == [("drift", "pass", "worker.s5", "system")]
        live["vms"]["vm-1"]["memory_mb"] = 8192  # RAM changed by hand in vCenter
        summary = self._run(monkeypatch, {rid: live})
        check = _check(s5db, rid, "drift")
        assert check["status"] == "incomplete" and check["report"]["findings"][0]["kind"] == "memory_changed"
        assert summary["drifted"] == [rid]
        assert [e[2] for e in _entries(s5db, rid)] == ["pass", "incomplete"]
        assert [n["event"] for n in notes] == ["drift_cleared", "drift_detected"]

    def test_a_failed_read_is_stored_as_an_error_and_recorded_nowhere(self, s5db, monkeypatch):
        rid = _range(s5db, "running", _output())
        self._run(monkeypatch, {rid: RuntimeError("vCenter unreachable")})
        check = _check(s5db, rid, "drift")
        assert check["status"] == "error" and "vCenter unreachable" in check["summary"]
        assert _entries(s5db, rid) == []

    def test_only_built_idle_recorded_ranges_are_read(self, s5db, monkeypatch):
        idle = _range(s5db, "stopped", _output())
        _range(s5db, "destroyed", _output())
        _range(s5db, "ready", {"vms": [{"vm_id": "vm-1", "name": "old"}]})  # built before S5: nothing to compare
        busy = _range(s5db, "ready", _output())
        with s5db.begin() as conn:
            conn.execute(tables.range_leases.insert().values(range_id=busy, holder="restore:x",
                                                             expires_at=datetime.now(UTC) + timedelta(minutes=3)))
        in_flight = _range(s5db, "ready", _output())
        _operation(s5db, in_flight)
        self._run(monkeypatch, {})
        assert self.prov.calls == [[idle]]

    def test_every_range_of_a_vcenter_is_read_in_one_call_and_saved_as_it_goes(self, s5db, monkeypatch):
        out = _output()
        ids = [_range(s5db, "ready", out) for _ in range(3)]
        self._run(monkeypatch, dict.fromkeys(ids, _live(out)))
        assert [sorted(c) for c in self.prov.calls] == [sorted(ids)]
        assert all(_check(s5db, rid, "drift")["status"] == "pass" for rid in ids)

    def test_a_range_that_changed_during_the_read_records_nothing(self, s5db, monkeypatch):
        out = _output()
        quiet, busy = _range(s5db, "ready", out), _range(s5db, "ready", out)
        live = _live(out)
        live["vms"]["vm-1"]["snapshots"] = [{"moid": "snapshot-5", "name": "tnnew"}]  # mid-snapshot: looks drifted
        summary = self._run(monkeypatch, {quiet: _live(out), busy: live}, during=lambda: _operation(s5db, busy))
        assert summary["skipped"] == [busy]
        assert _check(s5db, busy, "drift") is None and _entries(s5db, busy) == []  # no false drift recorded
        assert _check(s5db, quiet, "drift")["status"] == "pass"

    def test_a_snapshot_being_taken_during_the_read_records_nothing(self, s5db, monkeypatch):
        out = _output()
        rid = _range(s5db, "ready", out)

        def snapshot_starts():
            with s5db.begin() as conn:
                conn.execute(sa.text("INSERT INTO range_snapshots (id, range_id, snapshot_state) "
                                     "VALUES (:i, :r, 'creating')"), {"i": str(uuid.uuid4()), "r": rid})
        summary = self._run(monkeypatch, {rid: _live(out)}, during=snapshot_starts)
        assert summary["skipped"] == [rid] and _entries(s5db, rid) == []

    def test_an_overlapping_sweep_does_nothing(self, s5db, monkeypatch):
        import contextlib

        monkeypatch.setattr(range_drift, "sweep_lock", lambda: contextlib.nullcontext(False))
        _range(s5db, "ready", _output())
        assert range_drift.check_range_drift()["status"] == "skipped"


def _operation(db, rid: str, status: str = "dispatched") -> None:
    now = datetime.now(UTC)
    with db.begin() as conn:
        conn.execute(tables.range_operations.insert().values(
            id=uuid.uuid4(), tenant_id=TENANT, range_id=rid, action="restore", generation=1, request_hash="h",
            status=status, dispatch_attempts=1, created_at=now, updated_at=now))


class TestTeardownRecord:
    def test_the_report_is_graded_and_stored(self, s5db):
        rid = _range(s5db, "destroying", _output())
        report = {"deleted": [{"kind": "vm", "id": "vm-1"}], "remaining": [], "not_checked": [],
                  "unidentified": [{"kind": "vm", "moid": "vm-9", "name": "stray", "marks": ["annotation"]}]}
        status = range_teardown.record(lambda: _Session(s5db), rid, DestroyResult(status="ok", teardown=report))
        assert status == "incomplete"
        check = _check(s5db, rid, "teardown")
        assert check["report"]["unidentified"][0]["moid"] == "vm-9" and "1 unidentified" in check["summary"]

    def test_a_backend_that_verifies_nothing_stores_nothing(self, s5db):
        rid = _range(s5db, "destroying", _output())
        assert range_teardown.record(lambda: _Session(s5db), rid, DestroyResult(status="ok")) is None
        assert _check(s5db, rid, "teardown") is None


class _Session:
    """A commit-on-exit connection, as the tasks' _db_session is."""

    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        self.conn = self.engine.connect()
        self.tx = self.conn.begin()
        return self.conn

    def __exit__(self, exc_type, *_):
        (self.tx.rollback if exc_type else self.tx.commit)()
        self.conn.close()


LISTED = [{"kind": "vm", "moid": "vm-9", "name": "stray", "instance_uuid": UUID2, "marks": ["annotation"]},
          {"kind": "vm", "moid": "vm-8", "name": "other", "instance_uuid": UUID1, "marks": ["tag"]}]


class TestConfirmTask:
    def _prepare(self, db, state: str):
        rid = _range(db, state, _output())
        with _Session(db) as conn:
            range_inventory_db.save_check(conn, rid, "teardown", "incomplete", "2 unidentified",
                                          {"remaining": [], "unidentified": LISTED,
                                           "deleted": [{"kind": "vm", "id": "vm-1"}]})
        prov = MagicMock(supports_inventory=True)
        prov.delete_confirmed = AsyncMock(return_value={
            "remaining": [], "unidentified": [LISTED[1]], "not_checked": [], "deleted": [],
            "confirmed_deleted": [{"kind": "vm", "moid": "vm-9", "name": "stray"}], "confirm_skipped": []})
        return rid, prov

    def test_only_listed_moids_reach_the_backend_and_the_report_is_redone(self, s5db, monkeypatch):
        rid, prov = self._prepare(s5db, "destroyed")
        monkeypatch.setattr(range_teardown, "_get_backend", lambda *a, **k: prov)
        monkeypatch.setattr(range_teardown, "notify_api", lambda *a: None)
        out = range_teardown.delete_confirmed_teardown_items(rid, ["vm-9", "vm-404"], "admin-1")
        (_, _, items), _ = prov.delete_confirmed.await_args
        assert items == [LISTED[0]]  # vm-404 was never listed: not passed on
        report = _check(s5db, rid, "teardown")["report"]
        assert [(d["moid"], d["by"]) for d in report["confirmed_deleted"]] == [("vm-9", "admin-1")]
        assert {"moid": "vm-404", "why": "not listed in the range's last teardown report"} in report["confirm_skipped"]
        assert report["deleted"] == [{"kind": "vm", "id": "vm-1"}]  # the destroy's own deletions are kept
        assert out["status"] == "incomplete" and out["deleted"] == ["vm-9"]

    def test_nothing_is_deleted_unless_the_range_is_torn_down(self, s5db, monkeypatch):
        rid, prov = self._prepare(s5db, "ready")
        monkeypatch.setattr(range_teardown, "_get_backend", lambda *a, **k: prov)
        out = range_teardown.delete_confirmed_teardown_items(rid, ["vm-9"], "admin-1")
        assert out["status"] == "skipped" and not prov.delete_confirmed.await_count

    def test_moids_not_listed_delete_nothing(self, s5db, monkeypatch):
        rid, prov = self._prepare(s5db, "failed")
        monkeypatch.setattr(range_teardown, "_get_backend", lambda *a, **k: prov)
        out = range_teardown.delete_confirmed_teardown_items(rid, ["VM-9", "vm-9 "], "admin-1")  # not exact
        assert out["status"] == "nothing_listed" and not prov.delete_confirmed.await_count


class TestCertificationAppend:
    def test_appends_follow_the_records_sequence_and_skip_repeats(self, s5db):
        rid = _range(s5db, "ready", _output())
        with _Session(s5db) as conn:
            assert range_inventory_db.append_entry(conn, rid, "drift", "pass", "clean", {"findings": []})
            assert not range_inventory_db.append_entry(conn, rid, "drift", "pass", "clean", {"findings": []})
            assert range_inventory_db.append_entry(conn, rid, "drift", "fail", "vlan", {"findings": [{"k": 1}]})
        assert [(e[0], e[2]) for e in _entries(s5db, rid)] == [(1, "pass"), (2, "fail")]

    def test_a_range_with_no_record_gets_none(self, s5db):
        with _Session(s5db) as conn:
            assert not range_inventory_db.append_entry(conn, str(uuid.uuid4()), "drift", "pass", "x", {})
