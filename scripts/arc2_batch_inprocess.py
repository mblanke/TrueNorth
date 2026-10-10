"""Batch ARC² on a TEST host: every catalogue course → ARC² run → release → accepted → published.

For staging only, where the runner accepts the outline and preview gates by itself
(ARC2_AUTO_ACCEPT_GATES, docs/arc2-course-studio.md §13). Nothing here is reviewed by a
person: the releases it accepts are test content, never for Students.

Runs inside a one-off api container, in-process as the bootstrap administrator (the same
harness as scripts/load_content_inprocess.py: scripts/_inprocess_api.py). Every step goes
through the API's own routes and checks:

    POST /arc2/runs                         queue a run named after the catalogue code
    GET  /arc2/runs                         progress (phase, last job, QA)
    POST /arc2/runs/{slug}/retry            after a usage limit or a transient failure
    POST /course-releases                   the release tarball (built here, see below)
    POST /course-releases/{id}/accept       acknowledging the release's open actions
    PATCH /courses/{id}                     keep/get the TrueNorth course published
    POST /course-releases/{id}/publications the tenant's Moodle; wait for "published"

The release tarball is built in this process with the engine's own builder
(tools/arc2/release.py, from the mounted checkout) from a snapshot of the run: the run
directory is read from ARC2_RUNS_DIR (mounted with group read), regular files only, no
symlink followed, so a run cannot pull a file from outside itself into a release. Only a
run that the Studio lists for this tenant and reports as packaged is built, and
``release.readiness`` refuses anything not accepted, not QA-pass or changed since.

    docker compose run --rm --no-deps -T -w /app -e PYTHONPATH=/app \\
      -v <app checkout>:/srcapp:ro --entrypoint python api \\
      /srcapp/scripts/arc2_batch_inprocess.py --admin <upn> [--courses C1*] [--dry-run]

Idempotent: a course with a run of its name is never queued again (the run is followed
instead); uploads of the same release return the existing one; accepted releases and
published publications are left as they are. --state/--resume keep retry counts and
retry times across restarts. Courses held for a cleared author (production register) are
skipped unless --include-held. Exits 1 if any course failed (held is not failure).
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

SRC = Path(__file__).resolve().parents[1]
COURSES_DIR = SRC / "content" / "courses"
REGISTER = SRC / "content" / "catalogue" / "production_register.csv"
TOOLS = SRC / "tools"

MAX_REQUEST = 3800  # the Studio accepts 4000 characters
ACCEPT_NOTES = "auto-accepted test content (staging)"
SLUG_RE = re.compile(r"^arc2-[a-z0-9-]{1,60}$")
MAX_RUN_BYTES = 300 * 1024 * 1024

# ── courses ─────────────────────────────────────────────────────────────


@dataclass
class Course:
    code: str  # catalogue code, e.g. "C103" or "RMC C202"; also the run's name
    title: str
    yaml_name: str
    doc: dict[str, Any]
    pattern: str = ""  # T / P / R from the production register
    term: str = ""
    held: bool = False  # reserved for a cleared author


def load_register(path: Path = REGISTER) -> dict[str, dict[str, str]]:
    """Production register rows keyed by course YAML file name."""
    try:
        with path.open(newline="") as f:
            return {row["course_yaml"]: row for row in csv.DictReader(f) if row.get("course_yaml")}
    except OSError:
        return {}


def is_held(row: dict[str, str] | None) -> bool:
    """Held for a cleared author: the register's blocker or review state says so."""
    return bool(row) and "cleared author" in f"{row.get('blockers') or ''} {row.get('review_state') or ''}".lower()


def order_key(c: Course) -> tuple[int, int, str]:
    """C1xx, C2xx, C3xx, C4xx first, then RMC and IoT."""
    m = re.fullmatch(r"C(\d)(\d\d)", c.code)
    if m and not c.yaml_name.startswith(("rmc-", "iot-")):
        return (int(m.group(1)), int(m.group(2)), c.code)
    return (9, 0, c.code)


