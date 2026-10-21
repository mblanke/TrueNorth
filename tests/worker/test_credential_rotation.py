"""Setting rotated range passwords inside running guests (ADR 0013 §3; issue #241).

* ``credential_rotation.plan``: which VMs, which admin login (snapshot-time password first,
  then the one the guest had before the rotation, then the current one), which changes;
  Security Onion nodes and appliances are left to their next build;
* ``vsphere_credcheck.rotate``: the new passwords reach the guest only in an uploaded file
  (owner-only), never on a command line; the files are deleted; each new password is
  verified by a login; changing the login's own password is started detached;
* the task runs after a successful ``restore_snapshot`` (the ``task_success`` hook) and
  records only verified accounts as applied;
* no password in a result, a log line or an error; ``Secret`` never pickles.
"""

from __future__ import annotations

import json
import pickle
from types import SimpleNamespace

import pytest
from worker import credential_rotation as cr
from worker import db_ops
from worker.provisioners import vsphere_credcheck as cc
from worker.provisioners import vsphere_guest as guest
from worker.provisioners.base import GuestLogin

RANGE_ID = "abcdef12-3456-7890-abcd-ef1234567890"
R8 = RANGE_ID[:8]
OLD, NEW, SNAP = "Old-Pass-1!", "New-Pass-2!", "Snap-Pass-3!"


def _row(rid, machine, username, *, role="user", student=True, pending=True, password=NEW, previous=OLD,
         generated=True):
    return {"id": rid, "tenant_id": "t1", "machine": machine, "username": username, "role": role,
            "student_visible": student, "generated": generated, "applied": not pending, "pending": pending,
            "password": password, "sealed": f"sealed-{rid}-{password}", "previous": previous if pending else None}


ROWS = [
    _row("a1", "ws01", "Administrator", role="admin", student=False, pending=False, password="Admin-Pw-4!"),
    _row("a2", "ws01", "student", pending=True),
    _row("b1", "web01", "ubuntu", role="admin", student=False, pending=False, password="Build-Pw-5!"),
    _row("b2", "web01", "analyst", pending=True),
    _row("c1", "so01", "analyst@range.local", pending=True),
    _row("d1", "fw01", "admin", pending=True),
]
VMS = [
    {"vm_id": "vm-1", "name": f"{R8}-ws01", "node_id": "ws01", "family": "windows"},
    {"vm_id": "vm-2", "name": f"{R8}-web01", "node_id": "web01", "family": "linux"},
    {"vm_id": "vm-3", "name": f"{R8}-so01", "node_id": "so01", "family": "linux", "securityonion": {"status": "ok"}},
    {"vm_id": "vm-4", "name": f"{R8}-fw01", "node_id": "fw01", "family": "appliance"},
]


class TestPlan:
    def test_pending_rows_per_vm_with_admin_logins_in_order(self):
        work, notes, undone = cr.plan(RANGE_ID, ROWS, VMS, snapshot={"a1": SNAP})
        by = {w["name"]: w for w in work}
        assert set(by) == {f"{R8}-ws01", f"{R8}-web01"}
        ws = by[f"{R8}-ws01"]
        assert ws["family"] == "windows" and [c.username for c in ws["changes"]] == ["student"]
        # the snapshot-time password first, then the current one (no rotation pending on it)
        assert [(lg.username, lg.password) for lg in ws["logins"]] == [("Administrator", SNAP),
                                                                     ("Administrator", "Admin-Pw-4!")]
        web = by[f"{R8}-web01"]
        assert [lg.username for lg in web["logins"]] == ["ubuntu"] and web["changes"][0].password == NEW
        assert any("so01" in n for n in notes) and any("fw01" in n for n in notes)
        assert {r["id"] for r in undone} == {"c1", "d1"}  # put back: the worker cannot change them

    def test_an_admin_rotation_of_the_login_itself_logs_in_with_its_previous_password(self):
        rows = [_row("a1", "ws01", "Administrator", role="admin", student=False, pending=True,
                     password=NEW, previous=OLD)]
        work, _, _ = cr.plan(RANGE_ID, rows, VMS, ids={"a1"})
        (ws,) = work
        assert [lg.password for lg in ws["logins"]] == [OLD, NEW]  # the guest still has OLD
        assert [(c.username, c.password) for c in ws["changes"]] == [("Administrator", NEW)]

    def test_the_linux_build_user_is_the_preferred_login(self, monkeypatch):
        monkeypatch.setenv("TN_TEMPLATE_LINUX_USER", "tnbuild")
        rows = [_row("x", "web01", "aadmin", role="admin", student=False, pending=False, password="A-1!aaaa"),
                _row("y", "web01", "tnbuild", role="admin", student=False, pending=False, password="B-1!bbbb"),
                _row("z", "web01", "analyst", pending=True)]
        (web,) = [w for w in cr.plan(RANGE_ID, rows, VMS)[0]]
        assert [lg.username for lg in web["logins"]] == ["tnbuild", "aadmin"]

    def test_scenario_and_typed_in_rows_are_never_rotated(self):
        rows = [*ROWS[:3], _row("t", "web01", "jsmith", generated=False)]
        work, _, _ = cr.plan(RANGE_ID, rows, VMS)
        assert all(c.username != "jsmith" for w in work for c in w["changes"])

    def test_only_pending_rows_are_changed(self):
        """An admin rotation names one account; a row with no change pending (already settled,
        or changed again meanwhile) is not touched."""
        work, _, _ = cr.plan(RANGE_ID, ROWS, VMS, ids={"a1", "b2"})
        assert [c.username for w in work for c in w["changes"]] == ["analyst"]

    def test_a_machine_with_no_built_vm_waits_for_its_build(self):
        work, notes, undone = cr.plan(RANGE_ID, [_row("q", "gone01", "x")], VMS)
        assert work == [] and "gone01" in notes[0] and [r["id"] for r in undone] == ["q"]


