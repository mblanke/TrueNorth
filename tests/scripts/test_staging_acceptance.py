"""scripts/staging_acceptance.py: the report, the runner's PASS/FAIL/SKIP rules, fixtures,
cleanup, personas, the command line, and the multi-user harness in scripts/_inprocess_api.py
(fakes only; the checks themselves run against a stack, see docs/runbooks/staging-acceptance.md)."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("staging_acceptance", ROOT / "scripts" / "staging_acceptance.py")
acc = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("staging_acceptance", acc)  # dataclasses look their module up
_spec.loader.exec_module(acc)
import _inprocess_api  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


def ctx(**kw) -> acc.Ctx:
    clock = FakeClock()
    kw.setdefault("tag", "t1")
    return acc.Ctx(
        kw.pop("admin", None), kw.pop("users", None), sleep=clock.sleep, clock=clock, log=lambda _: None, **kw
    )


# ── modules and checks ──────────────────────────────────────────────────


def test_seventeen_modules_each_with_checks():
    assert len(acc.MODULES) == 17 and len(set(acc.MODULES)) == 17
    assert set(acc.CHECKS) == set(acc.MODULES)
    assert all(acc.CHECKS[m] for m in acc.MODULES), [m for m in acc.MODULES if not acc.CHECKS[m]]
    names = [(m, n) for m in acc.MODULES for n, _ in acc.CHECKS[m]]
    assert len(names) == len(set(names))  # no check registered twice


def test_detection_runs_after_the_exercise_starts_and_before_the_aar_completes_it():
    order = list(acc.MODULES)
    assert order.index("scenario-engine") < order.index("detection") < order.index("reporting-aar")


def test_select_modules_keeps_run_order_and_refuses_unknown_names():
    assert acc.select_modules(None) == list(acc.MODULES)
    assert acc.select_modules("greyspace, infra") == ["infra", "greyspace"]
    with pytest.raises(ValueError, match="unknown module"):
        acc.select_modules("infra,bogus")


def test_check_decorator_refuses_an_unknown_module():
    with pytest.raises(ValueError):
        acc.check("nope", "x")


# ── report ──────────────────────────────────────────────────────────────


def test_report_counts_exit_code_and_table():
    r = acc.Report()
    r.add("infra", "a", acc.PASS, "fine")
    r.add("infra", "b", acc.SKIP, "needs real vSphere")
    assert r.exit_code() == 0
    r.add("lms", "c", acc.FAIL, "quiz not published")
    assert r.exit_code() == 1
    assert r.counts() == {"PASS": 1, "FAIL": 1, "SKIP": 1}
    table = r.table()
    assert table.splitlines()[0].split(" | ") == ["module", "check", "status", "detail"]
    assert "lms    | c     | FAIL   | quiz not published" in table
    assert table.endswith("1 passed, 1 failed, 1 skipped")


def test_a_skip_without_a_reason_is_a_failure():
    r = acc.Report()
    res = r.add("infra", "x", acc.SKIP, "   ")
    assert (res.status, res.detail) == (acc.FAIL, "skipped without a stated reason")
    assert r.exit_code() == 1


def test_strict_turns_undeclared_skips_into_failures():
    r = acc.Report(strict=True)
    declared = r.add("provisioners", "vsphere", acc.SKIP, "vsphere declared absent", declared=True)
    found = r.add("lms", "moodle", acc.SKIP, "no Moodle platform registered")
    assert (declared.status, declared.declared) == (acc.SKIP, True)
    assert (found.status, found.detail) == (acc.FAIL, "unexpected skip (--strict): no Moodle platform registered")
    assert r.exit_code() == 1
    assert acc.Report().add("lms", "moodle", acc.SKIP, "no Moodle platform registered").status == acc.SKIP


def test_runner_passes_the_declared_flag_and_absent_facilities_declare():
    def absent(c):
        c.require("vsphere")

    def by_design(_):
        acc.skip("needs a writable vCenter", True)

    def found_missing(_):
        acc.skip("no accepted release")

    checks = {"m": [("a", absent), ("b", by_design), ("c", found_missing)]}
    report = acc.run_checks(ctx(absent={"vsphere"}), ["m"], acc.Report(strict=True), checks, log=lambda _: None)
    assert [(r.status, r.declared) for r in report.results] == [("SKIP", True), ("SKIP", True), ("FAIL", False)]


def test_the_by_design_skips_are_declared():
    for module, name in (
        ("ranges-labs", "lab build on the hypervisor"),
        ("noise-engine", "deploy to agents"),
        ("provisioners", "Security Onion per-range setup (so-setup)"),
    ):
        fn = dict(acc.CHECKS[module])[name]
        with pytest.raises(acc.SkippedCheckError) as err:
            fn(ctx())
        assert err.value.declared is True


def test_dependents_of_a_declared_skip_are_declared_too():
    c = ctx(absent={"lrs"})
    with pytest.raises(acc.SkippedCheckError):
        c.memo("the release", lambda: c.require("lrs"))
    with pytest.raises(acc.SkippedCheckError) as err:
        c.memo("the release", lambda: None)
    assert err.value.declared is True


# ── permitted skips (docs/release-notes/v1.2.0-permitted-skips.json) ────


def test_the_shipped_inventory_is_exactly_the_registered_checks():
    _, inventory = acc.load_permitted(acc.PERMITTED_SKIPS, "staging")
    assert list(inventory) == list(acc.MODULES)
    assert acc.inventory_drift(acc.CHECKS, inventory, acc.MODULES) == []


def test_staging_permits_only_the_hardware_skips():
    """The two of the rc4 freeze, and Security Onion's so-setup (2026-10-20 amendment, #192)."""
    entries, _ = acc.load_permitted(acc.PERMITTED_SKIPS, "staging")
    assert {(e["module"], e["check"], e["milestone"]) for e in entries} == {
        ("ranges-labs", "lab build on the hypervisor", "H"),
        ("noise-engine", "deploy to agents", "H"),
        ("provisioners", "Security Onion per-range setup (so-setup)", "H"),
    }


@pytest.mark.parametrize("profile", ["staging", "ci-integration"])
def test_each_permitted_skip_matches_the_reason_its_check_gives(profile):
    """Run each permitted check where it skips (the CI stack's absent facilities); the reason
    it gives must carry the entry's reason_contains, or the entry can never apply."""
    entries, _ = acc.load_permitted(acc.PERMITTED_SKIPS, profile)
    absent = {"edge", "keycloak", "minio", "lrs", "moodle", "backup", "vsphere", "metrics-token", "arc2"}
    for e in entries:
        with pytest.raises(acc.SkippedCheckError) as err:
            # A database with no audit export and no audit rows: the CI stack's state.
            dict(acc.CHECKS[e["module"]])[e["check"]](ctx(absent=absent, options={"sql": lambda q, p: []}))
        assert e["reason_contains"] in str(err.value), (e["module"], e["check"], str(err.value))