def load_courses(courses_dir: Path = COURSES_DIR, register: Path = REGISTER) -> list[Course]:
    rows = load_register(register)
    out = []
    for path in sorted(courses_dir.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        row = rows.get(path.name) or {}
        code = row.get("catalogue_code") or str(doc.get("course_code") or "").strip()
        if not code:
            continue
        out.append(
            Course(
                code=code,
                title=row.get("title") or doc.get("title") or code,
                yaml_name=path.name,
                doc=doc,
                pattern=(row.get("pattern") or "").strip().upper(),
                term=row.get("term_code") or "",
                held=is_held(row),
            )
        )
    return sorted(out, key=order_key)


def select(courses: list[Course], spec: str | None) -> list[Course]:
    """``spec``: comma-separated catalogue codes ("C103,RMC C202") or file globs ("c1*",
    "rmc-*.yaml"). Empty: every course."""
    if not spec:
        return courses
    picked: set[str] = set()
    for item in (s.strip() for s in spec.split(",")):
        if not item:
            continue
        if any(ch in item for ch in "*?[") or item.endswith(".yaml"):
            pat = item.lower() if item.endswith((".yaml", "*")) else item.lower() + "*"
            picked |= {
                c.code
                for c in courses
                if fnmatch.fnmatchcase(c.yaml_name.lower(), pat) or fnmatch.fnmatchcase(c.code.upper(), item.upper())
            }
        else:
            picked |= {c.code for c in courses if c.code.lower() == item.lower()}
    return [c for c in courses if c.code in picked]


PATTERNS = {
    "T": "Pattern: theory (T). Every module is a theory activity (lessons, cases, decisions, quizzes, "
    "written work). No lab provisioning.",
    "P": "Pattern: practical (P). Modules work from supplied logs, packet captures, code, datasets or "
    "simulators; no provisioned VM unless an objective cannot be met without running systems.",
    "R": "Pattern: range (R). Use a range activity on the vSphere range only for modules whose objectives "
    "need running systems; the other modules are theory or practical.",
}
CONSTRAINTS = (
    "Constraints: cite only keys from content/catalogue/references.yaml; mark missing sources as gaps; "
    "no offensive tradecraft; English-first. This is test content for a staging host."
)


def _clean(text: Any) -> str:
    # One line of plain text; never a run flag (the Studio refuses --slug / --resume).
    return " ".join(str(text or "").replace("--", "-").split())


def _module_line(m: dict[str, Any], level: int) -> str:
    """level 0: objectives, topics, lab; 1: objectives and a short lab; 2: objectives; 3: title."""
    parts = [f"Module {m.get('ordinal', '')}: {_clean(m.get('title'))}."]
    if level <= 2 and m.get("objectives"):
        parts.append("Objectives: " + "; ".join(_clean(o).rstrip(".") for o in m["objectives"]) + ".")
    if level == 0 and m.get("topics"):
        parts.append("Topics: " + "; ".join(_clean(t).rstrip(".") for t in m["topics"]) + ".")
    lab = _clean(m.get("lab"))
    if lab and level <= 1:
        lab = lab if level == 0 or len(lab) <= 160 else lab[:157].rstrip() + "..."
        parts.append(lab if lab.lower().startswith("lab") else f"Lab: {lab}")
    return " ".join(parts)


def request_text(course: Course, hours: int | None = None, limit: int = MAX_REQUEST) -> str:
    """The ARC² request for a catalogue course, in plain language, from its YAML. It names
    the catalogue code so stage 1 sets ``course.catalogue_code`` (a release needs it)."""
    doc = course.doc
    hrs = hours or doc.get("duration_hours") or 15
    level = _clean(doc.get("difficulty") or "foundation")
    term = f", {course.term}" if course.term else ""
    head = [
        f"Produce catalogue course {course.code} {_clean(course.title)} (catalogue_code {course.code}) "
        f"for the cyber-operator programme{term}.",
        f"Audience: college students in the cyber-operator programme. Level: {level}. Delivery: instructor-led.",
        f"Duration: {hrs} student hours in total (contact, reading, practice and assessment together).",
    ]
    if course.pattern in PATTERNS:
        head.append(PATTERNS[course.pattern])
    modules = sorted(doc.get("modules") or [], key=lambda m: m.get("ordinal") or 0)
    for level_n in range(4):
        body = [_module_line(m, level_n) for m in modules]
        text = " ".join([*head, "Modules, from the course outline:", *body, CONSTRAINTS])
        if len(text) <= limit:
            return text
    return text[: limit - 3].rstrip() + "..."


# ── run state ───────────────────────────────────────────────────────────

LIMIT_MARKERS = ("usage limit", "limit reached", "rate limit", "rate_limit", "overloaded", "credit balance")
TRANSIENT_MARKERS = (
    "api error: 5",
    "service unavailable",
    "connection error",
    "could not connect",
    "timed out",
    "the runner stopped",
)


def is_limit(error: str) -> bool:
    return any(m in (error or "").lower() for m in LIMIT_MARKERS)


def retryable(error: str) -> bool:
    e = (error or "").lower()
    return is_limit(e) or any(m in e for m in TRANSIENT_MARKERS)


def reset_hint(error: str, now: datetime) -> datetime | None:
    """When a usage limit says it resets: an epoch after ``|`` (Claude Code's
    "usage limit reached|<epoch>"), "resets 3pm" / "resets at 15:00" (UTC assumed, the
    next such time), or "try again in N minutes"."""
    e = error or ""
    m = re.search(r"\|\s*(\d{10})\b", e)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), UTC)
    m = re.search(r"(?i)\b(?:try again|retry)\s+in\s+(\d+)\s*(second|sec|minute|min|hour|hr)s?\b", e)
    if m:
        unit = m.group(2).lower()
        secs = int(m.group(1)) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
        return now + timedelta(seconds=secs)
    m = re.search(r"(?i)\bresets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", e)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)
    return None