class FakeSession:
    """GuestSession's surface for rotation: upload, run, start_detached, try_login."""

    def __init__(self, creds, guest_passwords, *, run_status="ok"):
        self.creds, self.passwords = creds, guest_passwords
        self.uploads, self.ran, self.detached = [], [], []
        self.run_status = run_status
        self.t, self._poll = 0.0, 1.0
        self._clock = lambda: self.t
        self._sleep = lambda s: setattr(self, "t", self.t + s)

    def upload(self, path, data, deadline, *, overwrite=True, posix_mode=None):
        self.uploads.append((path, data.decode(), posix_mode))

    def run(self, cmd, deadline):
        self.ran.append(cmd)
        if cmd.label == "rotate" and self.run_status == "ok":
            self._apply()
        return self.run_status, 0 if self.run_status == "ok" else 1

    def start_detached(self, program, arguments):
        self.detached.append(arguments)
        self._apply()
        return 7

    def _apply(self):
        for path, text, _ in self.uploads:
            if path.endswith(".s"):
                continue
            for line in text.splitlines():
                user, _, pw = line.partition("\t" if "\t" in line else ":")
                self.passwords[user] = pw

    def try_login(self):
        return self.passwords.get(self.creds.username) == self.creds.password.reveal()


def _rotate(family, login_user, login_pw, changes, *, run_status="ok"):
    passwords = {login_user: login_pw}
    made = []

    def session_for(creds):
        s = FakeSession(creds, passwords, run_status=run_status)
        made.append(s)
        return s

    login = guest.GuestCredentials(family, login_user, guest.Secret(login_pw))
    session = session_for(login)
    accts = [guest.RangeAccount(u, guest.Secret(p)) for u, p in changes]
    verified, error = cc.rotate(session, family, accts, 100.0, session_for)
    return session, verified, error, passwords