def test_strict_with_a_profile_fails_every_skip_it_does_not_list():
    permitted = [{"module": "ranges-labs", "check": "lab build", "reason_contains": "writable, real vCenter"}]
    r = acc.Report(strict=True, permitted=permitted)
    listed = r.add("ranges-labs", "lab build", acc.SKIP, "it needs a writable, real vCenter", declared=True)
    other_reason = r.add("ranges-labs", "lab build", acc.SKIP, "no connection registered", declared=True)
    declared_unlisted = r.add("lms", "moodle", acc.SKIP, "moodle declared absent", declared=True)
    assert listed.status == acc.SKIP
    assert (other_reason.status, declared_unlisted.status) == (acc.FAIL, acc.FAIL)
    assert declared_unlisted.detail == "skip not in the permitted list (--strict): moodle declared absent"
    assert r.exit_code() == 1


def test_inventory_drift_names_missing_and_unrecorded_checks():
    checks = {"m": [("a", None), ("new", None)]}
    assert acc.inventory_drift(checks, {"m": ["a", "gone"]}, ["m"]) == [
        ("m", "gone", "in the release's check inventory but not registered: a missing check"),
        ("m", "new", "registered but not in the release's check inventory"),
    ]


def test_a_malformed_permitted_skips_file_is_refused(tmp_path):
    bad = tmp_path / "skips.json"
    bad.write_text(json.dumps({"profiles": {"staging": [{"module": "infra", "check": "nope"}]}}))
    with pytest.raises(ValueError, match="lacks reason_contains"):
        acc.load_permitted(bad, "staging")
    with pytest.raises(ValueError, match="no skip profile 'prod'"):
        acc.load_permitted(bad, "prod")
    entry = dict.fromkeys(acc.SKIP_ENTRY_KEYS, "x") | {"module": "infra", "check": "nope"}
    bad.write_text(json.dumps({"profiles": {"staging": [entry]}}))
    with pytest.raises(ValueError, match="which is not a check"):
        acc.load_permitted(bad, "staging")


def test_strict_refuses_to_start_without_a_usable_profile(tmp_path, capsys):
    assert acc.main(["--admin", "a@b", "--strict", "--skip-profile", "prod"]) == 2
    assert "no skip profile 'prod'" in capsys.readouterr().err
    assert acc.main(["--admin", "a@b", "--strict", "--permitted-skips", str(tmp_path / "missing.json")]) == 2


# ── AI disabled (tn_ai_enabled: false; profile ai-disabled) ─────────────


def test_profiles_combine_and_an_unknown_one_in_the_list_is_refused():
    staging, _ = acc.load_permitted(acc.PERMITTED_SKIPS, "staging")
    both, _ = acc.load_permitted(acc.PERMITTED_SKIPS, "staging, ai-disabled")
    assert both[: len(staging)] == staging
    assert {(e["module"], e["check"]) for e in both[len(staging):]} == {
        ("ai-arc2", "AI orchestrator answers through the API"),
        ("ai-arc2", "ARC² Course Studio enabled"),
    }
    with pytest.raises(ValueError, match="no skip profile 'nope'"):
        acc.load_permitted(acc.PERMITTED_SKIPS, "staging,nope")


class FakeAiAdmin:
    """The admin API as the AI check sees it: deep health and the detection draft."""

    def __init__(self, configured, draft):
        self.configured, self.draft = configured, draft

    def get(self, path, **kw):
        assert path == "/health/deep"
        details = {} if self.configured is None else {"ai_configured": self.configured}
        return 200, {"components": [{"name": "ai_orchestrator", "status": "healthy", "details": details}]}

    def post(self, path, json=None, **kw):
        assert path == "/ai/detection-draft"
        return self.draft


NOT_CONFIGURED = (503, {"detail": "AI is not configured on this host. An administrator must set the AI endpoint"})
DRAFTED = (200, {"output": "title: Suspicious PowerShell\ndetection: {}"})
AI_CHECK = ("ai-arc2", "AI orchestrator answers through the API")


def _ai_check(monkeypatch, enabled, admin, profile=None):
    monkeypatch.setenv("TN_AI_ENABLED", enabled)
    permitted = acc.load_permitted(acc.PERMITTED_SKIPS, profile)[0] if profile else None
    report = acc.Report(strict=profile is not None, permitted=permitted)
    checks = {"ai-arc2": [(AI_CHECK[1], dict(acc.CHECKS["ai-arc2"])[AI_CHECK[1]])]}
    acc.run_checks(ctx(admin=admin), ["ai-arc2"], report, checks=checks, log=lambda _: None)
    [r] = report.results
    return r


def test_ai_disabled_and_not_configured_is_a_declared_skip_permitted_only_with_ai_disabled(monkeypatch):
    admin = FakeAiAdmin(False, NOT_CONFIGURED)
    r = _ai_check(monkeypatch, "false", admin, "staging,ai-disabled")
    assert (r.status, r.declared) == (acc.SKIP, True)
    assert r.detail.startswith("AI disabled on this host (tn_ai_enabled: false;")
    assert _ai_check(monkeypatch, "false", admin, "staging").status == acc.FAIL  # never silently


def test_ai_disabled_but_the_orchestrator_is_configured_fails(monkeypatch):
    for configured in (True, None):
        r = _ai_check(monkeypatch, "false", FakeAiAdmin(configured, DRAFTED), "staging,ai-disabled")
        assert r.status == acc.FAIL and "ai_configured=" in r.detail


def test_ai_disabled_without_the_clear_503_fails(monkeypatch):
    for draft in (DRAFTED, (502, {"detail": "AI orchestrator failed to generate a draft."})):
        r = _ai_check(monkeypatch, "false", FakeAiAdmin(False, draft), "staging,ai-disabled")
        assert r.status == acc.FAIL and "must answer 503" in r.detail


def test_ai_enabled_must_draft(monkeypatch):
    assert _ai_check(monkeypatch, "true", FakeAiAdmin(True, DRAFTED)).status == acc.PASS
    r = _ai_check(monkeypatch, "true", FakeAiAdmin(False, NOT_CONFIGURED), "staging,ai-disabled")
    assert r.status == acc.FAIL and "503" in r.detail  # the flag says on: no skip for "not configured"