def next_retry(error: str, attempt: int, now: datetime, base: float = 300, cap: float = 7200) -> datetime:
    """After the reset hint (plus a margin) when there is one in the future, else
    exponential backoff: base, 2×base, 4×base … capped."""
    hint = reset_hint(error, now) if is_limit(error) else None
    if hint and hint > now:
        return hint + timedelta(minutes=2)
    return now + timedelta(seconds=min(cap, base * 2 ** max(0, attempt - 1)))


def classify(run: dict[str, Any]) -> tuple[str, str]:
    """(kind, words) for a Studio run summary. Kinds: working, held, retry, failed,
    packaged, gate, paused."""
    job = run.get("job") or {}
    phase = run.get("phase") or ""
    words = run.get("phase_text") or phase
    if job.get("state") in ("queued", "running"):
        return "working", words
    if phase in ("stopped", "takeover"):  # a STOP or QA HUMAN-TAKEOVER: a person decides
        return "held", words
    if job.get("state") == "failed":
        err = str(job.get("error") or "the job failed")
        return ("retry" if retryable(err) else "failed"), err
    if phase == "packaged":
        return "packaged", words
    if phase in ("outline", "preview"):
        return "gate", words
    return "paused", words


# ── release build ───────────────────────────────────────────────────────


class ReleaseBuildError(Exception):
    """The run could not be built into a release."""


