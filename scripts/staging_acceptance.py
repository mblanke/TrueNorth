#!/usr/bin/env python3
"""Post-install acceptance for TrueNorth: one check group per module, on an installed host.

Runs inside a one-off api container, in-process, as the bootstrap administrator (the same
harness as scripts/load_content_inprocess.py and scripts/arc2_batch_inprocess.py:
scripts/_inprocess_api.py). Every request goes through the API's routes, permission checks,
CSRF and validation; behind them are the installed stack's own services: PostgreSQL, Redis,
OpenSearch, MinIO, the LRS, the AI orchestrator, the Celery workers (a scenario run fires a
synthetic inject on worker-scenario, whose telemetry worker-telemetry indexes) and the
registered Moodle. Nothing is mocked here. Other roles are existing users: the installer's
demo accounts (tn_load_shipped_demo), looked up by email, or, when there are none, users this
suite creates (``acceptance-<role>@truenorth.test``). No password or token is used.

    docker compose -p docker -f compose.prod.yml --env-file /srv/truenorth/config/.env.production \\
      run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v /srv/truenorth/app:/srcapp:ro \\
      --entrypoint python api /srcapp/scripts/staging_acceptance.py --admin <upn>

Output: a table (module | check | PASS/FAIL/SKIP | detail), and with --json a report file.
Exit 1 on any FAIL, 2 when the suite cannot start. A SKIP always states its reason; a check
that can only be skipped on this stack (no real vSphere, a service an operator declared
absent with --absent) says so rather than passing. With --strict only the skips the release
permits (docs/release-notes/v1.2.0-permitted-skips.json, --skip-profile) stand.

Test data is named ``acceptance-...``. Fixtures that exercises keep as history (the range,
scenario and template an exercise ran on) are reused by name on the next run; the rest is
removed afterwards unless --keep. Runbook: docs/runbooks/staging-acceptance.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[1]
PREFIX = "acceptance-"
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
STATUSES = (PASS, FAIL, SKIP)

# Run (and report) order. Detection needs the exercise scenario-engine starts; reporting-aar
# completes it.
MODULES = (
    "infra",
    "platform-core",
    "web-shell",
    "content",
    "curriculum",
    "lms",
    "scenario-engine",
    "telemetry",
    "detection",
    "reporting-aar",
    "ai-arc2",
    "ranges-labs",
    "provisioners",
    "scheduling",
    "noise-engine",
    "greyspace",
    "wiki-helpdesk",
)

# What an operator may declare absent (--absent): the checks that need it SKIP, saying so.
FACILITIES = {
    "edge": "nginx in front of the SPA and the API (--edge-url)",
    "keycloak": "Keycloak (deep health; demo-account links need its admin credential)",
    "minio": "MinIO object storage",
    "lrs": "the xAPI LRS and cmi5 launching (CMI5_LRS_AUTH)",
    "moodle": "a registered Moodle platform",
    "backup": "the host's backup environment (backup.env)",
    "vsphere": "a vCenter (real or simulated) behind a hypervisor connection",
    "metrics-token": "METRICS_SCRAPE_TOKEN on the API",
    "arc2": "the ARC² Course Studio (ARC2_STUDIO_ENABLED)",
    "telemetry-pipeline": "OpenSearch's ingested-at pipeline (telemetry.pipelines.bootstrap)",
}

# The release's permitted skips, per environment (docs/release-notes/v1.2.0-module-matrix.md).
# Under --strict a SKIP stands only when its profile has an entry naming its module and check
# whose ``reason_contains`` is in the skip's reason; any other SKIP is a FAIL, declared or not.
# The file's ``checks`` inventory is every check the release expects: one missing, or one
# not recorded there, is a FAIL too.
PERMITTED_SKIPS = SRC / "docs" / "release-notes" / "v1.2.0-permitted-skips.json"
SKIP_ENTRY_KEYS = ("module", "check", "reason_contains", "reason", "alternative_evidence", "milestone")


def load_permitted(path: Path, profile: str) -> tuple[list[dict], dict[str, list[str]]]:
    """(the profile's permitted skips, the check inventory). ``profile`` may name several,
    comma-separated (``staging,ai-disabled``): their entries together. ValueError when the
    file, a profile or an entry is malformed, or an entry names a check that does not exist."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    profiles = doc.get("profiles") or {}
    names = [p.strip() for p in profile.split(",") if p.strip()]
    if not names:
        raise ValueError(f"no skip profile named (has: {', '.join(sorted(profiles))})")
    entries = []
    for name in names:
        if name not in profiles:
            raise ValueError(f"no skip profile {name!r} in {path} (has: {', '.join(sorted(profiles))})")
        entries += list(profiles[name])
    for e in entries:
        if lacking := [k for k in SKIP_ENTRY_KEYS if not str(e.get(k) or "").strip()]:
            raise ValueError(f"{path}: {profile} entry {e.get('module')} / {e.get('check')} lacks {', '.join(lacking)}")
        if e["check"] not in dict(CHECKS.get(e["module"], [])):
            raise ValueError(f"{path}: {profile} permits skipping {e['module']} / {e['check']}, which is not a check")
    return entries, dict(doc.get("checks") or {})


def permitted_skip(entries: Iterable[dict], module: str, check: str, detail: str) -> dict | None:
    return next(
        (e for e in entries if e["module"] == module and e["check"] == check and e["reason_contains"] in detail), None
    )


def inventory_drift(
    checks: dict[str, list[tuple[str, Check]]], inventory: dict[str, list[str]], modules: Iterable[str]
) -> list[tuple[str, str, str]]:
    """(module, check, problem) for each difference between the registered checks and the inventory."""
    out = []
    for m in modules:
        have, want = [n for n, _ in checks.get(m, [])], list(inventory.get(m, []))
        out += [(m, n, "in the release's check inventory but not registered: a missing check") for n in want if n not in have]
        out += [(m, n, "registered but not in the release's check inventory") for n in have if n not in want]
    return out

# The installer's demo people (scripts/load_content.py DEMO_PEOPLE), by persona.
PERSONAS = {
    "instructor": ("instructor.demo@truenorth.test", "instructor"),
    "student": ("trainee1.demo@truenorth.test", "student"),  # the participant
    "outsider": ("trainee2.demo@truenorth.test", "student"),  # enrolled in nothing here
    "observer": ("observer.demo@truenorth.test", "observer"),
    # The Moodle loop's own Student, never a demo account: its Moodle results land on its own
    # enrolment and leave the participant's record alone. Deactivated after a run and
    # reactivated by the next, so it keeps one TrueNorth id and one Moodle account.
    "moodle-student": ("acceptance-moodle-student@truenorth.test", "student"),
}

TEMPLATE_NAME = PREFIX + "template"
TEMPLATE_YAML = (
    f"name: {TEMPLATE_NAME}\n"
    "description: Post-install acceptance (scripts/staging_acceptance.py); never provisioned.\n"
    "environment: enterprise\n"
    "nodes:\n"
    "  - id: ws01\n    role: workstation\n    os: ubuntu22\n    vcpu: 1\n    ram_mb: 512\n    disk_gb: 8\n"
)
SCENARIO_NAME = PREFIX + "scenario"
# One timeline event whose injector writes synthetic records only (touches_range_hosts is
# False in scenario_engine/injectors/simulated_execution.py), so a run on a range that was
# never built still fires it, on every backend, without reaching a hypervisor.
SCENARIO_YAML = f"""name: {SCENARIO_NAME}
version: "1"
description: Post-install acceptance (scripts/staging_acceptance.py). Synthetic records only.
timeline:
  - t: "0:00"
    action: simulated_execution
    description: Synthetic process record; no range host is touched
    params: {{technique: T1059.001, process_name: acceptance.exe, target: acceptance-host}}
objectives:
  - id: detect_beacon
    type: detection
    validator: opensearch_query
    params: {{query: "event_type:acceptance_beacon", min_hits: 1}}
    points: 40
  - id: write_report
    type: deliverable
    validator: deliverable_check
    points: 10
"""
OBJECTIVES = {"detect_beacon": ("detection", 40), "write_report": ("deliverable", 10)}
NOISE_TEMPLATE_YAML = f"""name: {PREFIX}noise-template
nodes:
  - {{id: web01, role: web_server, os: ubuntu-2404, vlan: corporate_lan, ip: 10.10.0.20}}
  - {{id: lnx01, role: workstation, os: ubuntu-2404, vlan: corporate_lan, ip: 10.10.0.50}}
noise:
  enabled: true
  preset: office
  personas: 4
  mgmt: {{controller_url: "https://10.255.0.1/api"}}
"""
NOISE_TARGETS = {
    "web": ["http://10.10.0.20/"],
    "dns": ["web01.corp.local"],
    "subnet": ["10.10.0.0/24"],
    "hosts": ["10.10.0.50"],
}
GREYSPACE_TEMPLATE = "Small Office with Greyspace Internet"  # content/ranges/greyspace-internet


# ── outcomes ────────────────────────────────────────────────────────────


class FailedCheckError(Exception):
    """The flow did not do what it must on this host."""


class SkippedCheckError(Exception):
    """The flow cannot be checked on this host; the message is the reason. ``declared``: the
    reason was known before the run (a facility named in --absent, or a check this suite
    cannot make on any staging host), as opposed to something found missing on the way."""

    def __init__(self, reason: str, declared: bool = False):
        super().__init__(reason)
        self.declared = declared


def expect(cond: Any, detail: str) -> None:
    if not cond:
        raise FailedCheckError(detail)


def skip(reason: str, declared: bool = False) -> None:
    raise SkippedCheckError(reason, declared)


def short(body: Any, n: int = 220) -> str:
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def ok(resp: tuple[int, Any], *codes: int, what: str) -> Any:
    """The body of ``resp`` when its status is one of ``codes`` (default 200), else fail."""
    status, body = resp
    if status not in (codes or (200,)):
        raise FailedCheckError(f"{what} => {status} {short(body)}")
    return body


def items(body: Any) -> list:
    """A list endpoint's rows, whether it answers a list or ``{items: [...]}``."""
    if isinstance(body, dict):
        body = body.get("items", body.get("results", body.get("hits", [])))
        if isinstance(body, dict):  # OpenSearch: {hits: {hits: [...]}}
            body = body.get("hits", [])
    return body if isinstance(body, list) else []


def longest_text(value: Any) -> str:
    """The longest string anywhere in a JSON value."""
    if isinstance(value, str):
        return value
    found = (
        [longest_text(v) for v in (value.values() if isinstance(value, dict) else value or [])]
        if isinstance(value, (dict, list))
        else []
    )
    return max(found, key=len, default="")


# ── report ──────────────────────────────────────────────────────────────


@dataclass
class Result:
    module: str
    check: str
    status: str
    detail: str = ""
    seconds: float = 0.0
    declared: bool = False  # a SKIP whose reason was declared before the run


class Report:
    def __init__(self, strict: bool = False, permitted: list[dict] | None = None) -> None:
        self.results: list[Result] = []
        self.strict = strict
        self.permitted = permitted  # --strict with a permitted-skips profile: only those skips stand

    def add(
        self, module: str, check: str, status: str, detail: str = "", seconds: float = 0.0, declared: bool = False
    ) -> Result:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        detail = " ".join(str(detail or "").split())
        if status == SKIP and not detail:
            # A skip is a claim that this host cannot show the flow; without a reason it is
            # indistinguishable from a check that quietly did nothing.
            status, detail = FAIL, "skipped without a stated reason"
        elif status == SKIP and self.strict and self.permitted is not None:
            # The release's permitted list is the authority: a declared skip it does not name
            # (an --absent facility, a by-design skip) is as much a FAIL as one found on the way.
            if permitted_skip(self.permitted, module, check, detail) is None:
                status, detail = FAIL, f"skip not in the permitted list (--strict): {detail}"
        elif status == SKIP and self.strict and not declared:
            # --strict (CI): only skips declared up front may pass; anything found missing on
            # the way is a stack that cannot show the flow it was started to show.
            status, detail = FAIL, f"unexpected skip (--strict): {detail}"
        result = Result(module, check, status, detail, round(seconds, 2), status == SKIP and declared)
        self.results.append(result)
        return result

    def counts(self) -> dict[str, int]:
        return {s: sum(1 for r in self.results if r.status == s) for s in STATUSES}

    def exit_code(self) -> int:
        return 1 if any(r.status == FAIL for r in self.results) else 0

    def table(self, width: int = 170) -> str:
        mw = max([len("module")] + [len(r.module) for r in self.results])
        cw = max([len("check")] + [len(r.check) for r in self.results])
        head = f"{'module':<{mw}} | {'check':<{cw}} | {'status':<6} | detail"
        lines = [head, "-" * len(head)]
        room = max(20, width - mw - cw - 15)
        for r in self.results:
            detail = r.detail if len(r.detail) <= room else r.detail[: room - 1] + "…"
            lines.append(f"{r.module:<{mw}} | {r.check:<{cw}} | {r.status:<6} | {detail}")
        c = self.counts()
        lines.append("-" * len(head))
        lines.append(f"{c[PASS]} passed, {c[FAIL]} failed, {c[SKIP]} skipped")
        return "\n".join(lines)

    def to_dict(self, meta: dict | None = None) -> dict:
        return {
            "meta": meta or {},
            "counts": self.counts(),
            "passed": self.exit_code() == 0,
            "results": [asdict(r) for r in self.results],
        }

    def write_json(self, path: Path, meta: dict | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(meta), indent=2, default=str) + "\n", encoding="utf-8")


# ── checks registry and runner ──────────────────────────────────────────

Check = Callable[["Ctx"], Any]
CHECKS: dict[str, list[tuple[str, Check]]] = {m: [] for m in MODULES}


def check(module: str, name: str) -> Callable[[Check], Check]:
    if module not in CHECKS:
        raise ValueError(f"unknown module {module!r}")

    def deco(fn: Check) -> Check:
        CHECKS[module].append((name, fn))
        return fn

    return deco


def select_modules(spec: str | None) -> list[str]:
    if not spec:
        return list(MODULES)
    wanted = [m.strip() for m in spec.split(",") if m.strip()]
    unknown = [m for m in wanted if m not in MODULES]
    if unknown:
        raise ValueError(f"unknown module(s): {', '.join(unknown)} (known: {', '.join(MODULES)})")
    return [m for m in MODULES if m in wanted]


def run_checks(
    ctx: Ctx,
    modules: Iterable[str],
    report: Report,
    checks: dict[str, list[tuple[str, Check]]] | None = None,
    log: Callable[[str], None] = print,
) -> Report:
    for module in modules:
        for name, fn in (checks or CHECKS)[module]:
            start = ctx.clock()
            declared = False
            try:
                detail, status = fn(ctx) or "", PASS
            except SkippedCheckError as exc:
                status, detail, declared = SKIP, str(exc), exc.declared
            except FailedCheckError as exc:
                status, detail = FAIL, str(exc)
            except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check, with its reason
                status, detail = FAIL, f"{type(exc).__name__}: {exc}"
            r = report.add(module, name, status, str(detail), ctx.clock() - start, declared)
            log(f"  {r.status:<4}  {module} / {name}: {r.detail}")
    return report


# ── context: users, fixtures, cleanup ───────────────────────────────────