def test_report_refuses_unknown_status_and_flattens_detail():
    r = acc.Report()
    with pytest.raises(ValueError):
        r.add("infra", "x", "OK")
    assert r.add("infra", "y", acc.PASS, "two\n  lines").detail == "two lines"


def test_report_json(tmp_path):
    r = acc.Report()
    r.add("infra", "a", acc.PASS, "fine", 1.234)
    r.add("lms", "b", acc.FAIL, "broken")
    out = tmp_path / "sub" / "report.json"
    r.write_json(out, {"tag": "t1"})
    data = json.loads(out.read_text())
    assert data["meta"] == {"tag": "t1"} and data["passed"] is False
    assert data["counts"] == {"PASS": 1, "FAIL": 1, "SKIP": 0}
    assert data["results"][0] == {
        "module": "infra",
        "check": "a",
        "status": "PASS",
        "detail": "fine",
        "seconds": 1.23,
        "declared": False,
    }


def test_long_details_are_cut_in_the_table_but_kept_in_json():
    r = acc.Report()
    r.add("infra", "a", acc.FAIL, "x" * 500)
    assert len(r.table(width=100).splitlines()[2]) <= 100
    assert len(r.to_dict()["results"][0]["detail"]) == 500


# ── runner ──────────────────────────────────────────────────────────────


def test_runner_maps_outcomes():
    def passes(_):
        return "did it"

    def fails(_):
        acc.expect(False, "wrong answer")

    def skips(_):
        acc.skip("needs real vSphere")

    def crashes(_):
        raise KeyError("id")

    checks = {"m": [("p", passes), ("f", fails), ("s", skips), ("c", crashes)]}
    lines: list[str] = []
    report = acc.run_checks(ctx(), ["m"], acc.Report(), checks, log=lines.append)
    got = [(r.check, r.status, r.detail) for r in report.results]
    assert got == [
        ("p", "PASS", "did it"),
        ("f", "FAIL", "wrong answer"),
        ("s", "SKIP", "needs real vSphere"),
        ("c", "FAIL", "KeyError: 'id'"),
    ]
    assert len(lines) == 4 and report.exit_code() == 1


def test_ok_returns_the_body_or_fails_with_status_and_body():
    assert acc.ok((201, {"id": 1}), 201, what="POST") == {"id": 1}
    with pytest.raises(acc.FailedCheckError, match=r"POST /x => 409 \{\"detail\": \"exists\"\}"):
        acc.ok((409, {"detail": "exists"}), 201, what="POST /x")
    with pytest.raises(acc.FailedCheckError):
        acc.ok((201, None), what="default is 200 only")


def test_items_and_longest_text():
    assert acc.items([1, 2]) == [1, 2]
    assert acc.items({"items": [1], "total": 1}) == [1]
    assert acc.items({"hits": {"hits": [{"_id": "a"}]}}) == [{"_id": "a"}]
    assert acc.items("nope") == []
    assert acc.longest_text({"a": "short", "b": [{"c": "the longest text here"}], "n": 3}) == "the longest text here"
    assert acc.longest_text(None) == ""


def comp(name, status, message=""):
    return {"name": name, "status": status, "message": message}


def test_health_judgement():
    ok_ = [comp(n, "healthy") for n in acc.DEEP_COMPONENTS]
    assert acc.judge_components(ok_, set(), acc.DEEP_COMPONENTS) == ([], [])
    yellow = [*ok_[:2], comp("opensearch", "degraded", "Cluster status: yellow"), *ok_[3:]]
    assert acc.judge_components(yellow, set(), acc.DEEP_COMPONENTS) == ([], ["opensearch yellow (single node)"])
    # Degraded for any other reason, or another component degraded, is a problem.
    ai = [*ok_[:5], comp("ai_orchestrator", "degraded", "HTTP 502"), *ok_[6:]]
    assert acc.judge_components(ai, set(), acc.DEEP_COMPONENTS)[0] == ["ai_orchestrator degraded: HTTP 502"]
    # A component of a facility declared absent is noted, not judged; a required one missing fails.
    minio_down = [*ok_[:3], comp("minio", "unhealthy", "refused"), *ok_[4:]]
    assert acc.judge_components(minio_down, {"minio"}, acc.DEEP_COMPONENTS) == ([], ["minio not judged (absent)"])
    assert acc.judge_components(minio_down, set(), acc.DEEP_COMPONENTS)[0] == ["minio unhealthy: refused"]
    assert acc.judge_components(ok_[1:], set(), ("database",))[0] == ["database not reported"]


# ── context ─────────────────────────────────────────────────────────────


def test_a_failed_fixture_fails_its_first_user_and_skips_the_rest():
    c = ctx()
    calls = []

    def make():
        calls.append(1)
        raise acc.FailedCheckError("POST /ranges => 500")

    with pytest.raises(acc.FailedCheckError):
        c.memo("the range", make)
    with pytest.raises(acc.SkippedCheckError, match="needs the range, which could not be set up: POST /ranges => 500"):
        c.memo("the range", make)
    assert calls == [1]
    assert c.memo("ok", lambda: 7) == 7 and c.memo("ok", lambda: 8) == 7


def test_poll_waits_then_times_out():
    c = ctx()
    seq = iter([None, [], {"id": 1}])
    assert c.poll(lambda: next(seq), "it", timeout=10, interval=2) == {"id": 1}
    with pytest.raises(acc.FailedCheckError, match="timed out after 5s waiting for nothing"):
        c.poll(lambda: None, "nothing", timeout=5, interval=2)


def test_cleanup_runs_in_reverse_reports_leftovers_and_never_raises():
    c = ctx()
    done: list[str] = []
    c.on_cleanup("first", lambda: done.append("first") or "")
    c.on_cleanup("second", lambda: done.append("second") or "409 still referenced")

    def boom():
        raise RuntimeError("gone")

    c.on_cleanup("third", boom)
    assert c.cleanup() == ["third: RuntimeError: gone", "second: 409 still referenced"]
    assert done == ["second", "first"] and c.cleanups == []


def test_keep_leaves_everything():
    c = ctx(keep=True)
    c.on_cleanup("range r", lambda: pytest.fail("cleanup ran with --keep"))
    assert c.cleanup() == ["kept: range r"]