class TestRotateInGuest:
    def test_linux_through_owner_only_files_never_the_command_line(self):
        s, verified, error, pws = _rotate("linux", "ubuntu", "Build-Pw-5!", [("analyst", NEW)])
        assert (verified, error) == (["analyst"], "") and pws["analyst"] == NEW
        assert [(p.startswith("/tmp/tn-rotate-"), m) for p, _, m in s.uploads] == [(True, 0o600), (True, 0o600)]
        assert s.uploads[0][1] == f"analyst:{NEW}\n" and s.uploads[1][1] == "Build-Pw-5!\n"  # sudo -S input
        (cmd,) = s.ran
        assert NEW not in cmd.arguments and "Build-Pw-5!" not in cmd.arguments
        assert "chpasswd <" in cmd.arguments and "rm -f" in cmd.arguments and "sudo -S" in cmd.arguments

    def test_windows_through_adsi_with_the_file_deleted_in_the_same_command(self):
        import base64

        s, verified, error, _ = _rotate("windows", "Administrator", "Admin-Pw-4!", [("student", NEW)])
        assert verified == ["student"] and error == ""
        assert s.uploads[0][0].startswith("C:\\Windows\\Temp\\tn-rotate-") and s.uploads[0][1] == f"student\t{NEW}\r\n"
        script = base64.b64decode(s.ran[0].arguments.split()[-1]).decode("utf-16-le")
        assert "SetPassword" in script and "Remove-Item" in script and NEW not in s.ran[0].arguments

    def test_the_logins_own_password_is_changed_detached_then_verified(self):
        s, verified, error, pws = _rotate("windows", "Administrator", OLD, [("Administrator", NEW)])
        assert s.ran == [] and len(s.detached) == 1 and NEW not in s.detached[0]
        assert verified == ["Administrator"] and pws["Administrator"] == NEW

    def test_a_failed_change_is_not_verified_and_says_so_without_the_password(self):
        _, verified, error, _ = _rotate("linux", "ubuntu", "Build-Pw-5!", [("analyst", NEW)], run_status="failed")
        assert verified == [] and "ended failed" in error and NEW not in error

    def test_secrets_never_pickle(self):
        with pytest.raises(TypeError):
            pickle.dumps(guest.Secret(NEW))
        assert NEW not in repr(GuestLogin("a", NEW))


class TestRun:
    @pytest.fixture
    def env(self, monkeypatch):
        import worker.base_tasks as base_tasks
        import worker.tasks as tasks

        state = {"settled": [], "audits": [], "asked": None, "fail": set(), "raise": None, "stale": set()}

        class DB:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def commit(self):
                pass

        class Prov:
            supports_credential_rotation = True

            async def rotate_guest_passwords(self, range_id, vms, timeout):
                state["asked"] = vms
                if state["raise"]:
                    raise state["raise"]
                return {v["name"]: {"applied": [c.username for c in v["changes"] if c.username not in state["fail"]],
                                    "error": "x" if state["fail"] else ""} for v in vms}

        def settle(db, rid, cid, sealed, *, applied):
            if cid in state["stale"]:
                return False  # changed again meanwhile: the compare-and-set finds another version
            state["settled"].append((cid, sealed, applied))
            return True

        monkeypatch.setattr(tasks, "_db_session", lambda: DB())
        monkeypatch.setattr(base_tasks, "_get_backend", lambda *a, **k: Prov())
        monkeypatch.setattr(cr, "run_async", lambda coro: __import__("asyncio").run(coro))
        monkeypatch.setattr(db_ops, "account_rows", lambda db, rid, ids=None: ROWS)
        monkeypatch.setattr(db_ops, "snapshot_passwords", lambda db, rid, sid: {"a1": SNAP})
        monkeypatch.setattr(db_ops, "range_output_and_backend", lambda db, rid: (json.dumps({"vms": VMS}), "vsphere"))
        monkeypatch.setattr(db_ops, "settle_credential_change", settle)
        monkeypatch.setattr(db_ops, "audit_credential_change", lambda db, rid, tid, d: state["audits"].append(d))
        return state

    def test_after_a_restore_verified_changes_are_applied_and_the_rest_put_back(self, env, caplog):
        caplog.set_level("DEBUG")
        out = cr.run(RANGE_ID, snapshot_id="snap-1", operation_id="restore:snap-1")
        assert out["status"] == "ok" and out["applied"] == 2
        outcome = {cid: applied for cid, _, applied in env["settled"]}
        # a2/b2 verified in the guest; c1 (Security Onion) and d1 (appliance) cannot be changed here
        assert outcome == {"a2": True, "b2": True, "c1": False, "d1": False}
        assert ("b2", "sealed-b2-" + NEW, True) in env["settled"]  # settled on the version it set
        assert {a["outcome"] for a in env["audits"]} == {"applied", "reverted"}
        dumped = json.dumps([out, env["audits"]])
        for secret in (NEW, SNAP, OLD):
            assert secret not in dumped and secret not in caplog.text

    def test_a_change_the_guest_did_not_take_is_put_back(self, env):
        env["fail"] = {"analyst"}
        out = cr.run(RANGE_ID, snapshot_id="snap-1")
        assert out["status"] == "partial" and out["reverted"] == 1
        assert ("b2", "sealed-b2-" + NEW, False) in env["settled"] and ("a2", "sealed-a2-" + NEW, True) in env["settled"]

    @pytest.mark.parametrize("failure", ["unsupported", "error", "timeout"])
    def test_a_backend_that_cannot_or_does_not_finish_puts_everything_back(self, env, monkeypatch, failure):
        import worker.base_tasks as base_tasks
        from celery.exceptions import SoftTimeLimitExceeded

        if failure == "unsupported":
            monkeypatch.setattr(base_tasks, "_get_backend", lambda *a, **k: SimpleNamespace())
        else:
            env["raise"] = ConnectionError("vCenter down") if failure == "error" else SoftTimeLimitExceeded()
        if failure == "timeout":
            with pytest.raises(SoftTimeLimitExceeded):
                cr.run(RANGE_ID)
        else:
            assert cr.run(RANGE_ID)["status"] == "reverted"
        assert {cid for cid, _, applied in env["settled"] if not applied} == {"a2", "b2", "c1", "d1"}
        assert not [s for s in env["settled"] if s[2]]

    def test_a_row_changed_again_meanwhile_is_left_to_that_change(self, env):
        env["stale"] = {"b2"}
        cr.run(RANGE_ID)
        assert "b2" not in {cid for cid, _, _ in env["settled"]}

    def test_the_restore_hook_sends_the_rotation_only_after_a_restore_succeeded(self, no_real_broker):
        # The shared broker fixture: patching send_task on the app instance would outlive the test.
        restore = SimpleNamespace(name=cr.RESTORE_TASK, request=SimpleNamespace(args=[RANGE_ID, "snap-1"]))
        cr._after_restore(sender=restore, result={"status": "skipped"})
        cr._after_restore(sender=SimpleNamespace(name="worker.tasks.snapshot_range", request=restore.request),
                          result={"status": "restored"})
        assert [s for s in no_real_broker.sent if s[0] == cr.TASK] == []
        cr._after_restore(sender=restore, result={"status": "restored", "range_id": RANGE_ID})
        assert [s for s in no_real_broker.sent if s[0] == cr.TASK] == [
            (cr.TASK, [RANGE_ID, [], "restore:snap-1", "snap-1"])]


