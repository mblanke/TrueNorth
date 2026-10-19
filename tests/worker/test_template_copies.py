"""Host-local golden-template copies (ADR 0012 decision 6): naming, pruning rules, and the
prune task's contract (dry run by default, deferred while a range builds, one vCenter's
failure does not stop the others, an unsupported backend is reported as such).

The provisioner's side (which copy a clone uses, and the vCenter deletes) is in
test_vsphere_provision.py, against the same fakes as every other vSphere build test.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from worker.provisioners import template_copies as tc

pytest.importorskip("celery")

from worker import template_tasks  # noqa: E402


class TestNames:
    def test_parse(self):
        got = tc.parse("srv2022@esx03-20261019")
        assert (got.base, got.host, got.date) == ("srv2022", "esx03", "20261019")
        assert tc.parse("srv2022") is None
        assert tc.parse("srv2022@esx03") is None
        assert tc.parse("srv2022@esx03-2026101") is None  # not yyyymmdd
        assert tc.parse("tmpl-ubuntu-2404@esx02-20261001").base == "tmpl-ubuntu-2404"

    def test_host_short_name_is_the_first_label(self):
        assert tc.host_short_name("esx03.truenorth.lab") == "esx03"
        assert tc.host_short_name("ESX04") == "esx04"
        assert tc.host_short_name("10.30.30.12") == "10.30.30.12"
        assert tc.copy_name("srv2022", "esx02.truenorth.lab", "20261019") == "srv2022@esx02-20261019"

    def test_newest_on_host(self):
        names = ["srv@esx02-20261001", "srv@esx02-20261019", "srv@esx03-20261101", "other@esx02-20261231", "srv"]
        assert tc.newest_on_host(names, "srv", "esx02.truenorth.lab") == "srv@esx02-20261019"
        assert tc.newest_on_host(names, "srv", "esx04.truenorth.lab") is None


class TestStale:
    NAMES = ["srv@esx02-20260701", "srv@esx02-20261001", "srv@esx02-20261019",
             "srv@esx03-20261001", "kali@esx02-20260101", "kali@esx02-20261019", "srv", "unrelated-vm"]

    def test_older_than_the_newest_per_template_and_host(self):
        deletable, kept = tc.stale(self.NAMES, set())
        assert deletable == ["kali@esx02-20260101", "srv@esx02-20260701", "srv@esx02-20261001"]
        assert kept == []

    def test_a_copy_a_range_vm_was_cloned_from_is_kept(self):
        deletable, kept = tc.stale(self.NAMES, {"srv@esx02-20261001"})
        assert "srv@esx02-20261001" not in deletable and kept == ["srv@esx02-20261001"]

    def test_the_only_copy_on_a_host_and_plain_names_are_never_stale(self):
        deletable, _ = tc.stale(self.NAMES, set())
        assert "srv@esx03-20261001" not in deletable and "srv" not in deletable

    def test_names_in_use_reads_every_recorded_vm(self):
        outputs = [{"vms": [{"template": "a@esx02-20261001"}, {"name": "legacy"}]}, {}, {"vms": None},
                   {"vms": [{"template": "library:b"}]}]
        assert tc.names_in_use(outputs) == {"a@esx02-20261001", "library:b"}


# -- the task -------------------------------------------------------------------------
class FakeProvisioner:
    def __init__(self, host, *, fail=None, unsupported=False):
        self.host, self.fail, self.calls = host, fail, []
        if unsupported:
            self.prune_template_copies = None

    def connection_record(self):
        return {"id": None, "host": self.host}

    async def prune_template_copies(self, in_use, *, dry_run):
        self.calls.append((set(in_use), dry_run))
        if self.fail:
            raise RuntimeError(self.fail)
        return {"deletable": ["x@esx02-20260101"], "kept_in_use": [], "deleted": [] if dry_run else
                ["x@esx02-20260101"], "errors": [], "dry_run": dry_run}


@pytest.fixture
def world(monkeypatch):
    state = {"rows": [], "connections": [], "provisioners": {}}

    @contextmanager
    def session():
        yield object()

    monkeypatch.setattr(template_tasks, "_db_session", session)
    monkeypatch.setattr(template_tasks.db_ops, "undestroyed_range_outputs", lambda db: state["rows"])
    monkeypatch.setattr(template_tasks.db_ops, "active_connection_creds", lambda db, kind: state["connections"])
    monkeypatch.setattr(template_tasks, "get_provisioner",
                        lambda backend, creds=None, **kw: state["provisioners"][(creds or {}).get("host", "env")])
    monkeypatch.delenv("VSPHERE_URL", raising=False)
    return state


def _vc(world, host, **kw):
    prov = FakeProvisioner(host, **kw)
    world["provisioners"][host] = prov
    world["connections"].append({"host": host, "connection_id": host})
    return prov


class TestPruneTask:
    def test_dry_run_by_default_and_in_use_comes_from_recorded_vms(self, world):
        a = _vc(world, "vc-a")
        world["rows"] = [("r1", "ready", '{"vms": [{"template": "x@esx02-20261001"}]}')]
        out = template_tasks.run_prune()
        assert a.calls == [({"x@esx02-20261001"}, True)]
        assert out["dry_run"] is True and out["endpoints"]["vc-a"]["deleted"] == []

    def test_deleting_is_deferred_while_a_range_is_provisioning(self, world):
        a = _vc(world, "vc-a")
        world["rows"] = [("r1", "provisioning", None), ("r2", "ready", "{}")]
        out = template_tasks.run_prune(dry_run=False)
        assert a.calls == [(set(), True)]
        assert out["dry_run"] is True and out["requested_dry_run"] is False and "deferred" in out

    def test_deletes_when_asked_and_nothing_builds(self, world):
        a = _vc(world, "vc-a")
        world["rows"] = [("r1", "ready", "{}")]
        out = template_tasks.run_prune(dry_run=False)
        assert a.calls == [(set(), False)] and out["endpoints"]["vc-a"]["deleted"] == ["x@esx02-20260101"]
        assert out["status"] == "ok"

    def test_one_vcenter_failing_does_not_stop_the_others(self, world):
        _vc(world, "vc-a", fail="login refused")
        b = _vc(world, "vc-b")
        out = template_tasks.run_prune(dry_run=False)
        assert out["endpoints"]["vc-a"] == {"error": "login refused"}
        assert b.calls == [(set(), False)]
        assert out["status"] == "partial"

    def test_unsupported_backend_is_reported_not_success(self, world):
        _vc(world, "vc-a", unsupported=True)
        assert template_tasks.run_prune()["endpoints"]["vc-a"] == {"unsupported": True}

    def test_one_endpoint_reached_twice_is_pruned_once(self, world, monkeypatch):
        a = _vc(world, "vc-a")
        world["connections"].append({"host": "vc-a", "connection_id": "vc-a"})  # same record
        template_tasks.run_prune()
        assert len(a.calls) == 1

    def test_environment_vcenter_is_included_when_configured(self, world, monkeypatch):
        monkeypatch.setenv("VSPHERE_URL", "https://vcsa.env")
        env = FakeProvisioner("https://vcsa.env")
        world["provisioners"]["env"] = env
        template_tasks.run_prune()
        assert env.calls == [(set(), True)]

    def test_an_unreadable_output_defers_deleting(self, world):
        _vc(world, "vc-a")
        world["rows"] = [("r1", "ready", "{not json")]
        out = template_tasks.run_prune(dry_run=False)
        assert out["unreadable_outputs"] == ["r1"]
        assert out["dry_run"] is True and "unreadable" in out["deferred"]  # it may hold any copy