def test_absent_facilities_skip_with_the_reason():
    c = ctx(absent={"vsphere"})
    c.require("lrs")
    with pytest.raises(acc.SkippedCheckError, match="vsphere declared absent"):
        c.require("vsphere")


def test_left_reports_unexpected_cleanup_statuses():
    assert acc._left((204, None), 204, 404) == ""
    assert acc._left((409, {"detail": "has exercises"}), 204) == '409 {"detail": "has exercises"}'


# ── personas ────────────────────────────────────────────────────────────


class FakeUserApi:
    def __init__(self, email, role):
        self.email, self.role = email, role

    def get(self, path, **kw):
        assert path == "/auth/me"
        return 200, {"user": {"id": f"id-{self.email}", "tenant_id": "tenant-1"}}


class FakeUsers:
    def __init__(self, existing):
        self.existing = dict(existing)  # email -> role

    def api(self, email):
        role = self.existing.get(email)
        return FakeUserApi(email, role) if role else None


class FakeAdmin:
    def __init__(self, users: FakeUsers, rows=()):
        self.users = users
        self.rows = list(rows)
        self.calls: list[tuple] = []

    def get(self, path, **kw):
        self.calls.append(("GET", path))
        return 200, self.rows

    def post(self, path, json=None, **kw):
        self.calls.append(("POST", path, json))
        self.users.existing[json["email"]] = json["role"]
        return 201, {"id": "new-1", **json}

    def patch(self, path, params=None, json=None, **kw):
        # As the real route: its fields are query parameters, and a JSON body is ignored.
        self.calls.append(("PATCH", path, params))
        row = next(r for r in self.rows if path.endswith(r["id"]))
        if params:
            self.users.existing[row["email"]] = params.get("role", row["role"])
        return 200, {**row, **(params or {})}

    def delete(self, path, **kw):
        self.calls.append(("DELETE", path))
        return 204, None


def test_demo_accounts_are_used_when_they_exist_with_the_right_role():
    users = FakeUsers({"trainee1.demo@truenorth.test": "student"})
    c = ctx(admin=FakeAdmin(users), users=users)
    api = c.persona("student")
    assert api.email == "trainee1.demo@truenorth.test" and api.user_id == "id-trainee1.demo@truenorth.test"
    assert c.persona("student") is api  # one client per persona
    assert c.persona_source["student"].startswith("demo account") and c.cleanups == []


def test_without_a_demo_account_an_acceptance_user_is_created_and_removed_afterwards():
    users = FakeUsers({})
    admin = FakeAdmin(users)
    c = ctx(admin=admin, users=users)
    api = c.persona("observer")
    assert api.email == "acceptance-observer@truenorth.test"
    assert (
        "POST",
        "/users",
        {"email": api.email, "display_name": "Acceptance observer", "role": "observer"},
    ) in admin.calls
    assert "created by this suite" in c.persona_source["observer"]
    assert c.cleanup() == [] and admin.calls[-1] == ("DELETE", "/users/new-1")


def test_a_demo_account_with_another_role_is_not_used_and_a_deactivated_acceptance_user_is_revived():
    users = FakeUsers({"instructor.demo@truenorth.test": "student"})
    row = {"id": "u9", "email": "acceptance-instructor@truenorth.test", "role": "instructor", "is_active": False}
    admin = FakeAdmin(users, rows=[row])
    c = ctx(admin=admin, users=users)
    assert c.persona("instructor").email == "acceptance-instructor@truenorth.test"
    assert ("PATCH", "/users/u9", {"is_active": "true", "role": "instructor"}) in admin.calls


# ── the multi-user harness (scripts/_inprocess_api.py) ──────────────────


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class RecordingClient:
    """Records which identity the switch held when each request arrived."""

    def __init__(self, switch, name):
        self.switch, self.name = switch, name
        self.cookies = {"truenorth_csrf": f"{name}-tok"}
        self.seen: list[tuple[str, object, str]] = []

    def request(self, method, path, content=None, files=None, params=None, headers=None):
        self.seen.append((path, self.switch.current, (headers or {}).get("x-csrf-token", "")))
        return FakeResponse(200, {"ok": True})

    def get(self, path, **kw):
        self.seen.append((path, self.switch.current, ""))
        return FakeResponse(200, {"raw": True})


def test_each_users_request_carries_its_own_identity_and_csrf_token():
    switch = _inprocess_api.IdentitySwitch()
    a = _inprocess_api.UserApi(RecordingClient(switch, "a"), "ident-a", switch, email="a@x", role="admin")
    b = _inprocess_api.UserApi(RecordingClient(switch, "b"), "ident-b", switch, email="b@x", role="student")
    a.get("/one")
    b.post("/two", json={})
    a.put("/three", json={})
    b.delete("/four")
    assert a.client.seen == [("/one", "ident-a", ""), ("/three", "ident-a", "a-tok")]
    assert b.client.seen == [("/two", "ident-b", "b-tok"), ("/four", "ident-b", "b-tok")]
    a.raw("/aar/pdf")
    assert a.client.seen[-1] == ("/aar/pdf", "ident-a", "")


def test_the_switch_answers_401_when_no_user_is_in_flight():
    from fastapi import HTTPException

    switch = _inprocess_api.IdentitySwitch()
    with pytest.raises(HTTPException) as err:
        switch()
    assert err.value.status_code == 401
    switch.current = "ident"
    assert switch() == "ident"


# ── command line ────────────────────────────────────────────────────────


def test_list_needs_no_stack(capsys):
    assert acc.main(["--admin", "a@b", "--list", "--modules", "lms"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out and all(line.startswith("lms ") for line in out)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--admin", "a@b", "--modules", "nope"], "unknown module"),
        (["--admin", "a@b", "--absent", "vsphere,teleporter"], "unknown --absent: teleporter"),
    ],
)
def test_bad_arguments_are_refused(argv, message, capsys):
    with pytest.raises(SystemExit) as exit_:
        acc.main(argv)
    assert exit_.value.code == 2 and message in capsys.readouterr().err


def test_shipped_content_is_found_for_the_content_checks():
    codes = acc._shipped("content/courses/*.yaml", "course_code")
    assert len(codes) == len(list((ROOT / "content/courses").glob("*.yaml"))) and "C103" in codes
    assert acc.GREYSPACE_TEMPLATE in acc._shipped("content/ranges/*/template.yaml", "name")
    assert (ROOT / "content/scenarios/incident-response-drill/scenario.yaml").is_file()