def snapshot_run(runs_root: Path, slug: str, dest: Path, max_bytes: int = MAX_RUN_BYTES) -> Path:
    """Copy a run's regular files to ``dest/slug``. No symlink is followed anywhere: not
    the run directory, not a folder in it, not a file; anything else (fifo, device) is
    skipped. The engine wrote this tree, so it is read like the Studio reads it."""
    if not SLUG_RE.match(slug):
        raise ReleaseBuildError(f"not a run name: {slug!r}")
    out = dest / slug
    budget = max_bytes
    root = os.open(runs_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            run_fd = os.open(slug, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        except OSError as exc:
            raise ReleaseBuildError(f"run {slug} is not readable here: {exc}") from exc
    finally:
        os.close(root)
    try:
        for here, _dirs, files, here_fd in os.fwalk(".", dir_fd=run_fd, follow_symlinks=False):
            target = out / here.removeprefix("./") if here != "." else out
            target.mkdir(parents=True, exist_ok=True)
            for name in files:
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=here_fd)
                except OSError:
                    continue  # a symlink (ELOOP) or unreadable: not part of a release
                with os.fdopen(fd, "rb") as f:
                    st = os.fstat(f.fileno())
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    budget -= st.st_size
                    if budget < 0:
                        raise ReleaseBuildError(f"run {slug} is larger than {max_bytes // (1024 * 1024)} MB")
                    (target / name).write_bytes(f.read(st.st_size + 1))
    finally:
        os.close(run_fd)
    return out


def build_release(runs_root: Path, slug: str, tools_dir: Path = TOOLS) -> tuple[bytes, dict[str, Any]]:
    """The release tarball for a packaged run, with the engine's own builder."""
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    from arc2 import check, release

    with tempfile.TemporaryDirectory(prefix="arc2-release-") as tmp:
        snap = snapshot_run(runs_root, slug, Path(tmp) / "runs")
        try:
            out, meta = release.build(snap, Path(tmp) / "release.tar.gz")
        except (release.ReleaseError, check.ContractError, KeyError, ValueError) as exc:
            raise ReleaseBuildError(str(exc)) from exc
        return out.read_bytes(), meta


# ── driver ──────────────────────────────────────────────────────────────

PENDING, SKIPPED, WORKING, WAITING, HELD, FAILED = "pending", "skipped", "working", "waiting_retry", "held", "failed"
ACCEPTED, PUBLISHED, PLANNED = "accepted", "published", "planned"
TERMINAL = {SKIPPED, HELD, FAILED, ACCEPTED, PUBLISHED, PLANNED}


@dataclass
class Track:
    code: str
    status: str = PENDING
    slug: str | None = None
    detail: str = ""
    attempts: int = 0
    next_retry_at: str | None = None
    stalled_since: str | None = None
    release_id: str | None = None
    version: int | None = None
    publication_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str | None) -> datetime | None:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) if s else None


class StudioUnavailableError(RuntimeError):
    pass