class Ctx:
    """What a check needs: the administrator's API, other users, shared fixtures (made once,
    on first use, so any --modules subset works), polling, and the cleanup list."""

    def __init__(
        self,
        admin: Any,
        users: Any = None,
        *,
        tag: str | None = None,
        keep: bool = False,
        absent: Iterable[str] = (),
        options: dict | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
    ):
        self.admin = admin
        self.users = users
        self.tag = tag or datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.keep = keep
        self.absent = set(absent)
        self.opt = options or {}
        self.sleep = sleep
        self.clock = clock
        self.log = log
        self.cleanups: list[tuple[str, Callable[[], Any]]] = []
        self.persona_source: dict[str, str] = {}
        self._memo: dict[str, tuple[Any, str | None, bool]] = {}
        self._personas: dict[str, Any] = {}

    # -- environment ----------------------------------------------------

    def require(self, facility: str) -> None:
        if facility in self.absent:
            skip(f"{facility} declared absent on this stack (--absent): {FACILITIES.get(facility, facility)}", True)

    # -- fixtures -------------------------------------------------------

    def memo(self, key: str, make: Callable[[], Any]) -> Any:
        """``make()`` once. If it failed, every later user SKIPs, naming the failure (the
        first user reported it as its own FAIL or SKIP)."""
        if key in self._memo:
            value, error, declared = self._memo[key]
            if error is not None:
                skip(f"needs {key}, which could not be set up: {error}", declared)
            return value
        try:
            value = make()
        except Exception as exc:  # FailedCheckError, SkippedCheckError or a crash: remembered, then re-raised
            self._memo[key] = (None, str(exc) or type(exc).__name__, getattr(exc, "declared", False))
            raise
        self._memo[key] = (value, None, False)
        return value

    def on_cleanup(self, label: str, fn: Callable[[], Any]) -> None:
        self.cleanups.append((label, fn))

    def cleanup(self) -> list[str]:
        """Undo in reverse order; never raises. Returns what was left and why."""
        if self.keep:
            return [f"kept: {label}" for label, _ in self.cleanups]
        left = []
        for label, fn in reversed(self.cleanups):
            try:
                outcome = fn()
            except Exception as exc:  # noqa: BLE001 - cleanup must not stop the rest
                outcome = f"{type(exc).__name__}: {exc}"
            if isinstance(outcome, str) and outcome:
                left.append(f"{label}: {outcome}")
        self.cleanups.clear()
        return left

    def poll(self, fn: Callable[[], Any], what: str, timeout: float = 90, interval: float = 2) -> Any:
        deadline = self.clock() + timeout
        last = None
        while True:
            last = fn()
            if last:
                return last
            if self.clock() >= deadline:
                raise FailedCheckError(f"timed out after {timeout:.0f}s waiting for {what}")
            self.sleep(interval)

    # -- the database, read as stored -------------------------------------
    # A few checks (sealed at rest, append-only, the export chain) must see what PostgreSQL
    # holds, not what the API makes of it: the API unseals on load. Same database, same
    # credentials as the api container this suite runs in (app.db.engine). Tests pass
    # ``options={"engine": ...}`` (a real PostgreSQL) or ``options={"sql": ..., "refuses": ...}``.

    def _engine(self) -> Any:
        engine = self.opt.get("engine")
        if engine is None:
            from app.db import engine
        return engine

    def sql(self, query: str, **params: Any) -> list[tuple]:
        """Rows of a read-only query, as the database holds them. Always rolled back."""
        if "sql" in self.opt:
            return [tuple(r) for r in self.opt["sql"](query, params)]
        from sqlalchemy import text

        with self._engine().connect() as conn:
            try:
                return [tuple(r) for r in conn.execute(text(query), params)]
            finally:
                conn.rollback()

    def refuses(self, statement: str, **params: Any) -> str | None:
        """The database's error for ``statement``, or None when it ran. Run in a transaction
        that is rolled back whatever happens, so a statement the database should have refused
        changes nothing even when it was not refused."""
        if "refuses" in self.opt:
            return self.opt["refuses"](statement, params)
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError

        with self._engine().connect() as conn:
            try:
                conn.execute(text(statement), params)
                return None
            except DBAPIError as exc:
                return " ".join(str(getattr(exc, "orig", exc)).split())
            finally:
                conn.rollback()

    def dialect(self) -> str:
        return self.opt.get("dialect") or self._engine().dialect.name

    # -- users ----------------------------------------------------------

    def persona(self, key: str) -> Any:
        """An API client acting as the persona: the installer's demo account when it exists
        with the expected role, else an ``acceptance-<key>`` user created here."""
        if key in self._personas:
            return self._personas[key]
        email, role = PERSONAS[key]
        if self.users is None:
            skip("no user directory in this run")
        api = self.users.api(email)
        if api is not None and getattr(api, "role", role) == role:
            self.persona_source[key] = f"demo account {email}"
        else:
            api = self._acceptance_user(key, role)
        st, me = api.get("/auth/me")
        user = me.get("user") if isinstance(me, dict) else None
        expect(st == 200 and isinstance(user, dict), f"GET /auth/me as {key} => {st} {short(me)}")
        api.user_id = str(user["id"])
        api.tenant_id = str(user.get("tenant_id") or "")
        self._personas[key] = api
        return api

    def _acceptance_user(self, key: str, role: str) -> Any:
        email = f"{PREFIX}{key}@truenorth.test"
        rows = items(ok(self.admin.get("/users"), what="GET /users"))
        row = next((u for u in rows if str(u.get("email", "")).lower() == email), None)
        if row is None:
            row = ok(
                self.admin.post("/users", json={"email": email, "display_name": f"Acceptance {key}", "role": role}),
                201,
                what=f"POST /users ({email})",
            )
        elif not row.get("is_active", True) or row.get("role") != role:
            # PATCH /users/{id} takes its fields as query parameters (the contract, UserUpdateIn
            # as a dependency); a JSON body is ignored, so a deactivated suite user stayed off.
            ok(self.admin.patch(f"/users/{row['id']}", params={"is_active": "true", "role": role}), what="PATCH /users")
        uid = row["id"]
        self.on_cleanup(f"user {email}", lambda: _left(self.admin.delete(f"/users/{uid}"), 204, 404))
        self.persona_source[key] = f"{email} (no demo account with role {role}; created by this suite)"
        api = self.users.api(email)
        expect(api is not None, f"{email} was created but cannot be signed in as")
        return api


def _left(resp: tuple[int, Any], *fine: int) -> str:
    """Cleanup outcome: '' when the status is one of ``fine``, else what was left."""
    status, body = resp
    return "" if status in fine else f"{status} {short(body, 120)}"


def listing(api: Any, path: str, params: dict | None = None) -> list:
    st, body = api.get(path, params=params) if params else api.get(path)
    if st == 422 and params:  # an endpoint without paging parameters
        st, body = api.get(path)
    return items(ok((st, body), what=f"GET {path}"))


def find_named(api: Any, path: str, name: str, field: str = "name") -> dict | None:
    return next((row for row in listing(api, path, {"limit": 200}) if row.get(field) == name), None)


def all_courses(api: Any) -> list[dict]:
    out, offset = [], 0
    while True:
        st, body = api.get("/courses", params={"limit": 200, "offset": offset})
        rows = items(ok((st, body), what="GET /courses"))
        out += rows
        total = body.get("total") if isinstance(body, dict) else None
        offset += len(rows)
        if not rows or total is None or offset >= total:
            return out


def catalogue_course(api: Any, code: str) -> dict | None:
    return next((c for c in all_courses(api) if str(c.get("name", "")).startswith(f"{code} — ")), None)


# ── shared fixtures ─────────────────────────────────────────────────────


def fx_template(ctx: Ctx) -> str:
    def make() -> str:
        row = find_named(ctx.admin, "/templates", TEMPLATE_NAME)
        if row is None:
            row = ok(
                ctx.admin.post("/templates", json={"name": TEMPLATE_NAME, "yaml": TEMPLATE_YAML, "is_public": False}),
                200,
                201,
                what="POST /templates",
            )
        return str(row["id"])

    return ctx.memo("the acceptance template", make)


def fx_range(ctx: Ctx, name: str = PREFIX + "range") -> dict:
    """A range kept between runs (exercises keep theirs as history); never provisioned."""

    def make() -> dict:
        row = find_named(ctx.admin, "/ranges", name)
        if row is None or row.get("state") not in ("created",):
            row = ok(
                ctx.admin.post(
                    "/ranges",
                    json={"name": name if row is None else f"{name}-{ctx.tag}", "template_id": fx_template(ctx)},
                ),
                200,
                201,
                what="POST /ranges",
            )
        return ok(ctx.admin.get(f"/ranges/{row['id']}"), what="GET /ranges/{id}")

    return ctx.memo(f"range {name}", make)


def fx_scenario(ctx: Ctx) -> str:
    def make() -> str:
        row = find_named(ctx.admin, "/scenarios", SCENARIO_NAME)
        if row is None:
            row = ok(
                ctx.admin.post("/scenarios", json={"name": SCENARIO_NAME, "yaml": SCENARIO_YAML, "is_public": False}),
                200,
                201,
                what="POST /scenarios",
            )
        elif (row.get("yaml") or "").strip() != SCENARIO_YAML.strip():
            ok(ctx.admin.put(f"/scenarios/{row['id']}", json={"yaml": SCENARIO_YAML}), what="PUT /scenarios/{id}")
        return str(row["id"])

    return ctx.memo("the acceptance scenario", make)


def fx_exercise(ctx: Ctx) -> dict:
    """``acceptance-exercise-<tag>`` on the acceptance range, created and started. Exercises
    are history (no delete); cleanup completes it if a check left it running."""

    def make() -> dict:
        rng = fx_range(ctx)
        name = f"{PREFIX}exercise-{ctx.tag}"
        ex = ok(
            ctx.admin.post("/exercises", json={"name": name, "range_id": rng["id"], "scenario_id": fx_scenario(ctx)}),
            201,
            what="POST /exercises",
        )
        eid = str(ex["id"])
        ctx.on_cleanup(f"exercise {name}", lambda: _complete_if_live(ctx, eid))
        made = {
            o["ref_id"]: (o["objective_type"], o["points"])
            for o in ok(ctx.admin.get(f"/exercises/{eid}/objectives"), what="GET objectives")
        }
        expect(made == OBJECTIVES, f"objectives made from the scenario: {made}, expected {OBJECTIVES}")
        expect(ex.get("state") == "pending" and ex.get("max_score") == 50, f"new exercise: {short(ex)}")
        started = ok(ctx.admin.post(f"/exercises/{eid}/start"), 200, 202, what="POST /exercises/{id}/start")
        expect(started.get("state") == "running", f"after start: {started.get('state')}")
        return {"id": eid, "name": name, "range_id": rng["id"], "backend": rng.get("provisioner_backend") or ""}

    return ctx.memo("the acceptance exercise", make)


def _complete_if_live(ctx: Ctx, eid: str) -> str:
    st, ex = ctx.admin.get(f"/exercises/{eid}")
    if st == 200 and ex.get("state") in ("running", "paused"):
        return _left(ctx.admin.post(f"/exercises/{eid}/complete"), 200, 202)
    return ""


def fx_course(ctx: Ctx) -> dict:
    """The LMS course (--lms-course, default C103) with a quizzed module, and the participant
    Student enrolled on it."""

    def make() -> dict:
        code = ctx.opt.get("lms_course") or "C103"
        course = catalogue_course(ctx.admin, code)
        expect(course is not None, f"no catalogue course {code} (load the shipped content)")
        outline = ok(ctx.admin.get(f"/courses/{course['id']}/outline"), what="GET outline")
        quizzed = [m for m in outline.get("modules", []) if m.get("quiz")]
        expect(quizzed, f"{code} has no module with a quiz")
        student = ctx.persona("student")
        st, body = ctx.admin.post(
            f"/courses/{course['id']}/enroll", json={"user_id": student.user_id, "course_id": course["id"]}
        )
        # 200: already enrolled (idempotent, #159); 409: what the API said before.
        expect(st in (200, 201, 409), f"enrol the Student on {code} => {st} {short(body)}")
        return {
            "id": str(course["id"]),
            "code": code,
            "name": course["name"],
            "outline": outline,
            "module": quizzed[-1],
            "newly_enrolled": st == 201,
        }

    return ctx.memo(f"the LMS course {ctx.opt.get('lms_course') or 'C103'}", make)


def fx_roster(ctx: Ctx) -> dict:
    """The exercise's roster: a draft booking tying it to the LMS course. A draft holds
    nothing and the scheduler clock never acts on it, but it names the course whose active
    Students take part (app/exercise_participants.py).

    A booking seats its course only while it overlaps the exercise's run (started, to
    completed or now). The exercise is started before this is made, so a booking from
    five minutes ago to two hours ahead overlaps it however long ago it started (its end
    is after the start, its start before now). A booking weeks ahead seats nobody."""

    def make() -> dict:
        ex, course = fx_exercise(ctx), fx_course(ctx)
        now = datetime.now(UTC).replace(microsecond=0)
        start, end = now - timedelta(minutes=5), now + timedelta(hours=2)
        evt = ok(
            ctx.admin.post(
                "/schedule/events",
                json={
                    "name": f"{PREFIX}roster-{ctx.tag}",
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "exercise_id": ex["id"],
                    "course_id": course["id"],
                    "draft": True,
                },
            ),
            201,
            what="POST /schedule/events (draft roster)",
        )
        eid = evt["id"]
        ctx.on_cleanup(
            f"roster booking {eid}", lambda: _left(ctx.admin.delete(f"/schedule/events/{eid}"), 200, 204, 404)
        )
        return evt

    return ctx.memo("the exercise roster booking", make)


def ingest(ctx: Ctx, range_id: str, events: list[dict], marker: str) -> None:
    body = ok(ctx.admin.post(f"/telemetry/{range_id}/events", json=events), 202, what="POST telemetry events")
    expect(body.get("accepted") == len(events), f"accepted {body.get('accepted')} of {len(events)}")

    def found() -> bool:
        st, res = ctx.admin.get(f"/telemetry/{range_id}/search", params={"q": f"marker:{marker}", "size": 50})
        return st == 200 and len(items(res)) >= len(events)

    ctx.poll(found, f"{len(events)} events with marker {marker} to be searchable", timeout=60)


# ══ infra ═══════════════════════════════════════════════════════════════

DEEP_COMPONENTS = {
    "database": None,
    "redis": None,
    "opensearch": None,
    "minio": "minio",
    "celery": None,
    "ai_orchestrator": None,
    "keycloak": "keycloak",
    "audit_export": None,  # ADR 0011: the signed daily export is current
}


def judge_components(components: list[dict], absent: set[str], required: Iterable[str]) -> tuple[list[str], list[str]]:
    """(problems, notes) for health components. Healthy passes; so does OpenSearch yellow
    (a single-node cluster cannot place replicas, so yellow is its normal state); a
    component of a facility declared absent is noted, not judged; anything else fails."""
    seen = {c["name"]: c for c in components}
    bad = [f"{n} not reported" for n in required if n not in seen]
    notes = []
    for name, c in seen.items():
        facility = DEEP_COMPONENTS.get(name)
        if facility and facility in absent:
            notes.append(f"{name} not judged (absent)")
        elif c["status"] == "healthy":
            continue
        elif name == "opensearch" and c["status"] == "degraded" and "yellow" in (c.get("message") or ""):
            notes.append("opensearch yellow (single node)")
        else:
            bad.append(f"{name} {c['status']}: {c.get('message')}")
    return bad, notes