def test_the_acceptance_scenario_scores_what_the_checks_expect():
    import yaml

    doc = yaml.safe_load(acc.SCENARIO_YAML)
    assert doc["name"] == acc.SCENARIO_NAME
    assert {o["id"]: (o["type"], o["points"]) for o in doc["objectives"]} == acc.OBJECTIVES
    # The one inject writes synthetic records only, so a run never reaches a hypervisor.
    from scenario_engine.injectors.simulated_execution import SimulatedExecutionInjector

    assert [e["action"] for e in doc["timeline"]] == [SimulatedExecutionInjector.name]
    assert SimulatedExecutionInjector.touches_range_hosts is False


# ── web-shell: the SPA's root element (staging, v1.2.0-rc7: the check looked for <app-root>) ──

INDEX_HTML = ROOT / "control-plane/web/src/index.html"


class _Html:
    status_code = 200
    headers = {"content-type": "text/html; charset=utf-8"}

    def __init__(self, text: str) -> None:
        self.text = text


def test_the_spa_root_selector_is_the_shells_own():
    """The check's selector is the one custom element index.html's body bootstraps, and AppComponent's."""
    import re

    body = re.search(r"<body[^>]*>(.*?)</body>", INDEX_HTML.read_text(encoding="utf-8-sig"), re.S).group(1)
    assert re.findall(r"<([a-z][a-z0-9]*-[a-z0-9-]*)[\s>]", body) == [acc.SPA_ROOT_SELECTOR]
    app = (ROOT / "control-plane/web/src/app/app.component.ts").read_text(encoding="utf-8")
    assert f"selector: '{acc.SPA_ROOT_SELECTOR}'" in app


def test_web_spa_passes_on_the_real_index_and_fails_on_another_page(monkeypatch):
    index = INDEX_HTML.read_text(encoding="utf-8-sig")
    seen: list[str] = []
    monkeypatch.setattr(acc, "edge_get", lambda _c, path: seen.append(path) or _Html(index))
    assert "Angular shell" in acc.web_spa(ctx())
    assert seen == ["/", "/dashboard", "/learning/courses"]

    monkeypatch.setattr(acc, "edge_get", lambda _c, _p: _Html("<html><body><app-root></app-root></body></html>"))
    with pytest.raises(acc.FailedCheckError, match="not the Angular shell"):
        acc.web_spa(ctx())


# ── checks added by the 2026-10-20 amendment (#192) ─────────────────────
# Each against fakes of the API and of the database as stored; the audit export against an
# archive the worker's own writer produced, and the append-only guard against PostgreSQL.

AMENDED = {
    ("platform-core", "login banner (AC-8) on the realm's sign-in page"),
    ("platform-core", "audit log append-only: UPDATE and DELETE refused"),
    ("platform-core", "audit export: latest signed, chained, verifiable"),
    ("ranges-labs", "range credentials: staff read, Student 404, sealed at rest"),
    ("provisioners", "host-local template copies resolve per discovered host"),
    ("provisioners", "Security Onion per-range setup (so-setup)"),
    ("wiki-helpdesk", "Ranges space: system-owned, staff-only, no password stored"),
}


def test_the_amendment_adds_exactly_its_checks_to_existing_modules():
    doc = json.loads(acc.PERMITTED_SKIPS.read_text(encoding="utf-8"))
    [amendment] = doc["amendments"]
    assert (amendment["date"], amendment["issue"]) == ("2026-10-20", "#192")
    assert {tuple(c) for c in amendment["added_checks"]} == AMENDED
    registered = {(m, n) for m in acc.MODULES for n, _ in acc.CHECKS[m]}
    assert registered >= AMENDED and len(registered) == 49 and len(acc.MODULES) == 17
    matrix = (ROOT / doc["matrix"]).read_text(encoding="utf-8")
    assert "Amendment 2026-10-20" in matrix
    for module, name in AMENDED:
        assert f"`{module} / {name}`" in matrix, (module, name)


def _profile_report(profile: str) -> acc.Report:
    return acc.Report(strict=True, permitted=acc.load_permitted(acc.PERMITTED_SKIPS, profile)[0])


@pytest.mark.parametrize(
    ("module", "name", "reason", "ci", "staging"),
    [
        ("platform-core", "login banner (AC-8) on the realm's sign-in page",
         "keycloak declared absent on this stack (--absent)", acc.SKIP, acc.FAIL),
        ("platform-core", "audit export: latest signed, chained, verifiable",
         f"no audit export yet ...: {acc.NOTHING_TO_EXPORT}", acc.SKIP, acc.FAIL),
        ("platform-core", "audit export: latest signed, chained, verifiable",
         "/srv/truenorth/audit-archive is not visible in this container", acc.FAIL, acc.FAIL),
        ("provisioners", "host-local template copies resolve per discovered host",
         f"{acc.NEEDS_VCENTER}: no vSphere connection registered", acc.SKIP, acc.FAIL),
        ("provisioners", "Security Onion per-range setup (so-setup)",
         "... it needs a provisioned range on a writable vCenter", acc.SKIP, acc.SKIP),
        ("ranges-labs", "range credentials: staff read, Student 404, sealed at rest", "anything", acc.FAIL, acc.FAIL),
        ("wiki-helpdesk", "Ranges space: system-owned, staff-only, no password stored", "anything", acc.FAIL, acc.FAIL),
        ("platform-core", "audit log append-only: UPDATE and DELETE refused", "not PostgreSQL", acc.FAIL, acc.FAIL),
    ],
)
def test_amended_skips_stand_only_where_the_profile_lists_them(module, name, reason, ci, staging):
    for profile, want in (("ci-integration", ci), ("staging", staging), ("staging,ai-disabled", staging)):
        assert _profile_report(profile).add(module, name, acc.SKIP, reason, declared=True).status == want, profile


