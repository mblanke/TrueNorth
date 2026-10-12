"""scripts/staging_acceptance.py: the report, the runner's PASS/FAIL/SKIP rules, fixtures,
cleanup, personas, the command line, and the multi-user harness in scripts/_inprocess_api.py
(fakes only; the checks themselves run against a stack, see docs/runbooks/staging-acceptance.md)."""

from __future__ import annotations

import importlib.util
import json
import sys
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
    for module, name in (("ranges-labs", "lab build on the hypervisor"), ("noise-engine", "deploy to agents")):
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


def test_staging_permits_only_the_two_hardware_skips():
    entries, _ = acc.load_permitted(acc.PERMITTED_SKIPS, "staging")
    assert {(e["module"], e["check"], e["milestone"]) for e in entries} == {
        ("ranges-labs", "lab build on the hypervisor", "H"),
        ("noise-engine", "deploy to agents", "H"),
    }


@pytest.mark.parametrize("profile", ["staging", "ci-integration"])
def test_each_permitted_skip_matches_the_reason_its_check_gives(profile):
    """Run each permitted check where it skips (the CI stack's absent facilities); the reason
    it gives must carry the entry's reason_contains, or the entry can never apply."""
    entries, _ = acc.load_permitted(acc.PERMITTED_SKIPS, profile)
    absent = {"edge", "keycloak", "minio", "lrs", "moodle", "backup", "vsphere", "metrics-token", "arc2"}
    for e in entries:
        with pytest.raises(acc.SkippedCheckError) as err:
            dict(acc.CHECKS[e["module"]])[e["check"]](ctx(absent=absent))
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
    ai = [*ok_[:5], comp("ai_orchestrator", "degraded", "HTTP 502"), ok_[6]]
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