class TestEnforceNeedsTemplatePasswords:
    """Adversarial review of #250, item 5: under RANGE_TEMPLATE_LOGIN_CHECK=enforce a vSphere
    worker that does not know the template passwords stops at start, loudly."""

    BASE = {"PROVISIONER_BACKEND": "vsphere_api", "TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD": "W-1!aaaa",
            "TN_TEMPLATE_LINUX_PASSWORD": "L-1!aaaa"}

    @pytest.mark.parametrize(("env", "problem"), [
        ({}, False),
        ({"TN_TEMPLATE_LINUX_PASSWORD": ""}, True),
        ({"TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD": " "}, True),
        ({"TN_TEMPLATE_LINUX_PASSWORD": "", "RANGE_TEMPLATE_LOGIN_CHECK": "warn"}, False),
        ({"TN_TEMPLATE_LINUX_PASSWORD": "", "PROVISIONER_BACKEND": "mock"}, False),
    ])
    def test_startup_problem(self, env, problem):
        result = cc.startup_problem({**self.BASE, **env})
        assert (result is not None) == problem
        if result:
            assert "TN_TEMPLATE" in result and "W-1!aaaa" not in result

    def test_the_worker_refuses_to_start(self, monkeypatch, caplog):
        for k, v in {**self.BASE, "TN_TEMPLATE_LINUX_PASSWORD": ""}.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("RANGE_TEMPLATE_LOGIN_CHECK", raising=False)
        with pytest.raises(SystemExit):
            cr._refuse_to_start_without_template_passwords()
        assert "RANGE_TEMPLATE_LOGIN_CHECK=enforce" in caplog.text
        monkeypatch.setenv("TN_TEMPLATE_LINUX_PASSWORD", "L-1!aaaa")
        cr._refuse_to_start_without_template_passwords()  # configured: starts

    def test_an_unknown_template_password_is_said_under_enforce_and_quiet_under_warn(self):
        result = {"status": cc.INCONCLUSIVE, "username": "ubuntu", "detail": cc.NOT_CONFIGURED}
        warnings: list[str] = []
        assert cc.report("vm", result, warnings, env={"RANGE_TEMPLATE_LOGIN_CHECK": "enforce"}) == ""
        assert warnings and "inconclusive" in warnings[0]
        quiet: list[str] = []
        cc.report("vm", result, quiet, env={"RANGE_TEMPLATE_LOGIN_CHECK": "warn"})
        assert quiet == []