class Api:
    """One role's API: (METHOD, path) -> (status, body), or a callable(json, params) giving it.
    An unexpected request answers 599 (and is recorded), so a check that wanders fails."""

    def __init__(self, role: str, routes: dict, user_id: str = "uid") -> None:
        self.role, self.routes, self.user_id, self.tenant_id = role, dict(routes), user_id, "t1"
        self.calls: list[tuple] = []

    def request(self, method, path, json=None, params=None, **kw):
        key = (method.upper(), path)
        self.calls.append((*key, json))
        if key not in self.routes:
            return 599, f"unexpected {key}"
        r = self.routes[key]
        return r(json, params) if callable(r) else r

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def put(self, path, **kw):
        return self.request("PUT", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


def with_personas(c: acc.Ctx, **apis: Api) -> acc.Ctx:
    c._personas.update(apis)
    return c


# -- ranges-labs / range credentials --------------------------------------

RID, TPL = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
GENERATED = "Gen3rated-Pw.4acceptance"
STAFF_READ = (200, {"range_id": RID, "scope": "all", "accounts": [
    {"id": "c-an", "machine": "ws01", "username": "acceptance-analyst", "source": "declared", "scenario": True,
     "generated": False, "student_visible": True, "applied": False},
    {"id": "c-ad", "machine": "ws01", "username": "acceptance-admin", "source": "declared",
     "generated": True, "student_visible": False, "applied": False},
    {"id": "c-ub", "machine": "ws01", "username": "ubuntu", "source": "implicit",
     "generated": True, "student_visible": False, "applied": False},
]})
REVEALED = {"c-an": acc.CREDENTIALS_LITERAL, "c-ad": GENERATED}


def credentials_world(student=(404, {"detail": "Range not found"}), stored_yaml="name: x\n  password: tnsec:rc1:gAAA",
                      stored_passwords=("tnsec:rc1:aaa", "tnsec:rc1:bbb", "tnsec:rc1:ccc"), staff=STAFF_READ):
    admin = Api("admin", {
        ("GET", "/templates"): (200, []),
        ("POST", "/templates"): lambda j, p: (201, {"id": TPL, **j}),
        ("POST", "/ranges"): lambda j, p: (201, {"id": RID, "name": j["name"], "state": "created"}),
        ("GET", f"/ranges/{RID}/credentials"): staff,
        **{("POST", f"/ranges/{RID}/credentials/{cid}/reveal"): (200, {"id": cid, "password": pw})
           for cid, pw in REVEALED.items()},
        ("GET", "/audit-log"): (200, [{"action": "range_credential_reveal", "resource_id": RID,
                                       "detail": '{"machine": "ws01", "scope": "all"}'}] * 2),
        ("DELETE", f"/ranges/{RID}"): (204, None),
    })
    personas = {
        "instructor": Api("instructor", {("GET", f"/ranges/{RID}/credentials"): STAFF_READ}),
        "student": Api("student", {("GET", f"/ranges/{RID}/credentials"): student,
                                   ("GET", f"/templates/{TPL}"): (200, {"yaml": "password: '********'"})}),
        "outsider": Api("student", {("GET", f"/ranges/{RID}/credentials"): (404, {"detail": "Range not found"})}),
        "observer": Api("observer", {("GET", f"/ranges/{RID}/credentials"): (403, {"detail": "no"})}),
    }

    def sql(query, params):
        if "FROM templates" in query:
            assert str(params["id"]) == TPL
            return [(stored_yaml,)]
        if "FROM range_credentials" in query:
            assert str(params["id"]) == RID
            return [(p,) for p in stored_passwords]
        raise AssertionError(query)

    return admin, with_personas(ctx(admin=admin, options={"sql": sql}), **personas)


def test_range_credentials_pass_and_create_from_the_declaring_template():
    admin, c = credentials_world()
    detail = acc.ranges_credentials(c)
    assert "Students 404, Observer 403" in detail and "2 reveals audited" in detail
    posted = next(j for m, p, j in admin.calls if (m, p) == ("POST", "/templates"))
    assert posted["name"] == acc.CREDENTIALS_TEMPLATE and acc.CREDENTIALS_LITERAL in posted["yaml"]
    assert c.cleanup() == [] and admin.calls[-1][:2] == ("DELETE", f"/ranges/{RID}")


def test_the_credentials_template_declares_what_the_check_expects():
    """It validates as the API does (accounts rules, ADR 0012 §12, ADR 0013 §3): one typed-in
    scenario account, one generated."""
    import yaml
    from app.range_credentials import accounts

    specs, errors = accounts.parse_yaml(acc.CREDENTIALS_TEMPLATE_YAML)
    assert errors == []
    assert {s.username: ("generated" if s.generate else "declared literal") for s in specs} == acc.CREDENTIALS_ACCOUNTS
    assert yaml.safe_load(acc.CREDENTIALS_TEMPLATE_YAML)["accounts"][0]["password"] == acc.CREDENTIALS_LITERAL


@pytest.mark.parametrize(
    ("world", "message"),
    [
        ({"student": STAFF_READ}, "expected 404"),
        ({"stored_yaml": f"password: {acc.CREDENTIALS_LITERAL}"}, "not sealed"),
        ({"stored_passwords": ("tnsec:rc1:aaa", GENERATED)}, "stored unsealed"),
        ({"stored_passwords": ("tnsec:rc1:aaa", "tnsec:v1:bbb")}, "stored unsealed"),  # not the range key
        # ADR 0013 §3: the list never carries a password.
        ({"staff": (200, {**STAFF_READ[1], "accounts": [
            {**a, "password": "x"} for a in STAFF_READ[1]["accounts"]]})}, "the list shows"),
        # A range never built may not claim an account is set (setup_status, #216).
        ({"staff": (200, {**STAFF_READ[1], "accounts": [
            {**a, "setup_status": "being_set_up"} for a in STAFF_READ[1]["accounts"]]})}, "claim to be set"),
    ],
)
def test_range_credentials_fail_on_a_leak(world, message):
    _, c = credentials_world(**world)
    with pytest.raises(acc.FailedCheckError, match=message):
        acc.ranges_credentials(c)


# -- wiki-helpdesk / Ranges space -----------------------------------------

PID = "33333333-3333-3333-3333-333333333333"


def wiki_world(put=(409, {"detail": "managed by TrueNorth"}), student_page=(404, {"detail": "Page not found"}),
               stored="## Accounts\n| `ws01` | `acceptance-analyst` | user |", staff_body="acceptance-analyst | user"):
    name = f"{acc.PREFIX}credentials-t1"
    admin = Api("admin", {
        ("GET", "/wiki/spaces"): (200, [{"slug": "ranges", "system_owned": True, "visibility": "staff"},
                                        {"slug": "howto", "system_owned": False, "visibility": "all"}]),
        ("PUT", "/wiki/spaces/ranges"): put,
        ("GET", "/wiki/spaces/ranges/tree"): (200, [{"id": "x", "title": "Other", "children": [
            {"id": PID, "title": name, "children": []}]}]),
    })
    instructor = Api("instructor", {("GET", f"/wiki/pages/{PID}"): (200, {
        "id": PID, "tags": f"range-doc,range:{RID}", "body": staff_body})})

    def reader(role):
        return Api(role, {("GET", "/wiki/spaces"): (200, [{"slug": "howto"}]),
                          ("GET", "/wiki/spaces/ranges"): (404, {"detail": "Space not found"}),
                          ("GET", f"/wiki/pages/{PID}"): student_page})

    def sql(query, params):
        assert "FROM wiki_pages" in query and str(params["id"]) == PID
        return [(stored,)]

    c = with_personas(ctx(admin=admin, options={"sql": sql}), instructor=instructor, student=reader("student"),
                      observer=reader("observer"))
    c._memo["the credentials range"] = ({"id": RID, "name": name, "template_id": TPL}, None, False)
    return c


def test_ranges_space_passes_when_system_owned_and_hidden():
    assert "space 'ranges' system-owned and staff-only" in acc.wiki_ranges_space(wiki_world())


@pytest.mark.parametrize(
    ("world", "message"),
    [
        ({"put": (200, {"slug": "ranges", "visibility": "all"})}, "expected 409"),
        ({"student_page": (200, {"body": "x"})}, "as student => 200, expected 404"),
        ({"stored": f"analyst / {acc.CREDENTIALS_LITERAL}"}, "holds an account password"),
        ({"stored": "{{tn-credential:44444444-4444-4444-4444-444444444444}}"}, "holds credential tokens"),
        ({"staff_body": f"acceptance-analyst / {acc.CREDENTIALS_LITERAL}"}, "shows staff an account password"),
    ],
)
def test_ranges_space_fails_when_open_or_storing_passwords(world, message):
    with pytest.raises(acc.FailedCheckError, match=message):
        acc.wiki_ranges_space(wiki_world(**world))


# -- provisioners / host-local template copies ------------------------------


def copies_world(hosts):
    admin = Api("admin", {
        ("GET", "/hypervisors/connections"): (200, [{"id": "c1", "name": "vc", "hypervisor_type": "vsphere",
                                                     "is_primary": True}]),
        ("GET", "/hypervisors/connections/c1/nodes"): (200, [{"node_name": h} for h in hosts]),
    })
    return ctx(admin=admin)


def test_template_copies_resolve_for_named_hosts():
    detail = acc.provisioners_template_copies(copies_world(["esx01.truenorth.lab", "ESX02.truenorth.lab", "esx03"]))
    assert detail.startswith("vc: 3 hosts")


def test_template_copies_fail_where_build_sh_and_the_provisioner_disagree():
    """A host registered by IP address: build.sh names its copy <id>@10-<date> (up to the first
    dot), the provisioner looks for <id>@10.30.30.21-<date>, which cannot even parse."""
    with pytest.raises(acc.FailedCheckError, match=r"host '10\.30\.30\.21'"):
        acc.provisioners_template_copies(copies_world(["esx01.truenorth.lab", "10.30.30.21"]))


def test_template_copies_skip_without_a_vcenter():
    with pytest.raises(acc.SkippedCheckError, match=acc.NEEDS_VCENTER) as err:
        acc.provisioners_template_copies(ctx(absent={"vsphere"}))
    assert err.value.declared is True
    admin = Api("admin", {("GET", "/hypervisors/connections"): (200, [])})
    with pytest.raises(acc.SkippedCheckError, match=acc.NEEDS_VCENTER) as err:
        acc.provisioners_template_copies(ctx(admin=admin))
    assert err.value.declared is False


def test_replicate_name_is_build_sh_naming():
    script = (ROOT / "infra/vsphere/packer/build.sh").read_text(encoding="utf-8")
    assert 'short="${host%%.*}"' in script and 'copy="${id}@${short}-${date}"' in script
    assert acc.replicate_name("srv2022", "esx03.truenorth.lab", "20261019") == "srv2022@esx03-20261019"


# -- platform-core / login banner ------------------------------------------

BANNER = "Site notice: it''s '{'monitored'}'. Use is recorded."
SIGN_IN = ('<div id="tn-banner"><div class="tn-banner-text">Site notice: it&#39;s {monitored}. Use is recorded.'
           '</div></div><form><input type="checkbox" id="tn-ack" name="tn_ack" required></form>')


def banner_world(monkeypatch, *, theme="truenorth", l10n=None, page=SIGN_IN):
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", "x")
    monkeypatch.setattr(acc, "_keycloak_token", lambda base: "tok")
    if l10n is None:
        l10n = {"tnLoginBannerTitle": "Notice", "tnLoginBanner": BANNER, "tnLoginBannerAck": "I accept"}

    def get(url, token=None, params=None):
        if url.endswith("/admin/realms/truenorth"):
            return 200, {"realm": "truenorth", "loginTheme": theme}
        if url.endswith("/localization/en"):
            return 200, l10n
        if url.endswith("/clients"):
            assert params == {"clientId": "truenorth-web"} and token == "tok"
            return 200, [{"clientId": "truenorth-web", "redirectUris": ["https://tn.example/*"]}]
        assert url.endswith("/realms/truenorth/protocol/openid-connect/auth") and token is None
        assert params["redirect_uri"] == "https://tn.example/" and params["code_challenge_method"] == "S256"
        return 200, page

    monkeypatch.setattr(acc, "_http_get", get)


def test_login_banner_passes_when_the_page_shows_the_realms_text(monkeypatch):
    banner_world(monkeypatch)
    assert "theme truenorth" in acc.core_login_banner(ctx())


@pytest.mark.parametrize(
    ("world", "message"),
    [
        ({"theme": "keycloak"}, "loginTheme is 'keycloak'"),
        ({"l10n": {"tnLoginBannerTitle": "Notice"}}, "no site text"),
        ({"page": SIGN_IN.replace(" required", "")}, "no required acknowledgement"),
        ({"page": SIGN_IN.replace("Site notice", "Old notice")}, "does not show the realm's banner text"),
    ],
)
def test_login_banner_fails(monkeypatch, world, message):
    banner_world(monkeypatch, **world)
    with pytest.raises(acc.FailedCheckError, match=message):
        acc.core_login_banner(ctx())


def test_login_banner_skips_without_keycloak_or_its_credential(monkeypatch):
    with pytest.raises(acc.SkippedCheckError, match="keycloak declared absent"):
        acc.core_login_banner(ctx(absent={"keycloak"}))
    monkeypatch.delenv("KEYCLOAK_ADMIN_PASSWORD", raising=False)
    with pytest.raises(acc.SkippedCheckError, match="KEYCLOAK_ADMIN_PASSWORD"):
        acc.core_login_banner(ctx())


def test_message_text_undoes_the_installers_escaping():
    assert acc.message_text(BANNER) == "Site notice: it's {monitored}. Use is recorded."


# -- platform-core / audit log append-only ----------------------------------

GUARD_UPDATE = "audit_logs rows are never changed (row x)"
GUARD_DELETE = "audit_logs rows are deleted only by retention after a verified export (row x)"


def append_only_ctx(update=GUARD_UPDATE, delete=GUARD_DELETE):
    tried = []

    def refuses(statement, params):
        tried.append(statement.split()[0])
        return update if statement.startswith("UPDATE") else delete

    c = ctx(options={"sql": lambda q, p: [("row-1",)], "refuses": refuses, "dialect": "postgresql"})
    return c, tried


def test_append_only_passes_when_both_are_refused_by_the_guard():
    c, tried = append_only_ctx()
    assert "UPDATE refused" in acc.core_audit_append_only(c) and tried == ["UPDATE", "DELETE"]


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"update": None}, "UPDATE of audit row row-1 was accepted"),
        ({"delete": None}, "DELETE of audit row row-1 was accepted"),
        ({"delete": "permission denied for table audit_logs"}, "not by the audit guard"),
    ],
)
def test_append_only_fails_without_the_guard(kw, message):
    with pytest.raises(acc.FailedCheckError, match=message):
        acc.core_audit_append_only(append_only_ctx(**kw)[0])


