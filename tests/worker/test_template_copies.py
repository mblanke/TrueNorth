"""Host-local golden-template copies (ADR 0012 decision 6, ADR 0013 §2): naming, build
stamps, pruning rules (from the build records: never a promoted build's copy or one a range
records), and the prune task's contract (dry run by default, deferred while a range builds,
one vCenter's failure does not stop the others, an unsupported backend is reported as such).

The provisioner's side (which copy a clone uses, and the vCenter deletes) is in
test_vsphere_provision.py, against the same fakes as every other vSphere build test.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

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

    def test_newest_copy_selection_is_gone_for_builds(self):
        """ADR 0013 §2: no newest-copy selection and no date-ordered staleness for a build;
        ``newest_local`` exists only for the transition flag's unpromoted VMs."""
        assert not hasattr(tc, "newest_on_host") and not hasattr(tc, "stale")
        names = ["srv@esx02-20261001", "srv@esx02-20261019", "srv@esx03-20261101", "srv"]
        assert tc.newest_local(names, "srv", "esx02.truenorth.lab") == "srv@esx02-20261019"
        assert tc.newest_local(names, "srv", "esx04") is None


SUM = "a" * 64


class TestStamps:
    def test_round_trip(self):
        note = "TrueNorth golden: Ubuntu 24.04\n" + tc.stamp("ubuntu-lts", "ubuntu-lts-20261019T1200", SUM)
        assert tc.stamp_of(note) == ("ubuntu-lts", "ubuntu-lts-20261019T1200", SUM)
        assert tc.stamp_problem(note, "ubuntu-lts", "ubuntu-lts-20261019T1200", SUM) is None

    def test_missing_mismatched_and_ambiguous_stamps_are_problems(self):
        assert "no single build stamp" in tc.stamp_problem("TrueNorth golden", "srv", "b1", SUM)
        assert "no single build stamp" in tc.stamp_problem(None, "srv", "b1", SUM)
        assert "not srv build b1" in tc.stamp_problem(tc.stamp("srv", "b0", SUM), "srv", "b1", SUM)
        assert "not srv build b1" in tc.stamp_problem(tc.stamp("srv", "b1", "b" * 64), "srv", "b1", SUM)
        two = tc.stamp("srv", "b1", SUM) + "\n" + tc.stamp("srv", "b2", "b" * 64)
        assert tc.stamp_of(two) is None  # cannot be shown to be one build

    def test_another_images_build_with_the_same_id_and_checksum_is_refused(self):
        assert "is stamped kali build b1" in tc.stamp_problem(tc.stamp("kali", "b1", SUM), "srv", "b1", SUM)


class TestPrunable:
    NOTES = {"srv@esx02-20260701": "", "srv@esx02-20261001": tc.stamp("srv", "b1", SUM), "srv@esx02-20261019": "",
             "srv@esx04-20260101": tc.stamp("srv", "b1", SUM), "kali@esx02-20260101": "", "srv": "",
             "unrelated-vm": ""}
    POLICY = tc.PrunePolicy(protected=frozenset({"srv@esx02-20261001"}), managed=frozenset({"srv"}),
                            builds=frozenset({("srv", "b1", SUM)}))

    def test_only_copies_of_a_managed_template_not_protected(self):
        deletable, kept = tc.prunable(self.NOTES, self.POLICY)
        # The newest unrecorded copy goes as well as the oldest: no date ordering.
        assert deletable == ["srv@esx02-20260701", "srv@esx02-20261019"]
        assert kept == ["srv@esx02-20261001", "srv@esx04-20260101"]  # by name; by stamp

    def test_nothing_without_a_policy(self):
        assert tc.prunable(self.NOTES, tc.PrunePolicy()) == ([], [])

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

    async def prune_template_copies(self, policy, *, dry_run):
        self.calls.append((set(policy.protected), dry_run))
        if self.fail:
            raise RuntimeError(self.fail)
        return {"deletable": ["x@esx02-20260101"], "kept_in_use": [], "deleted": [] if dry_run else
                ["x@esx02-20260101"], "errors": [], "dry_run": dry_run}


@pytest.fixture
def world(monkeypatch):
    state = {"rows": [], "builds": [], "connections": [], "provisioners": {}}

    @contextmanager
    def session():
        yield object()

    monkeypatch.setattr(template_tasks, "_db_session", session)
    monkeypatch.setattr(template_tasks.db_ops, "undestroyed_range_outputs", lambda db: state["rows"])
    monkeypatch.setattr(template_tasks.db_ops, "golden_image_build_copies", lambda db: state["builds"])
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


# -- what pruning keeps, from the build records (ADR 0013 §2) ---------------------------
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _row(image, build_id, copies, *, promoted=False, at=T0, checksum=SUM, template="srv2022"):
    return {"image_id": image, "catalogue_id": template, "image_template": template, "build_id": build_id,
            "checksum": checksum, "build_template": template, "promoted": promoted, "created_at": at,
            "copies": json.dumps(copies)}


class TestPrunePolicy:
    def test_promoted_and_newer_builds_are_protected_older_ones_are_not(self):
        rows = [
            _row("i1", "old", {"esx02": "srv2022@esx02-20260901"}, at=T0 - timedelta(days=30)),
            _row("i1", "cur", {"esx02": "srv2022@esx02-20261001"}, promoted=True, at=T0),
            _row("i1", "next", {"esx02": "srv2022@esx02-20261019"}, at=T0 + timedelta(days=18), checksum="b" * 64),
        ]
        policy = template_tasks.prune_policy(rows, {"srv2022@esx03-20260801"})
        assert policy.managed == {"srv2022"}
        assert policy.protected == {"srv2022@esx02-20261001", "srv2022@esx02-20261019", "srv2022@esx03-20260801"}
        assert policy.builds == {("srv2022", "cur", SUM), ("srv2022", "next", "b" * 64)}
        notes = {"srv2022@esx02-20260901": "", "srv2022@esx02-20261001": "", "srv2022@esx02-20261019": "",
                 "srv2022@esx03-20260801": ""}
        assert tc.prunable(notes, policy) == (["srv2022@esx02-20260901"],
                                             ["srv2022@esx02-20261001", "srv2022@esx02-20261019",
                                              "srv2022@esx03-20260801"])

    def test_an_image_without_a_promoted_build_is_not_managed(self):
        rows = [_row("i2", "b1", {"esx02": "kali@esx02-20261001"}, template="kali")]
        policy = template_tasks.prune_policy(rows, set())
        assert policy.managed == frozenset() and policy.builds == frozenset()
        assert tc.prunable({"kali@esx02-20260101": "", "kali@esx02-20261001": ""}, policy) == ([], [])

    def test_the_task_passes_the_policy_built_from_the_records(self, world):
        a = _vc(world, "vc-a")
        world["builds"] = [_row("i1", "cur", {"esx02": "srv2022@esx02-20261001"}, promoted=True)]
        world["rows"] = [("r1", "ready", '{"vms": [{"template": "srv2022@esx03-20260801"}]}')]
        out = template_tasks.run_prune(dry_run=False)
        assert a.calls == [({"srv2022@esx02-20261001", "srv2022@esx03-20260801"}, False)]
        assert out["managed"] == ["srv2022"]