class Driver:
    def __init__(
        self,
        api: Any,
        courses: list[Course],
        *,
        builder: Callable[[str], tuple[bytes, dict[str, Any]]],
        concurrency: int = 1,
        publish_platform: str = "moodle",
        platform_id: str | None = None,
        include_held: bool = False,
        hours: int | None = None,
        max_attempts: int = 6,
        retry_base: float = 300,
        retry_cap: float = 7200,
        stall_timeout: float = 1800,
        publish_timeout: float = 1800,
        poll: float = 60,
        state_path: Path | None = None,
        resume: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ):
        self.api, self.builder = api, builder
        self.courses = {c.code: c for c in courses}
        self.concurrency = max(1, concurrency)
        self.publish_platform, self.platform_id = publish_platform, platform_id
        self.hours = hours
        self.max_attempts, self.retry_base, self.retry_cap = max_attempts, retry_base, retry_cap
        self.stall_timeout, self.publish_timeout, self.poll = stall_timeout, publish_timeout, poll
        self.state_path = state_path
        self.clock, self.sleep, self.log = clock, sleep, log
        self.tracks: dict[str, Track] = {}
        saved = self._load() if resume else {}
        for c in courses:
            t = Track(code=c.code)
            old = saved.get(c.code)
            if old:
                t.slug, t.attempts, t.next_retry_at = (
                    old.get("slug"),
                    int(old.get("attempts") or 0),
                    old.get("next_retry_at"),
                )
                if old.get("status") == WAITING:
                    t.status, t.detail = WAITING, old.get("detail") or ""
            if c.held and not include_held:
                t.status, t.detail = SKIPPED, "held for a cleared author (--include-held to try)"
            self.tracks[c.code] = t

    # state file
    def _load(self) -> dict[str, dict]:
        if not self.state_path:
            return {}
        try:
            return (json.loads(self.state_path.read_text()) or {}).get("courses") or {}
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        if not self.state_path:
            return
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"courses": {k: asdict(t) for k, t in self.tracks.items()}}, indent=1))
        tmp.replace(self.state_path)

    def _set(self, t: Track, status: str, detail: str = "") -> None:
        if (t.status, t.detail) != (status, detail):
            self.log(f"{_iso(self.clock())}  {t.code:<9} {status:<13} {detail[:160]}")
        t.status, t.detail = status, detail

    # runs
    def runs_by_name(self) -> dict[str, dict[str, Any]]:
        status, body = self.api.get("/arc2/runs")
        if status == 404:
            raise StudioUnavailableError("the ARC² Studio is not enabled on this API (ARC2_STUDIO_ENABLED)")
        if status != 200 or not isinstance(body, dict):
            raise StudioUnavailableError(f"GET /arc2/runs answered {status}: {str(body)[:200]}")
        by_name: dict[str, dict[str, Any]] = {}
        for run in body.get("runs") or []:  # newest first: the first of a name wins
            by_name.setdefault(str(run.get("name") or "").strip().lower(), run)
        return by_name

    def inflight(self) -> int:
        return sum(1 for t in self.tracks.values() if t.status in (WORKING, WAITING))

    def plan(self) -> list[tuple[str, str]]:
        """Dry run: what would happen, with no request other than GET /arc2/runs."""
        by_name = self.runs_by_name()
        out = []
        for code, t in self.tracks.items():
            if t.status == SKIPPED:
                out.append((code, t.detail))
                continue
            run = by_name.get(code.lower())
            if run:
                t.slug = run.get("slug")
                kind, words = classify(run)
                self._set(t, PLANNED, f"follow existing run {t.slug}: {kind} ({words})")
            else:
                text = request_text(self.courses[code], self.hours)
                self._set(t, PLANNED, f"would queue a run ({len(text)} chars)")
            out.append((code, t.detail))
        return out

    def step(self) -> bool:
        """One pass over every course. True when every course is finished."""
        by_name = self.runs_by_name()
        now = self.clock()
        for code, t in self.tracks.items():
            if t.status in TERMINAL:
                continue
            run = by_name.get(code.lower())
            if t.status == PENDING:
                if run is not None:
                    t.slug = run.get("slug")
                    self._set(t, WORKING, f"following existing run {t.slug}")
                elif self.inflight() < self.concurrency:
                    self._create(t)
                    continue
                else:
                    continue
            if t.status == WAITING:
                when = _parse(t.next_retry_at)
                if when and now < when:
                    continue
                self._retry(t)
                continue
            if run is None:
                continue  # just created; the next poll lists it
            self._advance(t, run, now)
        self.save()
        return all(t.status in TERMINAL for t in self.tracks.values())

    def _create(self, t: Track) -> None:
        text = request_text(self.courses[t.code], self.hours)
        status, body = self.api.post("/arc2/runs", json={"name": t.code, "request": text})
        if status == 201 and isinstance(body, dict):
            t.slug = body.get("slug")
            self._set(t, WORKING, f"queued run {t.slug}")
        else:
            self._set(t, FAILED, f"could not queue a run: {status} {str(body)[:200]}")

    def _retry(self, t: Track) -> None:
        status, body = self.api.post(f"/arc2/runs/{t.slug}/retry")
        if status == 200:
            t.next_retry_at = None
            self._set(t, WORKING, f"re-queued (attempt {t.attempts + 1})")
        elif status == 409:  # already working again, or nothing failed: follow the run
            t.next_retry_at = None
            self._set(t, WORKING, f"retry not needed: {str(body)[:120]}")
        else:
            self._set(t, FAILED, f"retry refused: {status} {str(body)[:200]}")

    def _advance(self, t: Track, run: dict[str, Any], now: datetime) -> None:
        kind, words = classify(run)
        if kind != "gate" and kind != "paused":
            t.stalled_since = None
        if kind == "working":
            self._set(t, WORKING, words)
        elif kind == "held":
            self._set(t, HELD, words)
        elif kind == "failed":
            self._set(t, FAILED, f"ARC² job failed: {words}")
        elif kind == "retry":
            t.attempts += 1
            if t.attempts > self.max_attempts:
                self._set(t, FAILED, f"gave up after {self.max_attempts} retries: {words}")
                return
            when = next_retry(words, t.attempts, now, self.retry_base, self.retry_cap)
            t.next_retry_at = _iso(when)
            self._set(t, WAITING, f"retry {t.attempts}/{self.max_attempts} at {t.next_retry_at}: {words}")
        elif kind == "packaged":
            self.release(t)
        else:  # waiting at a gate, or paused, with nothing queued: auto-accept should move it
            since = _parse(t.stalled_since)
            if since is None:
                t.stalled_since = _iso(now)
            elif (now - since).total_seconds() > self.stall_timeout:
                self._set(t, FAILED, f"stalled at {run.get('phase')}: {words} (is ARC2_AUTO_ACCEPT_GATES on?)")
                return
            self._set(t, WORKING, words)

    # release → accept → publish
    def release(self, t: Track) -> None:
        try:
            data, meta = self.builder(t.slug)
        except ReleaseBuildError as exc:
            self._set(t, FAILED, f"release build refused: {exc}")
            return
        files = {"file": (f"{t.slug}-{meta.get('release_digest', '')[:12]}.tar.gz", data, "application/gzip")}
        status, rel = self.api.post("/course-releases", files=files)
        if status not in (200, 201) or not isinstance(rel, dict):
            self._set(t, FAILED, f"upload refused: {status} {str(rel)[:200]}")
            return
        t.release_id, t.version = rel.get("id"), rel.get("version")
        if rel.get("state") == "candidate":
            ack = [a["id"] for a in rel.get("open_actions") or []]
            status, rel = self.api.post(
                f"/course-releases/{t.release_id}/accept", json={"acknowledge_actions": ack, "notes": ACCEPT_NOTES}
            )
            if status != 200 or not isinstance(rel, dict):
                self._set(t, FAILED, f"accept refused: {status} {str(rel)[:200]}")
                return
        if rel.get("state") != "accepted":
            self._set(t, FAILED, f"release v{t.version} is {rel.get('state')}, not accepted")
            return
        if not self._course_published(t, rel["course_id"]):
            return
        if self.publish_platform == "none":
            self._set(t, ACCEPTED, f"release v{t.version} accepted; LMS publishing skipped")
            return
        self._publish(t)

    def _course_published(self, t: Track, course_id: str) -> bool:
        status, course = self.api.get(f"/courses/{course_id}")
        if status != 200 or not isinstance(course, dict):
            self._set(t, FAILED, f"course {course_id} not readable: {status}")
            return False
        if course.get("is_published"):
            return True
        status, body = self.api.patch(f"/courses/{course_id}", json={"is_published": True})
        if status != 200:
            self._set(t, FAILED, f"could not publish the TrueNorth course: {status} {str(body)[:200]}")
            return False
        return True

    def _platform(self) -> str | None:
        if self.platform_id:
            return self.platform_id
        status, rows = self.api.get("/integrations/platforms")
        if status == 200 and isinstance(rows, list):
            moodles = [p for p in rows if p.get("platform_type") == "moodle" and p.get("is_active", True)]
            if moodles:
                self.platform_id = str(moodles[0]["id"])
        return self.platform_id

    def _publish(self, t: Track) -> None:
        platform = self._platform()
        if not platform:
            self._set(t, FAILED, f"release v{t.version} accepted; no active Moodle platform registered for this tenant")
            return
        status, pub = self.api.post(
            f"/course-releases/{t.release_id}/publications", json={"platform_id": platform}, params={"wait": "true"}
        )
        if status not in (200, 202) or not isinstance(pub, dict):
            self._set(t, FAILED, f"publish refused: {status} {str(pub)[:200]}")
            return
        t.publication_id = pub.get("id")
        deadline = self.clock() + timedelta(seconds=self.publish_timeout)
        while pub.get("state") not in ("published", "failed", "superseded"):
            if self.clock() >= deadline:
                self._set(t, FAILED, f"publication {t.publication_id} still {pub.get('state')} after the timeout")
                return
            self.sleep(10)
            status, body = self.api.get(f"/course-publications/{t.publication_id}")
            if status == 200 and isinstance(body, dict):
                pub = body
        if pub.get("state") == "published":
            self._set(t, PUBLISHED, f"release v{t.version} accepted and published to Moodle")
        else:
            self._set(t, FAILED, f"publication {pub.get('state')}: {str(pub.get('error') or '')[:200]}")

    def run(self, dry_run: bool = False) -> int:
        if dry_run:
            self.plan()
        else:
            while not self.step():
                self.sleep(self.poll)
        self.log(self.table())
        return 1 if any(t.status == FAILED for t in self.tracks.values()) else 0

    def table(self) -> str:
        lines = [f"{'course':<9} {'status':<13} {'run':<34} detail", "-" * 100]
        for t in self.tracks.values():
            lines.append(f"{t.code:<9} {t.status:<13} {(t.slug or '-'):<34} {t.detail[:120]}")
        counts: dict[str, int] = {}
        for t in self.tracks.values():
            counts[t.status] = counts.get(t.status, 0) + 1
        lines.append("-" * 100)
        lines.append("  ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--admin", required=True, help="email/UPN of an existing administrator")
    ap.add_argument("--courses", help="catalogue codes or file globs, comma-separated (default: all)")
    ap.add_argument("--concurrency", type=int, default=1, help="runs in flight at once (default 1)")
    ap.add_argument(
        "--request-from-yaml",
        action="store_true",
        default=True,
        help="build each request from the course YAML (the only source today; kept for the interface)",
    )
    ap.add_argument("--publish-platform", choices=("moodle", "none"), default="moodle")
    ap.add_argument("--platform-id", help="the Moodle platform to publish to (default: the tenant's first active one)")
    ap.add_argument("--include-held", action="store_true", help="also run courses held for a cleared author")
    ap.add_argument("--hours", type=int, help="student hours for every run (default: each YAML's duration_hours)")
    ap.add_argument(
        "--max-attempts", type=int, default=6, help="retries per course after a usage limit/transient failure"
    )
    ap.add_argument("--poll", type=float, default=60, help="seconds between polls")
    ap.add_argument("--stall-minutes", type=float, default=30, help="fail a run waiting at a gate this long")
    ap.add_argument("--state", type=Path, help="JSON state file (retry counts and times)")
    ap.add_argument("--resume", action="store_true", help="continue from --state")
    ap.add_argument("--dry-run", action="store_true", help="show what would be done; queue nothing")
    ap.add_argument("--print-requests", action="store_true", help="with --dry-run, print each request text")
    args = ap.parse_args(argv)
    if args.resume and not args.state:
        ap.error("--resume needs --state")

    courses = select(load_courses(), args.courses)
    if not courses:
        print("no course matches --courses", file=sys.stderr)
        return 2

    sys.path.insert(0, str(SRC / "scripts"))
    from _inprocess_api import connect

    connected = connect(args.admin)
    if connected is None:
        return 2
    _client, api = connected
    from app.routers.arc2_studio import runs_dir

    root = runs_dir()
    driver = Driver(
        api,
        courses,
        builder=lambda slug: build_release(root, slug),
        concurrency=args.concurrency,
        publish_platform=args.publish_platform,
        platform_id=args.platform_id,
        include_held=args.include_held,
        hours=args.hours,
        max_attempts=args.max_attempts,
        stall_timeout=args.stall_minutes * 60,
        poll=args.poll,
        state_path=args.state,
        resume=args.resume,
    )
    try:
        code = driver.run(dry_run=args.dry_run)
    except StudioUnavailableError as exc:
        print(f"arc2-batch: {exc}", file=sys.stderr)
        return 2
    if args.dry_run and args.print_requests:
        for c in courses:
            if driver.tracks[c.code].detail.startswith("would queue"):
                print(f"\n[{c.code}] {request_text(c, args.hours)}")
    return code


if __name__ == "__main__":
    sys.exit(main())