def test_append_only_skips_off_postgresql():
    with pytest.raises(acc.SkippedCheckError, match="not PostgreSQL"):
        acc.core_audit_append_only(ctx(options={"dialect": "sqlite"}))


def test_append_only_on_postgresql_changes_nothing(postgres_engine):
    """The real guard (migration b7c8d9e0f1a2): both refused, the row as it was."""
    from sqlalchemy import text

    rid = uuid.uuid4()
    with postgres_engine.begin() as conn:
        conn.execute(text("INSERT INTO audit_logs (id, timestamp, action, resource_type, resource_id, detail) "
                          "VALUES (:id, now(), 'probe', 'probe', 'x', 'kept')"), {"id": rid})
    assert "DELETE refused" in acc.core_audit_append_only(ctx(options={"engine": postgres_engine}))
    with postgres_engine.connect() as conn:
        assert conn.execute(text("SELECT detail FROM audit_logs WHERE id = :id"), {"id": rid}).scalar() == "kept"


# -- platform-core / signed audit export ------------------------------------
# The archive is written by the worker's own code (worker/audit_jobs.run_export), so the
# check's independent verifier is held to the format the worker actually writes.


@pytest.fixture
def exported(tmp_path):
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from worker import audit_jobs, audit_signing
    from worker.audit_archive import FilesystemArchive
    from worker.tables import audit_export_checks, audit_exports, audit_logs

    eng = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    audit_logs.metadata.create_all(eng, tables=[audit_logs, audit_exports, audit_export_checks])
    today = datetime.now(UTC).date()
    with Session(eng) as db:
        for days_ago in (3, 1):
            db.execute(audit_logs.insert().values(
                id=uuid.uuid4(), timestamp=datetime.combine(today - timedelta(days=days_ago), time(12), tzinfo=UTC),
                action="probe", resource_type="probe", resource_id="x"))
        db.commit()
        written = audit_jobs.run_export(db, FilesystemArchive(tmp_path / "archive"),
                                        audit_signing.from_seed("acceptance-seed-" + "x" * 40), today)
    assert len(written) == 3  # three days, one of them empty: still a manifest
    yield eng, tmp_path / "archive"
    eng.dispose()