@check("infra", "health live + ready")
def infra_ready(ctx: Ctx) -> str:
    ok(ctx.admin.get("/health/live"), what="GET /health/live")
    ready = ok(ctx.admin.get("/health/ready"), what="GET /health/ready")  # 503 when a dependency is down
    bad, notes = judge_components(ready.get("components", []), ctx.absent, ("database", "redis"))
    expect(not bad, "; ".join(bad))
    return (
        f"ready ({ready.get('status')}): "
        + ", ".join(f"{c['name']} {c['status']}" for c in ready.get("components", []))
        + (f"; {'; '.join(notes)}" if notes else "")
    )


@check("infra", "health deep: every component")
def infra_deep(ctx: Ctx) -> str:
    deep = ok(ctx.admin.get("/health/deep"), what="GET /health/deep")
    components = deep.get("components", [])
    bad, notes = judge_components(components, ctx.absent, DEEP_COMPONENTS)
    expect(not bad, "; ".join(bad))
    return f"{len(components)} components reported, none down" + (f" ({'; '.join(notes)})" if notes else "")


@check("infra", "metrics need the scrape token")
def infra_metrics(ctx: Ctx) -> str:
    ctx.require("metrics-token")
    token = os.getenv("METRICS_SCRAPE_TOKEN", "")
    expect(token, "METRICS_SCRAPE_TOKEN is not set: /metrics answers anyone who reaches the API")
    st, _ = ctx.admin.get("/metrics")
    expect(st == 401, f"GET /metrics without the token => {st}, expected 401")
    st, body = ctx.admin.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    expect(st == 200 and "# TYPE" in str(body), f"GET /metrics with the token => {st} {short(body, 80)}")
    return "401 without the token, Prometheus text with it"


def edge_get(ctx: Ctx, path: str) -> Any:
    import httpx

    ctx.require("edge")
    base = ctx.opt.get("edge_url") or "https://nginx"
    try:
        # Reachability of our own edge from inside the stack; the certificate is the
        # installer's (often an internal CA this container does not carry), so not verified.
        return httpx.get(base.rstrip("/") + path, verify=False, timeout=15, follow_redirects=False)  # noqa: S501
    except httpx.HTTPError as exc:
        raise FailedCheckError(f"{base}{path}: {type(exc).__name__}: {exc}") from exc


@check("infra", "edge: health, API, metrics refused")
def infra_edge(ctx: Ctx) -> str:
    r = edge_get(ctx, "/health")
    expect(r.status_code == 200, f"edge /health => {r.status_code}")
    r = edge_get(ctx, "/api/health")
    expect(r.status_code == 200, f"edge /api/health => {r.status_code} {short(r.text, 80)}")
    r = edge_get(ctx, "/api/metrics")
    expect(r.status_code == 404, f"edge /api/metrics => {r.status_code}, expected 404 (refused at the edge)")
    return "/health 200, /api/health 200, /api/metrics 404"