def export_ctx(eng, archive):
    return ctx(options={"engine": eng, "audit_archive": str(archive)})


def test_audit_export_verifies_the_workers_archive(exported):
    detail = acc.core_audit_export(export_ctx(*exported))
    assert detail.startswith("3 exports in one chain") and "verified with its file" in detail


def _newest_manifest(archive):
    return sorted(archive.rglob("*.manifest.json"))[-1]


def test_audit_export_fails_on_a_changed_file(exported):
    eng, archive = exported
    data = sorted(archive.rglob("*.jsonl.gz"))[-1]
    data.write_bytes(data.read_bytes() + b"x")
    with pytest.raises(acc.FailedCheckError, match="does not match its file_sha256"):
        acc.core_audit_export(export_ctx(eng, archive))


def test_audit_export_fails_on_a_signature_by_another_key(exported):
    from worker import audit_signing

    eng, archive = exported
    m = _newest_manifest(archive)
    other = audit_signing.from_seed("another-seed-" + "y" * 40)
    m.with_name(m.name + ".sig").write_bytes(other.sign(m.read_bytes()))
    with pytest.raises(acc.FailedCheckError, match="signature does not verify"):
        acc.core_audit_export(export_ctx(eng, archive))


def test_audit_export_fails_on_an_edited_manifest(exported):
    eng, archive = exported
    m = _newest_manifest(archive)
    m.write_bytes(m.read_bytes().replace(b'"schema":1', b'"schema":2'))
    with pytest.raises(acc.FailedCheckError, match="does not hash to audit_exports.manifest_sha256"):
        acc.core_audit_export(export_ctx(eng, archive))


def test_export_chain_finds_a_missing_link_and_a_fork():
    def row(sha, prev):
        return ("2026-10-10", 0, "f", "h", sha, prev, "k")

    assert acc.export_chain([row("a", None), row("b", "a"), row("c", "b")])[0] == []
    assert "does not hold" in acc.export_chain([row("a", None), row("c", "b")])[0][0]
    assert "2 exports have no successor" in acc.export_chain([row("a", None), row("b", "a"), row("c", "a")])[0][-1]


def test_audit_export_without_the_archive_mounted_skips(exported):
    eng, archive = exported
    with pytest.raises(acc.SkippedCheckError, match="mount the audit archive read-only"):
        acc.core_audit_export(export_ctx(eng, archive.parent / "not-mounted"))


def test_audit_export_none_yet():
    now = datetime.now(UTC)

    def world(oldest):
        return ctx(options={"sql": lambda q, p: [] if "audit_exports" in q else [(oldest,)]})

    with pytest.raises(acc.SkippedCheckError, match=acc.NOTHING_TO_EXPORT):
        acc.core_audit_export(world(now - timedelta(hours=20)))
    with pytest.raises(acc.FailedCheckError, match="no export at all"):
        acc.core_audit_export(world(now - timedelta(days=4)))