@check("infra", "backup environment present")
def infra_backup(ctx: Ctx) -> str:
    ctx.require("backup")
    cfg = Path(ctx.opt.get("host_config") or "/srv/truenorth/config")
    if not cfg.is_dir():
        skip(
            f"{cfg} is not visible in this container; mount the host's config read-only "
            "(-v /srv/truenorth/config:/hostcfg:ro --host-config /hostcfg) to check backup.env"
        )
    env = cfg / "backup.env"
    expect(env.is_file() and env.stat().st_size > 0, f"{env} is missing or empty")
    keys = {
        m.group(1) for m in re.finditer(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=", env.read_text(errors="replace"), re.M)
    }
    need = {"BACKUP_DIR"}
    expect(need <= keys, f"backup.env does not set {sorted(need - keys)}")
    return f"backup.env sets {len(keys)} variables (values not read out)"


# ══ platform-core ═══════════════════════════════════════════════════════


@check("platform-core", "users for each role")
def core_personas(ctx: Ctx) -> str:
    for key in PERSONAS:
        ctx.persona(key)
    return "; ".join(f"{k}: {v}" for k, v in ctx.persona_source.items())


@check("platform-core", "RBAC: Student and Observer refused staff routes")
def core_rbac(ctx: Ctx) -> str:
    student, observer, instructor = ctx.persona("student"), ctx.persona("observer"), ctx.persona("instructor")
    refused = [
        (student, "GET", "/audit-log", None),
        (student, "GET", "/users", None),
        (student, "POST", "/users", {"email": f"{PREFIX}nobody@truenorth.test", "display_name": "x", "role": "admin"}),
        (student, "GET", "/schedule/events", None),
        (student, "POST", "/ranges", {"name": f"{PREFIX}nope", "template_id": str(uuid.uuid4())}),
        (observer, "POST", "/ranges", {"name": f"{PREFIX}nope", "template_id": str(uuid.uuid4())}),
        (
            observer,
            "POST",
            "/exercises",
            {"name": "x", "range_id": str(uuid.uuid4()), "scenario_id": str(uuid.uuid4())},
        ),
        (instructor, "GET", "/ai-config/summary", None),
    ]
    wrong = []
    for api, method, path, body in refused:
        st, out = api.request(method, path, json=body)
        if st != 403:
            wrong.append(f"{api.role} {method} {path} => {st}")
    expect(not wrong, "not refused: " + "; ".join(wrong))
    expect(ctx.admin.get("/audit-log", params={"limit": 1})[0] == 200, "the administrator cannot read /audit-log")
    return f"{len(refused)} staff routes refused with 403; administrator allowed"


@check("platform-core", "tenant scoping")
def core_tenancy(ctx: Ctx) -> str:
    st, me = ctx.admin.get("/auth/me")
    tenant = str(((me or {}).get("user") or {}).get("tenant_id") or "")
    expect(st == 200 and tenant, f"GET /auth/me => {st} {short(me)}")
    for key in ("student", "instructor"):
        expect(ctx.persona(key).tenant_id == tenant, f"{key} is in tenant {ctx.persona(key).tenant_id}, not {tenant}")
    rng = fx_range(ctx)
    expect(str(rng.get("tenant_id")) == tenant, f"the acceptance range carries tenant {rng.get('tenant_id')}")
    for path in ("/ranges/{}", "/exercises/{}", "/scenarios/{}", "/tickets/{}"):
        st, _ = ctx.admin.get(path.format(uuid.uuid4()))
        expect(st == 404, f"GET {path} with an id that is not ours => {st}, expected 404")
    return f"tenant {tenant}: rows carry it; foreign ids are 404"


@check("platform-core", "audit log records writes")
def core_audit(ctx: Ctx) -> str:
    ex = fx_exercise(ctx)
    rows = ok(ctx.admin.get("/audit-log", params={"limit": 500}), what="GET /audit-log")
    mine = {(r["action"], r["resource_type"]) for r in rows if r.get("resource_id") == ex["id"]}
    expect({("create", "exercise"), ("start", "exercise")} <= mine, f"audit rows for {ex['id']}: {sorted(mine)}")
    return f"create and start of {ex['name']} audited"


@check("platform-core", "demo accounts linked to Keycloak")
def core_keycloak_links(ctx: Ctx) -> str:
    ctx.require("keycloak")
    password = os.getenv("KEYCLOAK_ADMIN_PASSWORD", "")
    if not password:
        skip(
            "KEYCLOAK_ADMIN_PASSWORD is not in this container's environment (pass -e KEYCLOAK_ADMIN_USER "
            "-e KEYCLOAK_ADMIN_PASSWORD to compare each demo account's subject with Keycloak's)"
        )
    from app.bootstrap_admin import _keycloak_admin_token
    from app.link_demo_accounts import find_keycloak_user

    base = os.getenv("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
    base = base if base.endswith("/auth") else base + "/auth"
    realm = os.getenv("KEYCLOAK_REALM", "truenorth")
    token = _keycloak_admin_token(base, os.getenv("KEYCLOAK_ADMIN_USER", "admin"), password)
    wrong, linked = [], 0
    for _key, (email, _role) in PERSONAS.items():
        found = ctx.users.lookup(email) if ctx.users else None
        if found is None:
            continue
        kc = find_keycloak_user(base, realm, token, email)
        if kc is None or kc["id"] != found[0].sub:
            wrong.append(f"{email}: {'no Keycloak account' if kc is None else 'subject differs'}")
        else:
            linked += 1
    expect(not wrong, "; ".join(wrong))
    if not linked:
        skip("no demo accounts on this host (tn_load_shipped_demo off)")
    return f"{linked} demo accounts match their Keycloak subjects"


# ── platform-core: S2 login banner, S3 append-only audit log and signed export ──
# (added by the 2026-10-20 amendment, #192; docs/release-notes/v1.2.0-module-matrix.md)

BANNER_MESSAGES = ("tnLoginBannerTitle", "tnLoginBanner", "tnLoginBannerAck")  # roles/tn_keycloak banner.yml
LOGIN_THEME = "truenorth"
WEB_CLIENT = "truenorth-web"
# Any well-formed S256 challenge renders the sign-in page (scripts/check_keycloak_realm.sh).
PKCE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def _keycloak_base() -> tuple[str, str]:
    base = os.getenv("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
    return (base if base.endswith("/auth") else base + "/auth"), os.getenv("KEYCLOAK_REALM", "truenorth")


def _keycloak_token(base: str) -> str:
    from app.bootstrap_admin import _keycloak_admin_token

    return _keycloak_admin_token(base, os.getenv("KEYCLOAK_ADMIN_USER", "admin"), os.getenv("KEYCLOAK_ADMIN_PASSWORD", ""))


def _http_get(url: str, token: str | None = None, params: dict | None = None) -> tuple[int, Any]:
    """(status, JSON body or text). Tests replace this."""
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = httpx.get(url, params=params, headers=headers, timeout=30, follow_redirects=False)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


def message_text(value: str) -> str:
    """A Java MessageFormat message as displayed: '' is a quote, '{' and '}' are braces."""
    return re.sub(r"'([{}])'", r"\1", value).replace("''", "'")


@check("platform-core", "login banner (AC-8) on the realm's sign-in page")
def core_login_banner(ctx: Ctx) -> str:
    """S2: the realm uses the truenorth login theme, its English localization carries the
    site's banner, and the sign-in page shows that text with a required acknowledgement."""
    import html

    ctx.require("keycloak")
    if not os.getenv("KEYCLOAK_ADMIN_PASSWORD", ""):
        skip(
            "KEYCLOAK_ADMIN_PASSWORD is not in this container's environment (pass -e KEYCLOAK_ADMIN_USER "
            "-e KEYCLOAK_ADMIN_PASSWORD to read the realm's theme and banner text)"
        )
    base, realm = _keycloak_base()
    token = _keycloak_token(base)
    st, rep = _http_get(f"{base}/admin/realms/{realm}", token)
    expect(st == 200 and isinstance(rep, dict), f"GET realm {realm} => {st} {short(rep)}")
    expect(rep.get("loginTheme") == LOGIN_THEME, f"realm {realm} loginTheme is {rep.get('loginTheme')!r}, not {LOGIN_THEME!r}")
    st, l10n = _http_get(f"{base}/admin/realms/{realm}/localization/en", token)
    expect(st == 200 and isinstance(l10n, dict), f"GET realm {realm} localization/en => {st} {short(l10n)}")
    blank = [k for k in BANNER_MESSAGES if not str(l10n.get(k) or "").strip()]
    expect(not blank, f"realm {realm} has no site text for {blank} (re-run 60-keycloak: roles/tn_keycloak banner.yml)")
    st, clients = _http_get(f"{base}/admin/realms/{realm}/clients", token, {"clientId": WEB_CLIENT})
    redirect = next((u for c in (clients if isinstance(clients, list) else []) for u in c.get("redirectUris") or []), "")
    expect(redirect, f"client {WEB_CLIENT} in realm {realm} has no redirect URI ({st})")
    st, page = _http_get(
        f"{base}/realms/{realm}/protocol/openid-connect/auth",
        params={
            "client_id": WEB_CLIENT,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": redirect.rstrip("*"),
            "code_challenge": PKCE_CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    expect(st == 200 and isinstance(page, str), f"the {realm} sign-in page => {st} {short(page, 120)}")
    expect('id="tn-banner"' in page, f"the {realm} sign-in page has no banner (id=\"tn-banner\")")
    expect(re.search(r'<input[^>]*name="tn_ack"[^>]*required', page), "no required acknowledgement box (tn_ack)")
    want = " ".join(message_text(str(l10n["tnLoginBanner"])).split())[:60]
    shown = " ".join(html.unescape(page).split())
    expect(want in shown, f"the sign-in page does not show the realm's banner text ({want!r}…)")
    return (
        f"realm {realm}: theme {LOGIN_THEME}, site banner text set, sign-in page shows it "
        f"({len(str(l10n['tnLoginBanner']))} chars) with a required acknowledgement"
    )


@check("platform-core", "audit log append-only: UPDATE and DELETE refused")
def core_audit_append_only(ctx: Ctx) -> str:
    """S3 (ADR 0011, migration b7c8d9e0f1a2): PostgreSQL itself refuses to change or delete
    an audit row outside the retention path. Tried on the newest row, in a transaction that
    is rolled back whatever happens."""
    dialect = ctx.dialect()
    if dialect != "postgresql":
        skip(f"the database is {dialect}, not PostgreSQL: the append-only triggers exist only there")
    rows = ctx.sql("SELECT id FROM audit_logs ORDER BY timestamp DESC LIMIT 1")
    expect(rows, "audit_logs is empty: nothing to try")
    rid = rows[0][0]
    upd = ctx.refuses("UPDATE audit_logs SET detail = detail WHERE id = :id", id=rid)
    expect(upd is not None, f"UPDATE of audit row {rid} was accepted (rolled back): the guard trigger is missing")
    expect("never changed" in upd, f"UPDATE refused, but not by the audit guard: {short(upd, 160)}")
    dele = ctx.refuses("DELETE FROM audit_logs WHERE id = :id", id=rid)
    expect(dele is not None, f"DELETE of audit row {rid} was accepted (rolled back): the guard trigger is missing")
    expect("deleted only by retention" in dele, f"DELETE refused, but not by the audit guard: {short(dele, 160)}")
    return "the newest audit row: UPDATE refused (never changed), DELETE refused (retention only); rolled back"


AUDIT_EXPORT_COLUMNS = "day, seq, file_name, file_sha256, manifest_sha256, prev_manifest_sha256, key_id"
NOTHING_TO_EXPORT = "nothing finished to export"


def _as_date(value: Any) -> Any:
    from datetime import date

    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).date()
    return value if isinstance(value, date) else datetime.fromisoformat(str(value)).date()


def export_chain(rows: list[tuple]) -> tuple[list[str], dict | None]:
    """(problems, head) of audit_exports rows: one chain through ``prev_manifest_sha256``
    from the newest manifest (nothing points at it) back to the first (prev null)."""
    by_sha = {r[4]: dict(zip(AUDIT_EXPORT_COLUMNS.split(", "), r, strict=True)) for r in rows}
    problems = [f"{len(rows) - len(by_sha)} exports share a manifest hash"] if len(by_sha) != len(rows) else []
    referenced = {e["prev_manifest_sha256"] for e in by_sha.values() if e["prev_manifest_sha256"]}
    problems += [f"an export points back to {s[:16]}…, which audit_exports does not hold" for s in referenced - set(by_sha)]
    heads = [e for s, e in by_sha.items() if s not in referenced]
    if len(heads) != 1:
        return problems + [f"{len(heads)} exports have no successor (expected 1)"], None
    seen, sha = set(), heads[0]["manifest_sha256"]
    while sha and sha in by_sha and sha not in seen:
        seen.add(sha)
        sha = by_sha[sha]["prev_manifest_sha256"]
    if len(seen) != len(by_sha):
        problems.append(f"the chain from the newest export reaches {len(seen)} of {len(by_sha)} exports")
    return problems, heads[0]


def verify_export(archive: Path, export: dict) -> str:
    """Check one export in the archive against its audit_exports row: the manifest's bytes
    hash to the recorded manifest_sha256, it is signed (Ed25519) by the key whose id it and
    the row carry (keys/<key_id>.pub.pem, key id = SHA-256 of the raw public key, 16 hex),
    and its file hashes to file_sha256. Independent of the worker's code (worker/audit_export.py
    writes this format). Returns the manifest's day; FailedCheckError on any mismatch."""
    import hashlib

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization

    fname = str(export["file_name"])
    mname = fname.removesuffix(".jsonl.gz") + ".manifest.json"
    try:
        mbytes = (archive / mname).read_bytes()
        sig = (archive / (mname + ".sig")).read_bytes()
        pem = (archive / "keys" / f"{export['key_id']}.pub.pem").read_bytes()
        data = (archive / fname).read_bytes()
    except OSError as exc:
        raise FailedCheckError(f"audit archive {archive}: {type(exc).__name__}: {exc}") from exc
    expect(hashlib.sha256(mbytes).hexdigest() == export["manifest_sha256"],
           f"{mname} does not hash to audit_exports.manifest_sha256")
    manifest = json.loads(mbytes)
    for key in ("file_sha256", "prev_manifest_sha256", "key_id"):
        expect(manifest.get(key) == export[key], f"{mname}: {key} differs from audit_exports")
    public = serialization.load_pem_public_key(pem)
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    expect(hashlib.sha256(raw).hexdigest()[:16] == export["key_id"], f"keys/{export['key_id']}.pub.pem is another key")
    try:
        public.verify(sig, mbytes)
    except InvalidSignature:
        raise FailedCheckError(f"{mname}: the signature does not verify with key {export['key_id']}") from None
    expect(hashlib.sha256(data).hexdigest() == export["file_sha256"], f"{fname} does not match its file_sha256")
    return str(manifest.get("day"))


@check("platform-core", "audit export: latest signed, chained, verifiable")
def core_audit_export(ctx: Ctx) -> str:
    """S3 (ADR 0011): audit_exports forms one hash chain, the newest export day is current
    (as deep health judges it), and the newest manifest in the archive verifies: its hash,
    its Ed25519 signature by the recorded key, and its file's hash. The archive is the
    worker's; mount it read-only (``--audit-archive``)."""
    rows = ctx.sql(f"SELECT {AUDIT_EXPORT_COLUMNS} FROM audit_exports")
    today = datetime.now(UTC).date()
    if not rows:
        oldest = ctx.sql("SELECT min(timestamp) FROM audit_logs")
        first = oldest[0][0] if oldest else None
        if first is None or _as_date(first) >= today - timedelta(days=1):
            skip(
                f"no audit export yet and no audit row older than yesterday ({first or 'no rows'}): "
                f"{NOTHING_TO_EXPORT} (the daily export runs at 00:45 UTC for days before yesterday's)"
            )
        raise FailedCheckError(
            f"audit rows since {_as_date(first)} but no export at all (python -m worker.audit_export_now "
            "in worker-scenario names the cause)"
        )
    problems, head = export_chain(rows)
    expect(not problems and head, "audit_exports chain broken: " + "; ".join(problems))
    latest = max(_as_date(r[0]) for r in rows)
    expect(latest >= today - timedelta(days=2), f"audit export behind: latest exported day {latest}")
    archive = Path(ctx.opt.get("audit_archive") or "/srv/truenorth/audit-archive")
    if not archive.is_dir():
        skip(
            f"{archive} is not visible in this container; mount the audit archive read-only "
            "(-v /srv/truenorth/audit-archive:/auditarchive:ro --audit-archive /auditarchive) to verify the signature"
        )
    day = verify_export(archive, head)
    return (
        f"{len(rows)} exports in one chain, latest day {latest}; newest manifest ({day}, seq {head['seq']}) "
        f"signed by key {head['key_id']} and verified with its file"
    )


# ══ web-shell ═══════════════════════════════════════════════════════════


# The Angular shell's root element: index.html's <tn-root> (AppComponent's selector). The
# contract test tests/scripts/test_staging_acceptance.py reads both and fails on drift.
SPA_ROOT_SELECTOR = "tn-root"


@check("web-shell", "SPA served through nginx")
def web_spa(ctx: Ctx) -> str:
    for path in ("/", "/dashboard", "/learning/courses"):
        r = edge_get(ctx, path)
        expect(
            r.status_code == 200 and "text/html" in r.headers.get("content-type", ""), f"edge {path} => {r.status_code}"
        )
        expect(f"<{SPA_ROOT_SELECTOR}" in r.text, f"edge {path} is not the Angular shell (no <{SPA_ROOT_SELECTOR}>)")
    return "/, /dashboard and /learning/courses serve the Angular shell"


# SPA route -> an API endpoint its page loads -> expected status per role (app/rbac.py).
ROUTE_BACKING: list[tuple[str, str, dict[str, int]]] = [
    ("/dashboard", "/auth/me", {"admin": 200, "instructor": 200, "student": 200, "observer": 200}),
    ("/dashboard", "/notifications/unread-count", {"admin": 200, "instructor": 200, "student": 200, "observer": 200}),
    ("/authoring/ranges", "/ranges", {"admin": 200, "instructor": 200}),
    ("/authoring/content", "/templates", {"admin": 200, "instructor": 200}),
    ("/authoring/scenarios", "/scenarios", {"admin": 200, "instructor": 200}),
    ("/authoring/detections", "/detection-rules", {"admin": 200, "instructor": 200}),
    ("/learning/courses", "/courses", {"admin": 200, "instructor": 200, "student": 200}),
    ("/learning/career-path", "/qsp/curriculum-map", {"admin": 200, "instructor": 200, "student": 200}),
    ("/exercises", "/exercises", {"admin": 200, "instructor": 200, "student": 200, "observer": 200}),
    ("/schedule", "/schedule/events", {"admin": 200, "instructor": 200, "observer": 200, "student": 403}),
    ("/users", "/users", {"admin": 200, "student": 403, "observer": 403}),
    ("/admin", "/audit-log", {"admin": 200, "student": 403, "observer": 403}),
    ("/infrastructure", "/hypervisors/summary", {"admin": 200, "student": 403}),
    ("/ai-orchestrator", "/ai-config/summary", {"admin": 200, "instructor": 403, "student": 403}),
    ("/integrations", "/integrations/platforms", {"admin": 200, "student": 403}),
    ("/wiki", "/wiki/spaces", {"admin": 200, "instructor": 200, "student": 200, "observer": 200}),
    ("/support", "/tickets", {"admin": 200, "instructor": 200, "student": 200}),
]


@check("web-shell", "each main route's API, by role")
def web_routes(ctx: Ctx) -> str:
    apis = {"admin": ctx.admin, **{k: ctx.persona(k) for k in ("instructor", "student", "observer")}}
    wrong, n = [], 0
    for route, path, expected in ROUTE_BACKING:
        for role, want in expected.items():
            st, body = apis[role].get(path)
            n += 1
            if st != want:
                wrong.append(f"{route} ({path}) as {role}: {st}, expected {want}")
    expect(not wrong, "; ".join(wrong))
    return f"{len(ROUTE_BACKING)} routes, {n} role checks"


# ══ content ═════════════════════════════════════════════════════════════


def _shipped(pattern: str, key: str) -> list[str]:
    sys.path.insert(0, str(SRC / "scripts"))
    from load_content import top_scalar

    out = []
    for fp in sorted(SRC.glob(pattern)):
        text = fp.read_text(encoding="utf-8")
        out.append(top_scalar(text, key) or fp.parent.name)
    return out


@check("content", "course files loaded")
def content_courses(ctx: Ctx) -> str:
    codes = _shipped("content/courses/*.yaml", "course_code")
    expect(codes, f"no course files under {SRC / 'content/courses'}")
    by_code = {}
    for c in all_courses(ctx.admin):
        by_code.setdefault(str(c.get("name", "")).split(" — ")[0], c)
    missing, empty = [], []
    for code in codes:
        course = by_code.get(code)
        if course is None:
            missing.append(code)
            continue
        outline = ok(ctx.admin.get(f"/courses/{course['id']}/outline"), what=f"GET {code} outline")
        if not outline.get("modules"):
            empty.append(code)
    expect(not missing and not empty, f"not in the catalogue: {missing}; no modules: {empty}")
    return f"{len(codes)}/{len(codes)} course files have their course and modules"


@check("content", "templates, scenarios, Sigma rules, VM images")
def content_catalogue(ctx: Ctx) -> str:
    out = []
    for label, pattern, key, path, field in (
        ("templates", "content/ranges/*/template.yaml", "name", "/templates", "name"),
        ("scenarios", "content/scenarios/*/scenario.yaml", "name", "/scenarios", "name"),
        ("Sigma rules", "content/detections/*.yaml", "title", "/detection-rules", "title"),
    ):
        shipped = set(_shipped(pattern, key))
        have = {row.get(field) for row in listing(ctx.admin, path, {"limit": 200})}
        missing = sorted(shipped - have)
        expect(not missing, f"{label} not loaded: {missing}")
        out.append(f"{len(shipped)} {label}")
    images = listing(ctx.admin, "/golden-images")
    expect(images, "the VM image catalogue is empty")
    out.append(f"{len(images)} VM images")
    return ", ".join(out)


# ══ curriculum ══════════════════════════════════════════════════════════


@check("curriculum", "QSP spine and learning paths")
def curriculum_spine(ctx: Ctx) -> str:
    quals = ok(ctx.admin.get("/qsp/qualifications"), what="GET /qsp/qualifications")
    expect(quals and all(q.get("po_count", 0) > 0 for q in quals), f"qualifications: {short(quals)}")
    q = quals[0]["qsp_code"]
    pos = ok(ctx.admin.get(f"/qsp/qualifications/{q}/objectives"), what=f"GET {q} objectives")
    expect(pos, f"{q} has no performance objectives")
    paths = listing(ctx.admin, "/learning-paths")
    expect(paths, "no learning paths")
    unpublished = [p.get("name") for p in paths if not p.get("is_published", True)]
    prog = ok(ctx.admin.get("/qsp/developmental-progression"), what="GET developmental-progression")
    expect(prog.get("progression") or prog.get("specialty_streams"), "no developmental progression")
    cmap = ok(ctx.admin.get("/qsp/curriculum-map"), what="GET /qsp/curriculum-map")
    expect(cmap.get("nodes"), "curriculum map has no nodes")
    return (
        f"{len(quals)} qualifications, {len(paths)} learning paths ({len(unpublished)} unpublished), "
        f"{len(cmap['nodes'])} map nodes"
    )


@check("curriculum", "a Student's own curriculum map")
def curriculum_student(ctx: Ctx) -> str:
    cmap = ok(ctx.persona("student").get("/qsp/curriculum-map"), what="GET /qsp/curriculum-map as Student")
    states = {o.get("progress_state") for n in cmap.get("nodes", []) for o in n.get("objectives", [])}
    expect(states and states <= {"not_started", "in_progress", "completed", "failed"}, f"progress states: {states}")
    return f"{len(cmap['nodes'])} nodes, states {sorted(states)}"


# ══ lms ═════════════════════════════════════════════════════════════════


@check("lms", "enrol, sit a quiz, progress recorded")
def lms_quiz(ctx: Ctx) -> str:
    course = fx_course(ctx)
    student = ctx.persona("student")
    module = course["module"]
    qid = module["quiz"]["id"]
    quiz = ok(ctx.admin.get(f"/quizzes/{qid}"), what="GET /quizzes/{id}")
    expect(quiz.get("is_published"), f"the {course['code']} quiz is not published")
    key = {q["id"]: q["correct"] for q in ok(ctx.admin.get(f"/quizzes/{qid}/questions"), what="GET questions")}
    attempt = ok(student.post(f"/quizzes/{qid}/attempts"), 201, what="POST attempt as Student")
    expect({q["id"] for q in attempt["questions"]} == set(key), "the attempt's questions differ from the quiz")
    expect(all("correct" not in q for q in attempt["questions"]), "the Student was shown the answers")
    result = ok(
        student.post(f"/quizzes/attempts/{attempt['attempt_id']}/submit", json={"answers": key}),
        what="submit as Student",
    )
    expect(result.get("passed") is True and result.get("pct") == 100.0, f"result: {short(result)}")
    progress = {
        p["module_id"]: p
        for p in ok(ctx.admin.get(f"/courses/{course['id']}/progress/{student.user_id}"), what="GET progress")
    }
    row = progress.get(module["id"]) or {}
    expect(row.get("status") == "completed" and row.get("score") == 100, f"module progress: {short(row)}")
    po = (module.get("delivers") or {}).get("po_code")
    if po:
        cmap = ok(student.get("/qsp/curriculum-map"), what="Student curriculum map")
        states = [o["progress_state"] for n in cmap["nodes"] for o in n["objectives"] if o["po_code"] == po]
        expect("completed" in states, f"{po} on the Student's map: {states}")
    return (
        f"{course['code']}: {'enrolled' if course['newly_enrolled'] else 'already enrolled'}, quiz passed 100%, "
        f"module completed" + (f", {po} completed on the map" if po else "")
    )


def _cmi5_release(ctx: Ctx) -> tuple[dict, dict]:
    """An accepted release with a cmi5 package, on a published course (Students see no other)."""
    for rel in listing(ctx.admin, "/course-releases"):
        if rel.get("state") != "accepted":
            continue
        st, course = ctx.admin.get(f"/courses/{rel['course_id']}")
        if st != 200 or not course.get("is_published"):
            continue
        st, s = ctx.admin.get(f"/cmi5/releases/{rel['id']}/structure")
        if st == 200 and s.get("aus"):
            return rel, s
    skip("no accepted course release with a cmi5 package on a published course on this host")
    raise AssertionError  # unreachable


@check("lms", "cmi5 launch, fetch, statements to the LRS")
def lms_cmi5(ctx: Ctx) -> str:
    from urllib.parse import parse_qs, urlsplit

    ctx.require("lrs")
    rel, _ = _cmi5_release(ctx)
    student = ctx.persona("student")
    st, body = ctx.admin.post(
        f"/courses/{rel['course_id']}/enroll", json={"user_id": student.user_id, "course_id": rel["course_id"]}
    )
    expect(st in (200, 201, 409), f"enrol on {rel.get('catalogue_code')} => {st} {short(body)}")
    s = ok(student.get(f"/cmi5/releases/{rel['id']}/structure"), what="structure as Student")
    expect(s.get("enrolled"), f"the Student is not pinned to release {rel['id']}: {short(s)}")
    free = [a["index"] for a in s["aus"] if not a["satisfied"] and not a["completed"]] or [s["aus"][0]["index"]]
    launch = ok(student.post(f"/cmi5/releases/{rel['id']}/aus/{free[0]}/launch", json={}), what="launch AU")
    q = {k: v[0] for k, v in parse_qs(urlsplit(launch["url"]).query).items()}
    fetch_path = urlsplit(q["fetch"]).path.removeprefix("/api")
    endpoint = urlsplit(q["endpoint"]).path.removeprefix("/api")
    token = ok(student.post(fetch_path), what="POST fetch").get("auth-token")
    expect(token, "the fetch URL gave no auth-token")
    again = ok(student.post(fetch_path), what="POST fetch again")
    expect(again.get("error-code") == "1", f"a second fetch answered {short(again)}; it must be one-time")
    lrs_h = {"Authorization": f"Basic {token}", "X-Experience-API-Version": "1.0.3"}
    actor = json.loads(q["actor"])
    st, data = student.get(
        f"{endpoint}activities/state",
        headers=lrs_h,
        params={
            "stateId": "LMS.LaunchData",
            "activityId": q["activityId"],
            "agent": json.dumps(actor),
            "registration": q["registration"],
        },
    )
    expect(st == 200 and isinstance(data, dict) and "contextTemplate" in data, f"LMS.LaunchData => {st} {short(data)}")
    # A conformant AU reads the learner preferences before its first statement (cmi5 11.0.0.0-3).
    st, prefs = student.get(
        f"{endpoint}agents/profile",
        headers=lrs_h,
        params={"agent": json.dumps(actor), "profileId": "cmi5LearnerPreferences"},
    )
    expect(st in (200, 404), f"cmi5LearnerPreferences => {st} {short(prefs)}")
    ids = []
    for verb in ("initialized", "terminated"):
        ctx_t = json.loads(json.dumps(data["contextTemplate"]))
        ctx_t["registration"] = q["registration"]
        ctx_t.setdefault("contextActivities", {})["category"] = [
            {"id": "https://w3id.org/xapi/cmi5/context/categories/cmi5"}
        ]
        stmt = {
            "id": str(uuid.uuid4()),
            "actor": actor,
            "verb": {"id": f"http://adlnet.gov/expapi/verbs/{verb}", "display": {"en": verb}},
            "object": {"objectType": "Activity", "id": q["activityId"]},
            "context": ctx_t,
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        if verb == "terminated":
            stmt["result"] = {"duration": "PT5S"}
        st, out = student.post(f"{endpoint}statements", json=stmt, headers=lrs_h)
        expect(st == 200, f"{verb} statement => {st} {short(out)}")
        ids.append(stmt["id"])
    stored = _lrs_has(ids[0])
    return (
        f"{rel.get('catalogue_code')} AU {free[0]}: launched, fetched once, LaunchData read, "
        f"initialized+terminated accepted" + (f"; {stored}" if stored else "")
    )


def _lrs_has(statement_id: str) -> str:
    """Read the statement back from the LRS with the API's own credential, when it has one."""
    import httpx

    url, auth = os.getenv("LRS_URL", ""), os.getenv("LRS_AUTH", "")
    if not (url and auth):
        return ""
    r = httpx.get(
        f"{url.rstrip('/')}/xapi/statements",
        params={"statementId": statement_id},
        headers={"Authorization": f"Basic {auth}", "X-Experience-API-Version": "1.0.3"},
        timeout=20,
    )
    expect(r.status_code == 200, f"the LRS does not have statement {statement_id}: {r.status_code}")
    return "statement read back from the LRS"


@check("lms", "Moodle platform reachable")
def lms_moodle(ctx: Ctx) -> str:
    ctx.require("moodle")
    platforms = [p for p in listing(ctx.admin, "/integrations/platforms") if p.get("platform_type") == "moodle"]
    if not platforms:
        skip("no Moodle platform registered for this tenant")
    out = []
    for p in platforms:
        res = ok(ctx.admin.post(f"/integrations/platforms/{p['id']}/test"), what=f"test {p.get('name')}")
        expect(res.get("reachable") is True, f"{p.get('name')}: {short(res)}")
        ok(ctx.admin.get(f"/integrations/platforms/{p['id']}/moodle-results"), what=f"{p.get('name')} results")
        out.append(f"{p.get('name')} {res.get('status_code')}")
    return "reachable: " + ", ".join(out)


MOODLE_LOOP = "Moodle loop: SSO, quiz, pull, grade on the enrolment (integration, bypasses browser)"


def _pull_now(ctx: Ctx, platform_id: str) -> dict:
    """The explicit trigger ("Pull now"), waiting out a scheduled pull that holds the claim."""

    def attempt() -> dict | None:
        st, body = ctx.admin.post(f"/integrations/platforms/{platform_id}/moodle-results/pull", json={})
        if st == 409:
            return None
        return ok((st, body), what="POST moodle-results/pull")

    return ctx.poll(attempt, "a Moodle results pull (another was running)", timeout=120, interval=5)


@check("lms", MOODLE_LOOP)
def lms_moodle_loop(ctx: Ctx) -> str:
    """M1 on an installed host, without a browser (the browser journey is CI's ``moodle``
    lane, and the click-through in docs/runbooks/moodle.md): a dedicated Student
    (``acceptance-moodle-student``) enrolled on the course is handed into Moodle by TrueNorth's
    own ticket, views a page, starts the quiz, leaves, resumes and submits it through
    Moodle's own pages over HTTP (scripts/_moodle_student.py); after a triggered pull the
    grade and completion are on that Student's enrolment, with Moodle's attempt id and time,
    and the sync is current. The participant Student's grades on the course do not go down."""
    sys.path.insert(0, str(SRC / "scripts"))
    from _moodle_student import MoodleWeb

    ctx.require("moodle")
    platforms = [
        p for p in listing(ctx.admin, "/integrations/platforms")
        if p.get("platform_type") == "moodle" and p.get("is_active", True) and p.get("lti_issuer")
    ]
    if not platforms:
        skip("no Moodle platform registered for this tenant")
    # The one app.moodle_sso hands Students to: the tenant's oldest active Moodle.
    platform = min(platforms, key=lambda p: str(p.get("created_at") or ""))
    code = ctx.opt.get("moodle_course") or ctx.opt.get("lms_course") or "C103"
    course = catalogue_course(ctx.admin, code)
    expect(course is not None, f"no catalogue course {code} (load the shipped content)")
    cid = str(course["id"])
    student = ctx.persona("moodle-student")
    st, body = ctx.admin.post(f"/courses/{cid}/enroll", json={"user_id": student.user_id, "course_id": cid})
    expect(st in (200, 201, 409), f"enrol {student.email} on {code} => {st} {short(body)}")
    outline = ok(student.get(f"/courses/{cid}/outline"), what=f"{code} outline as the Moodle Student")
    if not outline.get("moodle_available"):
        skip(f"{code} is not published to {platform.get('name')} (publish an accepted release: "
             "docs/runbooks/moodle.md section 2, or pick a course with --moodle-course)")
    key = []
    for m in outline.get("modules", []):
        if m.get("quiz"):
            key += ok(ctx.admin.get(f"/quizzes/{m['quiz']['id']}/questions"), what="quiz answer key") or []
    participant = ctx.persona("student")
    st, before = ctx.admin.get(f"/courses/{cid}/progress/{participant.user_id}")
    before = {p["module_id"]: p for p in before} if st == 200 and isinstance(before, list) else {}

    started = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=5)
    sso = ok(student.post("/integrations/moodle/sso", json={"course_id": cid}), what="POST /integrations/moodle/sso")
    web = MoodleWeb(platform["lti_issuer"], platform.get("base_url"))
    course_page = web.sign_in(sso["action"], sso["token"])
    course_url = web.url
    pages, quizzes = MoodleWeb.activities(course_page, "page"), MoodleWeb.activities(course_page, "quiz")
    expect(pages and quizzes, f"the Moodle course shows the Student {len(pages)} pages and {len(quizzes)} quizzes")
    web.get(f"/mod/page/view.php?id={pages[0]}")
    attempt = web.start_quiz(quizzes[0])
    shown, matched, html = web.answer_page(web.get(web.url), key)
    web.get(course_url)  # leaves mid-attempt
    expect(web.start_quiz(quizzes[0]) == attempt, "Continue your attempt opened a different attempt")
    html = web.get(web.url)
    for _ in range(100):
        if "/mod/quiz/summary.php" in web.url:
            break
        n, k, html = web.answer_page(html, key)
        shown, matched = shown + n, matched + k
    web.finish(html)
    ctx.sleep(7)  # local_truenorth leaves rows younger than its settle window (5 s) for the next pull

    pulled = _pull_now(ctx, platform["id"])
    mine = ok(student.get("/integrations/moodle/results", params={"course_id": cid}), what="GET moodle/results")
    sync = next((s for s in mine.get("sync", []) if s["platform_id"] == platform["id"]), None)
    expect(sync and sync["state"] == "current", f"sync after the pull: {short(sync)}")
    expect(mine.get("enrollment_id"), f"no enrolment in the results: {short(mine)}")
    grades = [r for r in mine["results"] if r["kind"] == "quiz_grade" and r["platform_id"] == platform["id"]]
    grade = next((r for r in grades if r.get("source_attempt_id") == attempt), None)
    note = ""
    if grade is None and grades and all(r.get("source_attempt_id") is None for r in grades):
        # A local_truenorth older than this release reports no attempt id: match on Moodle's time.
        grade = max(grades, key=lambda r: r.get("source_time") or "")
        expect(grade.get("source_time") and grade["source_time"] >= started.isoformat().replace("+00:00", ""),
               f"no grade from this run's attempt {attempt}: {short(grades)}")
        note = " (this Moodle's plugin reports no attempt id: matched on Moodle's time)"
    expect(grade, f"attempt {attempt}'s grade is not on the enrolment after the pull: {short(mine['results'])}")
    if matched == shown and shown:
        expect(grade["grade"] == grade["grade_max"], f"all {shown} answers keyed, but the grade is {short(grade)}")
    done = [r for r in mine["results"] if r["kind"] == "completion" and r.get("complete")]
    expect(done, f"no completed activity recorded on the enrolment: {short(mine['results'])}")

    st, after = ctx.admin.get(f"/courses/{cid}/progress/{participant.user_id}")
    after = {p["module_id"]: p for p in after} if st == 200 and isinstance(after, list) else {}
    lowered = [m for m, p in before.items() if (after.get(m, {}).get("score") or 0) < (p.get("score") or 0)
               or (p.get("status") == "completed" and after.get(m, {}).get("status") != "completed")]
    expect(not lowered, f"the participant's progress went down on modules {lowered}")
    return (
        f"{code} on {platform.get('name')}: ticket sign-in, page viewed, quiz attempt {attempt} started, left, "
        f"resumed, submitted ({matched}/{shown} answers keyed); pull applied {pulled.get('applied')}; grade "
        f"{grade['grade']:g}/{grade['grade_max']:g} at {grade['source_time']} on enrolment "
        f"{mine['enrollment_id']}{note}, {len(done)} completion(s); sync current; participant's grades kept"
    )


# ══ scenario-engine ═════════════════════════════════════════════════════


@check("scenario-engine", "shipped scenario becomes an exercise plan")
def scenario_shipped(ctx: Ctx) -> str:
    import yaml

    fp = SRC / "content/scenarios" / (ctx.opt.get("scenario") or "incident-response-drill") / "scenario.yaml"
    doc = yaml.safe_load(fp.read_text(encoding="utf-8"))
    row = find_named(ctx.admin, "/scenarios", doc["name"])
    expect(row is not None, f"shipped scenario {doc['name']!r} is not loaded")
    v = ok(ctx.admin.post("/scenarios/validate", json={"yaml": fp.read_text(encoding="utf-8")}), what="validate")
    expect(v.get("valid") is True, f"the shipped scenario does not validate: {short(v.get('errors'))}")
    want = {str(o["id"]): int(o.get("points") or 0) for o in doc.get("objectives") or []}
    # The list rows carry no scenario_id: read the candidates (the installer's demo
    # exercises, then this suite's own plan from an earlier run) one by one.
    plan_name = f"{PREFIX}plan-{doc['name']}"
    ex, how = None, ""
    for e in listing(ctx.admin, "/exercises", {"limit": 200}):
        if e["name"].startswith("Demo — ") or e["name"] == plan_name:
            full = ok(ctx.admin.get(f"/exercises/{e['id']}"), what="GET /exercises/{id}")
            if str(full.get("scenario_id")) == str(row["id"]) and (ex is None or full["name"] != plan_name):
                ex, how = full, ("the installer's demo exercise" if full["name"] != plan_name else "this suite's plan")
    if ex is None:  # a pending exercise is a plan: nothing runs until it is started; kept for the next run
        ex = ok(
            ctx.admin.post(
                "/exercises", json={"name": plan_name, "range_id": fx_range(ctx)["id"], "scenario_id": row["id"]}
            ),
            201,
            what="POST /exercises",
        )
        how = "a new pending exercise"
    got = {o["ref_id"]: o["points"] for o in ok(ctx.admin.get(f"/exercises/{ex['id']}/objectives"), what="objectives")}
    expect(got == want, f"objectives {got}, scenario says {want}")
    detail = ok(ctx.admin.get(f"/exercises/{ex['id']}/scenario-detail"), what="scenario-detail")
    expect(len(detail.get("timeline") or []) == len(doc.get("timeline") or []), "timeline length differs")
    return f"{doc['name']}: {len(want)} objectives, {len(doc.get('timeline') or [])} timeline events ({how})"


@check("scenario-engine", "start, inject fired by the worker, pause, resume")
def scenario_lifecycle(ctx: Ctx) -> str:
    ex = fx_exercise(ctx)
    eid = ex["id"]

    def injected() -> list:
        return [i for i in ok(ctx.admin.get(f"/exercises/{eid}/injects"), what="GET injects") if i.get("seq") == 0]

    recs = ctx.poll(injected, "worker-scenario to record the timeline's inject", timeout=120)
    expect(recs[0]["status"] == "fired", f"inject {recs[0]['action']}: {recs[0]['status']} {recs[0].get('detail')}")
    state = ok(ctx.admin.get(f"/exercises/{eid}"), what="GET exercise")["state"]
    if ex["backend"] == "mock":
        skip(f"range backend is mock: the run completes on its own (state {state}); pause/resume need a real backend")
    expect(state == "running", f"after its timeline a real-backend exercise stays running; it is {state}")
    paused = ok(ctx.admin.post(f"/exercises/{eid}/pause"), what="pause")
    expect(paused["state"] == "paused", f"pause: {paused['state']}")
    expect(ctx.admin.post(f"/exercises/{eid}/pause")[0] == 409, "pausing a paused exercise was not refused")
    resumed = ok(ctx.admin.post(f"/exercises/{eid}/resume"), what="resume")
    expect(resumed["state"] == "running", f"resume: {resumed['state']}")
    return f"{ex['name']} on {ex['backend']}: inject fired, paused, resumed"


# ══ telemetry ═══════════════════════════════════════════════════════════


@check("telemetry", "index an event, search it as staff")
def telemetry_staff(ctx: Ctx) -> str:
    rng = fx_range(ctx, PREFIX + "telemetry-range")  # no exercise: nobody but staff may read it
    marker = f"tm{ctx.tag.replace('-', '')}"
    ingest(
        ctx,
        rng["id"],
        [{"event_type": "acceptance.probe", "marker": marker, "n": i} for i in range(2)]
        + [{"event_type": "process_exec", "marker": marker}],
        marker,
    )
    tagged = items(
        ok(
            ctx.admin.get(f"/telemetry/{rng['id']}/search", params={"q": f"marker:{marker} mitre_technique:T1059"}),
            what="search",
        )
    )
    expect(len(tagged) == 1, f"process_exec was not tagged T1059: {len(tagged)} hits")
    seen = items(
        ok(
            ctx.persona("observer").get(f"/telemetry/{rng['id']}/search", params={"q": f"marker:{marker}"}),
            what="search as Observer",
        )
    )
    expect(len(seen) == 3, f"Observer saw {len(seen)} of 3")
    st, _ = ctx.admin.get(f"/telemetry/{rng['id']}/search", params={"q": "_index:range-*"})
    expect(st == 422, f"a query outside the grammar => {st}, expected 422")
    return "3 events indexed, MITRE-tagged, found by administrator and Observer; bad grammar 422"


@check("telemetry", "Students cannot read a range they are not on")
def telemetry_student(ctx: Ctx) -> str:
    rng = fx_range(ctx, PREFIX + "telemetry-range")
    for key in ("student", "outsider"):
        st, _ = ctx.persona(key).get(f"/telemetry/{rng['id']}/search", params={"q": "*"})
        expect(st == 404, f"{key} searching a range with no exercise or lab of theirs => {st}, expected 404")
    st, _ = ctx.persona("student").post(f"/telemetry/{rng['id']}/events", json=[{"event_type": "x"}])
    expect(st == 403, f"a Student writing telemetry => {st}, expected 403")
    return "search 404 and ingest 403 for Students"


# ══ detection ═══════════════════════════════════════════════════════════


@check("detection", "non-participant refused")
def detection_refused(ctx: Ctx) -> str:
    ex = fx_exercise(ctx)
    fx_roster(ctx)
    st, body = ctx.persona("outsider").post(
        f"/exercises/{ex['id']}/objectives/detect_beacon/detections", json={"query": "event_type:acceptance_beacon"}
    )
    expect(st == 403, f"a Student not on the roster => {st} {short(body)}, expected 403")
    return "403: not a participant (no active enrolment in the booked course)"


@check("detection", "participant's detection scored")
def detection_scored(ctx: Ctx) -> str:
    ex, course = fx_exercise(ctx), fx_course(ctx)
    fx_roster(ctx)
    state = ok(ctx.admin.get(f"/exercises/{ex['id']}"), what="GET exercise")["state"]
    if state != "running":
        if ex["backend"] == "mock":
            skip("range backend is mock: the run completed on its own, and detections need a running exercise")
        raise FailedCheckError(f"the exercise is {state}; detections are accepted while it runs")
    marker = f"dt{ctx.tag.replace('-', '')}"
    ingest(
        ctx,
        ex["range_id"],
        [{"event_type": "acceptance_beacon", "marker": marker, "url": {"domain": "c2.example"}} for _ in range(3)],
        marker,
    )
    student = ctx.persona("student")
    expect(ok(student.get(f"/exercises/{ex['id']}/detections"), what="own attempts") == [], "attempts before any")
    sub = ok(
        student.post(
            f"/exercises/{ex['id']}/objectives/detect_beacon/detections", json={"query": "event_type:acceptance_beacon"}
        ),
        201,
        what="submit as participant",
    )
    if sub.get("verdict") != "achieved" and not sub.get("events_matched"):
        if "telemetry-pipeline" in ctx.absent:
            skip(f"verdict {sub.get('verdict')}: no events in the window without the ingested-at pipeline")
        raise FailedCheckError(
            f"verdict {sub.get('verdict')}, 0 events matched: is OpenSearch's ingested-at pipeline "
            "installed (python -m telemetry.pipelines.bootstrap)? " + short(sub)
        )
    expect(sub.get("verdict") == "achieved", f"submission: {short(sub)}")
    expect(sub.get("on_target") is None, "the Student was shown on_target (answer key)")
    scored = ok(ctx.admin.get(f"/exercises/{ex['id']}"), what="GET exercise")
    expect(scored.get("total_score") == 40, f"total_score {scored.get('total_score')}, expected 40")
    obj = {o["ref_id"]: o for o in ok(ctx.admin.get(f"/exercises/{ex['id']}/objectives"), what="objectives")}
    expect(obj["detect_beacon"]["achieved"] and not obj["write_report"]["achieved"], "objective credit wrong")
    st, body = student.post(
        f"/exercises/{ex['id']}/objectives/detect_beacon/detections", json={"query": "event_type:acceptance_beacon"}
    )
    expect(st == 409, f"a second submission on an achieved objective => {st} {short(body)}")
    return (
        f"participant (enrolled on {course['code']}) credited: {sub.get('events_matched')} events, "
        "score 40/50; repeat refused 409"
    )


# ══ reporting-aar ═══════════════════════════════════════════════════════


@check("reporting-aar", "complete the exercise, generate the AAR")
def aar_generate(ctx: Ctx) -> str:
    ex = fx_exercise(ctx)
    state = ok(ctx.admin.get(f"/exercises/{ex['id']}"), what="GET exercise")["state"]
    if state != "completed":
        done = ok(ctx.admin.post(f"/exercises/{ex['id']}/complete"), 200, 202, what="complete")
        expect(done["state"] == "completed", f"complete: {done['state']}")
    expect(ctx.admin.post(f"/exercises/{ex['id']}/complete")[0] == 409, "completing twice was not refused")
    aar = ok(ctx.admin.post(f"/exercises/{ex['id']}/aar/generate"), 201, what="AAR generate")
    expect(str(aar.get("exercise_id")) == ex["id"], f"AAR for {aar.get('exercise_id')}")

    def page() -> str | None:
        r = ctx.admin.raw(f"/exercises/{ex['id']}/aar/html")  # whole page; .get() keeps 400 chars of non-JSON
        return r.text if r.status_code == 200 else None

    html = ctx.poll(page, "the AAR HTML", timeout=60)
    for needle in (ex["name"], "detect_beacon", "write_report", 'id="objectives"', 'id="timeline"'):
        expect(needle in html, f"AAR HTML lacks {needle!r}")
    return f"{ex['name']} completed; AAR generated with objectives and timeline"


@check("reporting-aar", "AAR PDF and Student access")
def aar_pdf(ctx: Ctx) -> str:
    ex = fx_exercise(ctx)
    r = ctx.admin.raw(f"/exercises/{ex['id']}/aar/pdf")
    expect(
        r.status_code == 200 and r.content.startswith(b"%PDF") and len(r.content) > 1000,
        f"AAR PDF => {r.status_code} {r.headers.get('content-type')} {len(r.content)} bytes",
    )
    st, _ = ctx.persona("student").get(f"/exercises/{ex['id']}/aar")
    expect(st == 200, f"a Student reading the AAR => {st}")
    expect(ctx.admin.get(f"/exercises/{uuid.uuid4()}/aar")[0] == 404, "a missing AAR is not 404")
    return f"PDF {len(r.content)} bytes; Student 200; missing 404"


# ══ ai-arc2 ═════════════════════════════════════════════════════════════


AI_DISABLED_REASON = "AI disabled on this host"


def ai_enabled_by_install() -> bool:
    """The installer's tn_ai_enabled, as compose gives it to the api (TN_AI_ENABLED)."""
    return os.getenv("TN_AI_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


@check("ai-arc2", "AI orchestrator answers through the API")
def ai_draft(ctx: Ctx) -> str:
    draft = {"technique": "T1059.001", "data_source": "sysmon"}
    if not ai_enabled_by_install():
        # A SKIP only when all three agree: the install says off, the orchestrator says it has
        # no backend, and the API answers the clear 503. Anything else is a FAIL, never a pass.
        deep = ok(ctx.admin.get("/health/deep"), what="GET /health/deep")
        comp = next((c for c in deep.get("components", []) if c.get("name") == "ai_orchestrator"), {})
        configured = (comp.get("details") or {}).get("ai_configured")
        expect(
            configured is False,
            f"TN_AI_ENABLED=false (tn_ai_enabled), but the orchestrator reports ai_configured={configured!r}"
            " (re-run 30-config and 50-stack-up)",
        )
        st, body = ctx.admin.post("/ai/detection-draft", json=draft)
        expect(
            st == 503 and "not configured" in short(body).lower(),
            f"AI is disabled, so POST /ai/detection-draft must answer 503 'AI is not configured'; got {st} {short(body)}",
        )
        skip(
            f"{AI_DISABLED_REASON} (tn_ai_enabled: false; the orchestrator reports ai_configured: false;"
            f" POST /ai/detection-draft => 503 {short(body, 80)})",
            True,
        )
    st, body = ctx.admin.post("/ai/detection-draft", json=draft)
    expect(st == 200, f"POST /ai/detection-draft => {st} {short(body)}")
    text = longest_text(body)
    expect(len(text) >= 20, f"the orchestrator returned no text: {short(body)}")
    return f"{len(text)} characters of draft ({short(text, 60)})"


@check("ai-arc2", "ARC² Course Studio enabled")
def arc2_status(ctx: Ctx) -> str:
    ctx.require("arc2")
    st = ok(ctx.persona("instructor").get("/arc2/status"), what="GET /arc2/status")
    expect(st.get("enabled") is True, f"ARC² is off: {st.get('reason')}")
    runs = ctx.admin.get("/arc2/runs")
    expect(runs[0] == 200, f"GET /arc2/runs => {runs[0]} {short(runs[1])}")
    return f"enabled; {len(items(runs[1]))} runs listed"


# ══ ranges-labs ═════════════════════════════════════════════════════════


@check("ranges-labs", "template validation")
def ranges_validate(ctx: Ctx) -> str:
    good = ok(ctx.admin.post("/templates/validate", json={"yaml": TEMPLATE_YAML}), what="validate")
    expect(good.get("valid") is True, f"the acceptance template does not validate: {short(good.get('errors'))}")
    bad = ok(ctx.admin.post("/templates/validate", json={"yaml": "name: x\nnodes: 7\n"}), what="validate bad")
    expect(bad.get("valid") is False and bad.get("errors"), f"a broken template validated: {short(bad)}")
    return "valid template accepted; broken one refused with errors"


@check("ranges-labs", "range create, read, documents, delete")
def ranges_crud(ctx: Ctx) -> str:
    name = f"{PREFIX}range-{ctx.tag}"
    rng = ok(ctx.admin.post("/ranges", json={"name": name, "template_id": fx_template(ctx)}), 201, what="POST /ranges")
    rid = rng["id"]
    ctx.on_cleanup(f"range {name}", lambda: _left(ctx.admin.delete(f"/ranges/{rid}"), 204, 404))
    expect(rng["state"] == "created" and rng.get("provisioner_backend"), f"new range: {short(rng)}")
    expect(ctx.admin.post("/ranges", json={"name": "x"})[0] == 422, "a range without a template was accepted")
    docs = ""
    if "minio" not in ctx.absent:
        up = ok(
            ctx.admin.post(
                f"/ranges/{rid}/documents",
                files=[("files", ("acceptance.txt", b"acceptance document\n", "text/plain"))],
            ),
            201,
            what="upload a range document (MinIO)",
        )
        doc_id = up[0]["id"]
        st, _ = ctx.admin.get(f"/ranges/{rid}/documents/{doc_id}")
        expect(st == 200, f"read the document back => {st}")
        ok(ctx.admin.delete(f"/ranges/{rid}/documents/{doc_id}"), 204, what="delete the document")
        docs = ", document stored and read back (MinIO)"
    ok(ctx.admin.delete(f"/ranges/{rid}"), 204, what="DELETE /ranges/{id}")
    expect(ctx.admin.get(f"/ranges/{rid}")[0] == 404, "the deleted range still reads")
    return f"backend {rng['provisioner_backend']}, created and deleted{docs}"


@check("ranges-labs", "lab session refusals")
def labs_refused(ctx: Ctx) -> str:
    course = fx_course(ctx)
    st, body = ctx.persona("outsider").post("/lab-sessions", json={"course_id": course["id"], "activity_id": "mod_001"})
    expect(st == 403, f"a lab on a course the Student is not enrolled on => {st} {short(body)}, expected 403")
    ok(ctx.admin.get("/lab-sessions", params={"all_students": "true"}), what="staff list of lab sessions")
    return "unenrolled Student refused 403; staff list answers"


@check("ranges-labs", "lab build on the hypervisor")
def labs_build(ctx: Ctx) -> str:
    skip(
        "building a lab clones VMs from a content-library template and waits for VMware Tools: it needs a "
        "writable, real vCenter (tests/integration/test_lab_sessions_vsphere.py), not the staging simulator",
        True,
    )


# ── ranges-labs: ADR 0012 range credentials (2026-10-20 amendment, #192) ──

CREDENTIALS_TEMPLATE = PREFIX + "credentials-template"
# A typed-in account password, as a template author writes one: a scenario account's (ADR
# 0013 §3: the only kind allowed). Not a secret: it is in this file. What the check proves is
# that the platform never stores it as typed and shows it only through an audited reveal.
CREDENTIALS_LITERAL = "acceptance-typed-in-7Qx!not-a-secret"
CREDENTIALS_ACCOUNTS = {"acceptance-analyst": "declared literal", "acceptance-admin": "generated"}
CREDENTIALS_TEMPLATE_YAML = (
    f"name: {CREDENTIALS_TEMPLATE}\n"
    "description: Post-install acceptance (scripts/staging_acceptance.py); declared accounts; never provisioned.\n"
    "environment: enterprise\n"
    "nodes:\n"
    "  - id: ws01\n    role: workstation\n    os: ubuntu22\n    vcpu: 1\n    ram_mb: 512\n    disk_gb: 8\n"
    "accounts:\n"
    f"  - {{machine: ws01, username: acceptance-analyst, password: \"{CREDENTIALS_LITERAL}\", role: user,"
    " scenario: true, student_visible: true, notes: acceptance}\n"
    "  - {machine: ws01, username: acceptance-admin, generate: true, role: admin, student_visible: false}\n"
)


def fx_credentials_range(ctx: Ctx) -> dict:
    """A range (``acceptance-credentials-<tag>``, removed afterwards, never provisioned) from
    a template declaring one typed-in and one generated account (kept and reused by name,
    like ``acceptance-template``). Creating it syncs its credential rows and writes its page
    in the Ranges wiki space (ADR 0012 §12-14)."""

    def make() -> dict:
        tpl = find_named(ctx.admin, "/templates", CREDENTIALS_TEMPLATE)
        if tpl is None:
            tpl = ok(
                ctx.admin.post(
                    "/templates", json={"name": CREDENTIALS_TEMPLATE, "yaml": CREDENTIALS_TEMPLATE_YAML, "is_public": False}
                ),
                200,
                201,
                what="POST /templates (declared accounts)",
            )
        name = f"{PREFIX}credentials-{ctx.tag}"
        rng = ok(ctx.admin.post("/ranges", json={"name": name, "template_id": tpl["id"]}), 201, what="POST /ranges")
        rid = str(rng["id"])
        ctx.on_cleanup(f"range {name}", lambda: _left(ctx.admin.delete(f"/ranges/{rid}"), 204, 404))
        return {"id": rid, "name": name, "template_id": str(tpl["id"])}

    return ctx.memo("the credentials range", make)


def _sealed(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("tnsec:")


@check("ranges-labs", "range credentials: staff read, Student 404, sealed at rest")
def ranges_credentials(ctx: Ctx) -> str:
    """ADR 0012 §12-13, ADR 0013 §3: staff list every account without passwords and reveal
    each one on its own, each reveal audited; Students who do not take part in the range get
    404 and observers 403; a Student reading the template sees no password; and PostgreSQL
    holds neither password as typed or generated (the stored template and the credential
    rows are sealed with the range-credential key)."""
    rng = fx_credentials_range(ctx)
    rid = rng["id"]
    staff = {}
    for who, api in (("administrator", ctx.admin), ("instructor", ctx.persona("instructor"))):
        body = ok(api.get(f"/ranges/{rid}/credentials"), what=f"GET /ranges/{{id}}/credentials as {who}")
        expect(body.get("scope") == "all", f"{who} read scope {body.get('scope')!r}, expected 'all'")
        listed = body.get("accounts", [])
        expect(not any("password" in a for a in listed), f"the list shows {who} passwords (ADR 0013: reveal only)")
        staff[who] = {a["username"]: a for a in listed if a.get("source", "declared") == "declared"}
    got = staff["administrator"]
    expect(set(got) == set(CREDENTIALS_ACCOUNTS), f"accounts {sorted(got)}, the template declares {sorted(CREDENTIALS_ACCOUNTS)}")

    def reveal(username: str) -> str:
        body = ok(ctx.admin.post(f"/ranges/{rid}/credentials/{got[username]['id']}/reveal"),
                  what=f"reveal {username}")
        return str(body.get("password") or "")

    expect(reveal("acceptance-analyst") == CREDENTIALS_LITERAL, "the typed-in account's password differs")
    generated = reveal("acceptance-admin")
    expect(got["acceptance-admin"].get("generated") is True and len(generated) >= 12, "no generated password")
    expect(staff["instructor"].keys() == got.keys(), f"the instructor reads {sorted(staff['instructor'])}")
    # Never built: no account may claim to be set on a machine (applied / setup_status, #216).
    claimed = [u for u, a in got.items() if a.get("applied") or a.get("setup_status", "not_set") != "not_set"]
    expect(not claimed, f"accounts of a range never built claim to be set: {claimed}")
    for key in ("student", "outsider"):
        st, body = ctx.persona(key).get(f"/ranges/{rid}/credentials")
        expect(st == 404, f"a Student ({key}) not taking part in the range => {st} {short(body)}, expected 404")
    st, body = ctx.persona("observer").get(f"/ranges/{rid}/credentials")
    expect(st == 403, f"an Observer => {st} {short(body)}, expected 403")
    st, tpl = ctx.persona("student").get(f"/templates/{rng['template_id']}")
    expect(st == 200 and CREDENTIALS_LITERAL not in str(tpl) and not _sealed_in(tpl),
           f"a Student reading the template => {st}: the password (or its seal) is shown")

    stored = ctx.sql("SELECT yaml FROM templates WHERE id = :id", id=uuid.UUID(rng["template_id"]))
    expect(stored, "the template row is not in the database")
    expect(CREDENTIALS_LITERAL not in stored[0][0] and "tnsec:rt1:" in stored[0][0],
           "templates.yaml holds the typed-in password as typed (not sealed)")
    rows = ctx.sql("SELECT password FROM range_credentials WHERE range_id = :id", id=uuid.UUID(rid))
    # The declared accounts, and the implicit staff login(s) of the machine (ADR 0013 §3).
    expect(len(rows) >= len(CREDENTIALS_ACCOUNTS), f"{len(rows)} range_credentials rows for the range")
    # Rows are sealed with the row key bound to tenant/range/credential (tnsec:rk1:); the
    # legacy Fernet seal (tnsec:rc1:) only survives until the ADR 0013 §3 migration runs.
    plain = [r for r in rows if not str(r[0]).startswith("tnsec:rk1:") or CREDENTIALS_LITERAL in r[0]
             or generated in r[0]]
    expect(not plain, f"{len(plain)} range_credentials passwords stored unsealed (or not with the range key)")
    audited = [
        r for r in ok(ctx.admin.get("/audit-log", params={"limit": 500}), what="GET /audit-log")
        if r.get("action") == "range_credential_reveal" and r.get("resource_id") == rid
    ]
    expect(len(audited) >= 2, f"{len(audited)} range_credential_reveal audit rows for the range, expected one per reveal")
    expect(all(CREDENTIALS_LITERAL not in str(r) and generated not in str(r) for r in audited), "an audit row holds a password")
    return (
        f"{rng['name']}: administrator and instructor list 2 declared accounts without passwords, the 2 reveals "
        f"give the typed-in and the generated one; Students 404, Observer 403, the template shows a Student no "
        f"password; stored sealed (templates, range_credentials); {len(audited)} reveals audited without passwords"
    )


def _sealed_in(body: Any) -> bool:
    return "tnsec:" in json.dumps(body, default=str)


# ══ provisioners ════════════════════════════════════════════════════════


@check("provisioners", "vSphere connection test and discovery")
def provisioners_vsphere(ctx: Ctx) -> str:
    ctx.require("vsphere")
    conns = [c for c in listing(ctx.admin, "/hypervisors/connections") if c.get("hypervisor_type") == "vsphere"]
    made = False
    if not conns:
        url, user, pw = (
            ctx.opt.get("vsphere_url"),
            ctx.opt.get("vsphere_user"),
            os.getenv("ACCEPTANCE_VSPHERE_PASSWORD"),
        )
        if not (url and user and pw):
            skip(
                "no vSphere connection registered; register one, or pass --vsphere-url/--vsphere-user with "
                "ACCEPTANCE_VSPHERE_PASSWORD in the environment"
            )
        from urllib.parse import urlsplit

        u = urlsplit(url if "://" in url else f"https://{url}")
        conn = ok(
            ctx.admin.post(
                "/hypervisors/connections",
                json={
                    "name": f"{PREFIX}vcenter-{ctx.tag}",
                    "hypervisor_type": "vsphere",
                    "host": u.hostname,
                    "port": u.port or 443,
                    "username": user,
                    "password": pw,
                    "verify_ssl": not ctx.opt.get("vsphere_insecure"),
                },
            ),
            201,
            what="POST /hypervisors/connections",
        )
        ctx.on_cleanup(
            "vSphere connection", lambda: _left(ctx.admin.delete(f"/hypervisors/connections/{conn['id']}"), 204, 404)
        )
        conns, made = [conn], True
    conn = next((c for c in conns if c.get("is_primary")), conns[0])
    res = ok(ctx.admin.post(f"/hypervisors/connections/{conn['id']}/test"), what="connection test")
    expect(res.get("success") is True, f"{conn['name']}: {res.get('message')}")
    disc = ok(ctx.admin.post(f"/hypervisors/connections/{conn['id']}/discover"), what="discover")
    nodes = ok(ctx.admin.get(f"/hypervisors/connections/{conn['id']}/nodes"), what="nodes")
    expect(nodes, f"discovery found no hosts: {short(disc)}")
    st, _ = ctx.persona("instructor").post(f"/hypervisors/connections/{conn['id']}/test")
    expect(st == 403, f"an instructor testing the connection => {st}, expected 403")
    return (
        f"{conn['name']}{' (created here)' if made else ''}: {res.get('version')}, "
        f"{len(nodes)} hosts discovered; instructor refused"
    )


@check("provisioners", "inventory summary")
def provisioners_summary(ctx: Ctx) -> str:
    s = ok(ctx.admin.get("/hypervisors/summary"), what="GET /hypervisors/summary")
    for key in ("total_connections", "total_nodes", "online_nodes"):
        expect(key in s, f"summary lacks {key}")
    return f"{s['total_connections']} connections, {s['online_nodes']}/{s['total_nodes']} hosts online"


# ── provisioners: ADR 0012 host-local template copies, Security Onion (2026-10-20 amendment, #192) ──

NEEDS_VCENTER = "needs vCenter"


def template_copies_module() -> Any:
    """worker/provisioners/template_copies.py, loaded by path: pure functions, no vendor SDK,
    and not the worker package (whose __init__ the api image cannot import)."""
    import importlib.util

    name = "tn_acceptance_template_copies"
    if name in sys.modules:
        return sys.modules[name]
    path = SRC / "control-plane/worker/worker/provisioners/template_copies.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # its dataclass looks its module up
    spec.loader.exec_module(mod)
    return mod


def replicate_name(base: str, host_name: str, date: str) -> str:
    """The copy name ``infra/vsphere/packer/build.sh replicate`` gives a host: the host name up
    to its first dot (``short="${host%%.*}"``), as written; ``<id>@<short>-<yyyymmdd>``."""
    return f"{base}@{str(host_name).split('.', 1)[0]}-{date}"


def copy_resolution_problems(tc: Any, hosts: list[str], base: str = "acceptance-base") -> list[str]:
    """For each host, what the provisioner and the prune make of a promoted build whose copy
    is named the way build.sh replicate names it on that host (ADR 0013 §2): the copy name's
    host is the key the provisioner looks the host up by (``host_short_name``), and pruning
    by the build records keeps that copy and deletes a newer one no record names."""
    problems = []
    for host in hosts:
        copy, newer = replicate_name(base, host, "20261001"), replicate_name(base, host, "20261019")
        parsed, key = tc.parse(copy), tc.host_short_name(host)
        if parsed is None or parsed.host != key:
            problems.append(f"host {host!r}: build.sh names its copy {copy!r}, the provisioner looks the host up "
                            f"as {key!r}")
            continue
        policy = tc.PrunePolicy(protected=frozenset({copy}), managed=frozenset({base}),
                                builds=frozenset({("acceptance-build", "0" * 64)}))
        if tc.prunable({base: "", copy: "", newer: ""}, policy) != ([newer], [copy]):
            problems.append(f"host {host!r}: pruning would not keep the promoted copy and drop the unrecorded one")
    return problems


@check("provisioners", "host-local template copies resolve per discovered host")
def provisioners_template_copies(ctx: Ctx) -> str:
    """ADR 0012 decision 6 and ADR 0013 §2, against the configured vCenter's inventory: for
    every ESXi host its discovery found, a promoted build's copy named as ``build.sh replicate``
    names it there is the one the vSphere provisioner looks up for that host, and the prune
    keeps it while dropping a newer copy no build records. A pure check on the real host
    names; nothing is cloned or deleted."""
    if "vsphere" in ctx.absent:
        skip(f"{NEEDS_VCENTER}: vsphere declared absent on this stack (--absent): {FACILITIES['vsphere']}", True)
    conns = [c for c in listing(ctx.admin, "/hypervisors/connections") if c.get("hypervisor_type") == "vsphere"]
    if not conns:
        skip(f"{NEEDS_VCENTER}: no vSphere connection registered (the vSphere connection check creates one with --vsphere-url)")
    conn = next((c for c in conns if c.get("is_primary")), conns[0])
    nodes = ok(ctx.admin.get(f"/hypervisors/connections/{conn['id']}/nodes"), what="nodes")
    if not nodes:
        ok(ctx.admin.post(f"/hypervisors/connections/{conn['id']}/discover"), what="discover")
        nodes = ok(ctx.admin.get(f"/hypervisors/connections/{conn['id']}/nodes"), what="nodes")
    hosts = sorted({str(n.get("node_name") or "") for n in nodes} - {""})
    expect(hosts, f"{conn['name']}: discovery found no hosts")
    problems = copy_resolution_problems(template_copies_module(), hosts)
    expect(not problems, "; ".join(problems))
    return (
        f"{conn['name']}: {len(hosts)} hosts ({', '.join(hosts[:4])}{'…' if len(hosts) > 4 else ''}); each resolves "
        "the promoted build's copy build.sh names for it; prune keeps it"
    )


@check("provisioners", "Security Onion per-range setup (so-setup)")
def provisioners_securityonion(ctx: Ctx) -> str:
    skip(
        "so-setup runs per range on a built securityonion VM over guest operations (ADR 0012 decision 8, "
        "worker/provisioners/vsphere_securityonion.py) and the reconcile task records its outcome "
        "(worker/setup_reconcile.py): it needs a provisioned range on a writable vCenter",
        True,
    )


# ══ scheduling ══════════════════════════════════════════════════════════


@check("scheduling", "capacity and fit check")
def sched_capacity(ctx: Ctx) -> str:
    cap = ok(ctx.admin.get("/schedule/capacity"), what="GET /schedule/capacity")
    expect(cap.get("vcpu_total", 0) > 0, f"no capacity reported: {short(cap)}")
    start = datetime.now(UTC) + timedelta(days=45)
    fit = ok(
        ctx.admin.post(
            "/schedule/check",
            json={
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(hours=1)).isoformat(),
                "template_id": fx_template(ctx),
            },
        ),
        what="POST /schedule/check",
    )
    expect(fit.get("fits") is True and fit.get("vm_count_needed") == 1, f"one small VM does not fit: {short(fit)}")
    return f"{cap['vcpu_total']} vCPU total ({cap.get('supply_source')}); one VM fits"


@check("scheduling", "book and cancel a session")
def sched_booking(ctx: Ctx) -> str:
    start = (datetime.now(UTC) + timedelta(days=45)).replace(microsecond=0)
    name = f"{PREFIX}booking-{ctx.tag}"
    evt = ok(
        ctx.admin.post(
            "/schedule/events",
            json={
                "name": name,
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(hours=1)).isoformat(),
                "vm_count": 1,
                "vcpu_total": 1,
                "ram_mb_total": 512,
                "disk_gb_total": 8,
            },
        ),
        201,
        what="POST /schedule/events",
    )
    eid = evt["id"]
    ctx.on_cleanup(f"booking {name}", lambda: _left(ctx.admin.post(f"/schedule/events/{eid}/cancel"), 200, 404, 409))
    expect(evt["state"] == "scheduled", f"booking state {evt['state']}")
    listed = ok(
        ctx.admin.get(
            "/schedule/events",
            params={
                "start": (start - timedelta(minutes=1)).isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "limit": 200,
            },
        ),
        what="GET /schedule/events",
    )
    expect(eid in {e["id"] for e in items(listed)}, "the booking is not in the calendar")
    seen = ok(ctx.persona("observer").get(f"/schedule/events/{eid}"), what="Observer reads the booking")
    expect(seen["name"] == name, "Observer saw another booking")
    expect(
        ctx.persona("student").post(
            "/schedule/events", json={"name": "x", "start_time": start.isoformat(), "end_time": start.isoformat()}
        )[0]
        == 403,
        "a Student could book",
    )
    done = ok(ctx.admin.post(f"/schedule/events/{eid}/cancel"), what="cancel")
    expect(done["state"] == "cancelled", f"after cancel: {done['state']}")
    return "booked (scheduled), listed, Observer can read, Student refused, cancelled"


# ══ noise-engine ════════════════════════════════════════════════════════


def _working_hours_offset() -> int:
    off = (10 - datetime.now(UTC).hour) % 24
    return off - 24 if off > 14 else off


@check("noise-engine", "profile, dry-run deploy, plan, stop")
def noise_flow(ctx: Ctx) -> str:
    ok(ctx.admin.get("/noise/presets"), what="GET /noise/presets")
    name = f"{PREFIX}noise-template"
    tpl = find_named(ctx.admin, "/templates", name) or ok(
        ctx.admin.post("/templates", json={"name": name, "yaml": NOISE_TEMPLATE_YAML, "is_public": False}),
        201,
        what="POST noise template",
    )
    rng = ok(
        ctx.admin.post("/ranges", json={"name": f"{PREFIX}noise-{ctx.tag}", "template_id": tpl["id"]}),
        201,
        what="POST /ranges",
    )
    rid = rng["id"]
    ctx.on_cleanup(f"noise range {rid}", lambda: _left(ctx.admin.delete(f"/ranges/{rid}"), 204, 404))
    before = ok(ctx.admin.get(f"/noise/ranges/{rid}"), what="GET noise profile")
    expect(before.get("configured") is False, f"a new range already has noise: {short(before)}")
    dry = ok(ctx.admin.post(f"/noise/ranges/{rid}/deploy", json={"dry_run": True}), what="deploy dry run")
    expect([a["node"] for a in dry.get("agents", [])] == ["lnx01"] and "task_id" not in dry, f"dry run: {short(dry)}")
    expect(ok(ctx.admin.get(f"/noise/ranges/{rid}/agents"), what="agents") == [], "a dry run registered agents")
    on = ok(
        ctx.admin.put(
            f"/noise/ranges/{rid}",
            json={
                "enabled": True,
                "paused": False,
                "level": 100,
                "utc_offset": _working_hours_offset(),
                "targets": NOISE_TARGETS,
            },
        ),
        what="enable noise",
    )
    expect(on.get("enabled") and on.get("effective_level") == 100, f"profile: {short(on)}")
    off = ok(ctx.admin.put(f"/noise/ranges/{rid}", json={"enabled": False}), what="disable noise")
    expect(off.get("enabled") is False and off.get("effective_level") == 0, f"after stop: {short(off)}")
    st, body = ctx.admin.post(f"/noise/ranges/{fx_range(ctx)['id']}/deploy", json={"dry_run": True})
    expect(st == 409, f"deploy on a template with no noise block => {st} {short(body)}, expected 409")
    st, _ = ctx.persona("student").put(f"/noise/ranges/{rid}", json={"enabled": True})
    expect(st == 403, f"a Student controlling noise => {st}, expected 403")
    return "dry run plans lnx01 without registering; enabled at 100, stopped; no-noise template 409; Student 403"


@check("noise-engine", "deploy to agents")
def noise_deploy(ctx: Ctx) -> str:
    skip(
        "a real deploy installs the agent on built range VMs (deploy_noise_agents on worker-provision); "
        "it needs a provisioned range on a writable hypervisor",
        True,
    )


# ══ greyspace ═══════════════════════════════════════════════════════════


@check("greyspace", "corpora and the template's block")
def greyspace_flow(ctx: Ctx) -> str:
    corpora = {c["tier"]: c for c in ok(ctx.admin.get("/greyspace/corpora"), what="GET /greyspace/corpora")}
    expect(corpora.get("t0", {}).get("available") is True, f"corpus t0 unavailable: {short(corpora)}")
    tpl = find_named(ctx.admin, "/templates", GREYSPACE_TEMPLATE)
    expect(tpl is not None, f"shipped template {GREYSPACE_TEMPLATE!r} is not loaded")
    rng = ok(
        ctx.admin.post("/ranges", json={"name": f"{PREFIX}greyspace-{ctx.tag}", "template_id": tpl["id"]}),
        201,
        what="POST /ranges",
    )
    rid = rng["id"]
    ctx.on_cleanup(f"greyspace range {rid}", lambda: _left(ctx.admin.delete(f"/ranges/{rid}"), 204, 404))
    before = ok(ctx.admin.get(f"/ranges/{rid}/greyspace"), what="greyspace status")
    expect(before.get("template_block"), f"the template's greyspace block is not read: {short(before)}")
    att = ok(ctx.admin.put(f"/ranges/{rid}/greyspace"), what="attach the template's block")
    expect(att.get("attached") and att.get("status") == "configured" and att.get("problems") == [], short(att))
    cfg = ok(ctx.admin.get(f"/ranges/{rid}/greyspace/config"), what="greyspace config")
    need = {"dns-root", "dns-tld", "dns-auth", "resolver", "webfarm"}
    expect(need <= set(cfg.get("services", [])), f"services: {cfg.get('services')}")
    expect(cfg.get("address_plan", {}).get("resolver"), "no resolver address planned")
    expect(
        ctx.admin.put(f"/ranges/{rid}/greyspace", json={"site_packs": ["knitting"]})[0] == 422,
        "an unknown site pack was accepted",
    )
    ok(ctx.admin.delete(f"/ranges/{rid}/greyspace"), 204, what="detach")
    return f"t0 available; block {att['block'].get('corpus_tier')} configured, {len(cfg['services'])} services planned"


# ══ wiki-helpdesk ═══════════════════════════════════════════════════════


@check("wiki-helpdesk", "wiki page, assigned ticket, assignee's inbox")
def helpdesk_flow(ctx: Ctx) -> str:
    instructor, student = ctx.persona("instructor"), ctx.persona("student")
    slug = f"{PREFIX}{ctx.tag}".lower()
    ok(ctx.admin.post("/wiki/spaces", json={"name": f"Acceptance {ctx.tag}", "slug": slug}), 201, what="POST space")
    ctx.on_cleanup(f"wiki space {slug}", lambda: _left(ctx.admin.delete(f"/wiki/spaces/{slug}"), 200, 204, 404))
    page = ok(
        ctx.admin.post(
            f"/wiki/spaces/{slug}/pages",
            json={"title": "Acceptance runbook", "body": "1. Check.\n2. Done.", "is_published": True},
        ),
        201,
        what="POST page",
    )
    ok(student.get(f"/wiki/pages/{page['id']}"), what="a Student reads the published page")
    expect(
        instructor.user_id in {a["id"] for a in ok(ctx.admin.get("/tickets/assignees"), what="assignees")},
        "the instructor is not offered as an assignee",
    )
    t = ok(
        ctx.admin.post(
            "/tickets",
            json={
                "subject": f"{PREFIX}ticket {ctx.tag}",
                "description": f"See [runbook](/wiki/pages/{page['id']}).",
                "type": "incident",
                "priority": "low",
                "assignee_id": instructor.user_id,
            },
        ),
        201,
        what="POST /tickets",
    )
    ctx.on_cleanup(f"ticket {t.get('key')}", lambda: _left(ctx.admin.delete(f"/tickets/{t['id']}"), 200, 204, 404))
    expect(t["assignee_id"] == instructor.user_id and t["status"] == "open", f"ticket: {short(t)}")
    link = f"/support/{t['id']}"
    inbox = ctx.poll(
        lambda: [
            n
            for n in ok(instructor.get("/notifications", params={"limit": 100}), what="inbox")
            if n.get("link") == link
        ],
        "the assignee's notification",
        timeout=30,
    )
    ok(instructor.post(f"/notifications/{inbox[0]['id']}/read"), what="mark read")
    theirs = ok(ctx.admin.get("/notifications", params={"limit": 100}), what="actor's inbox")
    expect(all(n.get("link") != link for n in theirs), "the actor was notified of their own assignment")
    expect(student.post(f"/notifications/{inbox[0]['id']}/read")[0] == 404, "another user's notification answered")
    return f"page published; {t['key']} assigned; in the instructor's inbox, marked read; private to them"


def _tree_pages(nodes: list[dict]) -> list[dict]:
    out = []
    for n in nodes or []:
        out += [n, *_tree_pages(n.get("children") or [])]
    return out


@check("wiki-helpdesk", "Ranges space: system-owned, staff-only, no password stored")
def wiki_ranges_space(ctx: Ctx) -> str:
    """ADR 0012 §14 (2026-10-20 amendment, #192): creating a range writes its page in the
    tenant's Ranges space, which TrueNorth owns: always staff-only (its visibility cannot be
    changed, 409), invisible to Students and Observers (404). The page names the accounts and
    holds no password or token for one, as stored or as staff read it (ADR 0013 §3)."""
    rng = fx_credentials_range(ctx)
    spaces = [s for s in listing(ctx.admin, "/wiki/spaces") if s.get("system_owned")]
    expect(len(spaces) == 1, f"{len(spaces)} system-owned wiki spaces in this tenant, expected the one Ranges space")
    space = spaces[0]
    slug = space["slug"]
    expect(space.get("visibility") == "staff", f"the Ranges space ({slug}) has visibility {space.get('visibility')!r}")
    st, body = ctx.admin.put(f"/wiki/spaces/{slug}", json={"visibility": "all"})
    expect(st == 409, f"opening the Ranges space to everyone => {st} {short(body)}, expected 409")
    tree = ok(ctx.admin.get(f"/wiki/spaces/{slug}/tree"), what="GET Ranges space tree")
    page = next((p for p in _tree_pages(tree) if p.get("title") == rng["name"]), None)
    expect(page is not None, f"no page for {rng['name']} in the Ranges space")
    staff = ok(ctx.persona("instructor").get(f"/wiki/pages/{page['id']}"), what="the range page as instructor")
    expect(f"range:{rng['id']}" in staff.get("tags", ""), f"the page is not tagged for the range: {staff.get('tags')!r}")
    expect("acceptance-analyst" in staff.get("body", ""), "the page does not name the range's accounts")
    expect(CREDENTIALS_LITERAL not in staff.get("body", ""), "the page shows staff an account password")
    for key in ("student", "observer"):
        api = ctx.persona(key)
        listed = {s.get("slug") for s in listing(api, "/wiki/spaces")}
        expect(slug not in listed, f"the Ranges space is listed to the {key}")
        for path in (f"/wiki/spaces/{slug}", f"/wiki/pages/{page['id']}"):
            st, _ = api.get(path)
            expect(st == 404, f"GET {path} as {key} => {st}, expected 404")
    stored = ctx.sql("SELECT body FROM wiki_pages WHERE id = :id", id=uuid.UUID(str(page["id"])))
    expect(stored and "{{tn-credential:" not in stored[0][0], "the stored page holds credential tokens")
    expect(CREDENTIALS_LITERAL not in stored[0][0], "wiki_pages.body holds an account password")
    return (
        f"space {slug!r} system-owned and staff-only (opening it 409); {rng['name']}'s page names the accounts "
        "and holds no password or token; Student and Observer 404"
    )


# ── main ────────────────────────────────────────────────────────────────


def _quiet_logs() -> None:
    import logging

    logging.disable(logging.CRITICAL)  # the API logs every request; the table is the output


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--admin", required=True, help="email/UPN of an existing administrator")
    ap.add_argument("--modules", help=f"comma-separated subset (default all): {', '.join(MODULES)}")
    ap.add_argument("--keep", action="store_true", help="leave the test data in place")
    ap.add_argument("--json", type=Path, help="write the report as JSON to this path")
    ap.add_argument(
        "--absent", default="", help="facilities this stack does not have; their checks SKIP: " + ", ".join(FACILITIES)
    )
    ap.add_argument("--edge-url", default="https://nginx", help="the edge, as this container reaches it")
    ap.add_argument("--host-config", default="/srv/truenorth/config", help="the host's config dir, if mounted")
    ap.add_argument(
        "--audit-archive",
        default="/srv/truenorth/audit-archive",
        help="the worker's signed audit archive (tn_audit_archive_path), if mounted read-only",
    )
    ap.add_argument("--lms-course", default="C103", help="catalogue course for the LMS checks (default C103)")
    ap.add_argument(
        "--moodle-course", help="catalogue course for the Moodle loop, published to Moodle (default: --lms-course)"
    )
    ap.add_argument("--scenario", default="incident-response-drill", help="shipped scenario directory to plan")
    ap.add_argument("--vsphere-url", help="create a temporary connection to this vCenter when none is registered")
    ap.add_argument("--vsphere-user", help="its user (password: ACCEPTANCE_VSPHERE_PASSWORD)")
    ap.add_argument("--vsphere-insecure", action="store_true", help="do not verify that vCenter's certificate")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="a SKIP not in the permitted-skips file's --skip-profile is a FAIL, as is a check missing from it",
    )
    ap.add_argument(
        "--skip-profile",
        default="staging",
        help="--strict: the permitted-skips profile(s), comma-separated (staging, ci-integration, ai-disabled)",
    )
    ap.add_argument("--permitted-skips", type=Path, default=PERMITTED_SKIPS, help="--strict: the permitted-skips file")
    ap.add_argument("--list", action="store_true", help="list the checks and exit")
    ap.add_argument("--verbose", action="store_true", help="keep the API's own request logs")
    args = ap.parse_args(argv)

    try:
        modules = select_modules(args.modules)
    except ValueError as exc:
        ap.error(str(exc))
    absent = {a.strip() for a in args.absent.split(",") if a.strip()}
    if unknown := absent - set(FACILITIES):
        ap.error(f"unknown --absent: {', '.join(sorted(unknown))}")
    if args.list:
        for m in modules:
            for name, _ in CHECKS[m]:
                print(f"{m:<16} {name}")
        return 0
    permitted, inventory = None, {}
    if args.strict:
        try:
            permitted, inventory = load_permitted(args.permitted_skips, args.skip_profile)
        except (OSError, ValueError) as exc:
            print(f"--strict: cannot use the permitted skips: {exc}", file=sys.stderr)
            return 2

    sys.path.insert(0, str(SRC / "scripts"))
    from _inprocess_api import connect_users

    users = connect_users()
    if not args.verbose:
        _quiet_logs()
    admin = users.api(args.admin)
    if admin is None or admin.role != "admin":
        print(f"administrator {args.admin} not found (or not an active admin)", file=sys.stderr)
        return 2
    ctx = Ctx(
        admin,
        users,
        keep=args.keep,
        absent=absent,
        options={
            "edge_url": args.edge_url,
            "host_config": args.host_config,
            "audit_archive": args.audit_archive,
            "lms_course": args.lms_course,
            "moodle_course": args.moodle_course,
            "scenario": args.scenario,
            "vsphere_url": args.vsphere_url,
            "vsphere_user": args.vsphere_user,
            "vsphere_insecure": args.vsphere_insecure,
        },
    )
    started = datetime.now(UTC)
    print(f"staging acceptance {ctx.tag}: {len(modules)} modules as {args.admin}")
    report = run_checks(ctx, modules, Report(strict=args.strict, permitted=permitted))
    if args.strict:
        for module, name, problem in inventory_drift(CHECKS, inventory, modules):
            report.add(module, name, FAIL, problem)
    left = ctx.cleanup()
    meta = {
        "tag": ctx.tag,
        "admin": args.admin,
        "started": started.isoformat(),
        "finished": datetime.now(UTC).isoformat(),
        "host": os.uname().nodename,
        "version": os.getenv("TN_VERSION", ""),
        "provisioner_backend": os.getenv("PROVISIONER_BACKEND", ""),
        "modules": modules,
        "absent": sorted(absent),
        "personas": ctx.persona_source,
        "cleanup_left": left,
        "kept": args.keep,
        "strict": args.strict,
        "skip_profile": args.skip_profile if args.strict else None,
    }
    print()
    print(report.table())
    for line in left:
        print(f"cleanup: {line}")
    if args.json:
        report.write_json(args.json, meta)
        print(f"report: {args.json}")
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
