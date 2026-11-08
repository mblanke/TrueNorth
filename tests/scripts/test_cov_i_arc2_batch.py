"""COV-I: scripts/arc2_batch_inprocess.py — the edges test_arc2_batch_inprocess.py stops short of.

Whole-slice coverage before this file: 87.8% (533 statements). Missing were: a register file
that cannot be read, a course YAML with no code, empty --courses items, the request-text
truncation floor, reset_hint's 12am and impossible clock times, snapshot_run's non-regular
files and size budget, build_release's sys.path insert, _load's fallbacks, a non-404 Studio
refusal, dry-run's skipped track, the just-created/waiting/refused-create/-retry driver
branches, every release→accept→publish refusal (upload, accept, wrong state, unreadable or
unpublishable course, the platform-id shortcut, publish refusals, the publish timeout, a
failed publication), the run() poll loop, and main()'s guardrails.

The ``api`` object is the Studio boundary the Driver is handed (the same seam
_inprocess_api.connect occupies in production); the release builder is injected, so the
real tarball build is exercised where the card's branches need it and stubbed where they
only need the driver's own decisions.
"""

from __future__ import annotations

import os
import runpy
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = __import__("importlib.util", fromlist=["spec_from_file_location"]).spec_from_file_location(
    "arc2_batch_inprocess", ROOT / "scripts" / "arc2_batch_inprocess.py"
)
batch = __import__("importlib").import_module("importlib.util").module_from_spec(_spec)
sys.modules.setdefault("arc2_batch_inprocess", batch)  # dataclasses look their module up
_spec.loader.exec_module(batch)
import _inprocess_api  # noqa: E402

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


# ── courses and request text ────────────────────────────────────────────


def test_an_unreadable_register_is_empty_and_a_codeless_course_is_skipped(tmp_path):
    assert batch.load_register(tmp_path / "absent.csv") == {}
    (tmp_path / "a.yaml").write_text("title: No code anywhere\n")
    (tmp_path / "b.yaml").write_text("course_code: X1\ntitle: Has one\n")
    courses = batch.load_courses(courses_dir=tmp_path, register=tmp_path / "absent.csv")
    assert [c.code for c in courses] == ["X1"]


def test_empty_items_in_the_courses_spec_are_skipped():
    courses = batch.load_courses()
    assert [c.code for c in batch.select(courses, " C103 , ,C104 ")] == ["C103", "C104"]


def test_a_request_that_cannot_shed_enough_detail_is_truncated_with_an_ellipsis():
    long = "y" * 200
    doc = {"modules": [{"ordinal": i, "title": f"M{i} {long}", "objectives": [long] * 3,
                        "topics": [long] * 3, "lab": long} for i in range(1, 9)]}
    c = batch.Course(code="C998", title=f"Huge {long}", yaml_name="c998.yaml", doc=doc)
    text = batch.request_text(c, limit=500)
    assert len(text) == 500 and text.endswith("...")


def test_reset_hint_midnight_am_and_impossible_clock_times():
    now = T0
    assert batch.reset_hint("usage limit reached ∙ resets 12am", now) == (
        (now + timedelta(days=1)).replace(hour=0))
    assert batch.reset_hint("resets 25:00", now) is None
    assert batch.reset_hint("resets 10:99", now) is None


# ── the release build ───────────────────────────────────────────────────


def test_snapshot_skips_a_fifo_and_refuses_an_oversized_run(tmp_path):
    run_dir = tmp_path / "arc2-fifo"
    run_dir.mkdir()
    (run_dir / "keep.txt").write_text("x")
    os.mkfifo(run_dir / "pipe")
    out = batch.snapshot_run(tmp_path, "arc2-fifo", tmp_path / "out")
    assert (out / "keep.txt").read_text() == "x" and not (out / "pipe").exists()

    big = tmp_path / "arc2-big"
    big.mkdir()
    (big / "blob").write_bytes(b"z" * 4096)
    with pytest.raises(batch.ReleaseBuildError, match="larger than 0 MB"):
        batch.snapshot_run(tmp_path, "arc2-big", tmp_path / "out2", max_bytes=1024)


def test_build_release_puts_its_tools_dir_on_the_path_before_importing(tmp_path, monkeypatch):
    empty_tools = tmp_path / "tools"
    empty_tools.mkdir()

    def stop(snapshot_root, slug, dest, **kw):
        raise RuntimeError("stop before the build")

    monkeypatch.setattr(batch, "snapshot_run", stop)
    assert str(empty_tools) not in sys.path
    with pytest.raises(RuntimeError, match="stop before the build"):
        batch.build_release(tmp_path, "arc2-none", tools_dir=empty_tools)
    assert str(empty_tools) in sys.path


# ── driver: state file, studio, dry run, queue and retry ────────────────


def run(phase="new", job_state=None, error=None, name="C103", slug="arc2-c103", auto=True, blocking=0):
    return {
        "slug": slug, "name": name, "phase": phase, "phase_text": phase,
        "job": {"state": job_state, "error": error} if job_state else None,
        "auto_accepted": auto, "actions_blocking": blocking,
    }


class FakeApi:
    def __init__(self, runs=None):
        self.runs = runs or []
        self.calls: list[tuple[str, str, object]] = []
        self.release = {
            "id": "rel-1", "course_id": "course-1", "catalogue_code": "C103",
            "auto_accepted": True, "version": 1, "state": "candidate", "open_actions": [],
        }
        self.course_published = False
        self.pub_states = ["published"]

    def get(self, path, **kw):
        self.calls.append(("GET", path, kw))
        if path == "/arc2/runs":
            return 200, {"runs": self.runs}
        if path.startswith("/courses/"):
            return 200, {"id": "course-1", "is_published": self.course_published}
        if path == "/integrations/platforms":
            return 200, [{"id": "moodle-1", "platform_type": "moodle", "is_active": True}]
        if path.startswith("/course-publications/"):
            return 200, {"id": "pub-1", "state": self.pub_states.pop(0)}
        return 404, None

    def post(self, path, **kw):
        self.calls.append(("POST", path, kw))
        if path == "/arc2/runs":
            name = kw["json"]["name"]
            slug = "arc2-" + name.lower().replace(" ", "-")
            self.runs.append(run("queued", "queued", name=name, slug=slug))
            return 201, {"slug": slug}
        if path.endswith("/retry"):
            return 200, {}
        if path == "/course-releases":
            return 201, dict(self.release)
        if path.endswith("/accept"):
            self.release["state"] = "accepted"
            return 200, dict(self.release)
        if path.endswith("/publications"):
            return 202, {"id": "pub-1", "state": "staging"}
        return 404, None

    def patch(self, path, **kw):
        self.calls.append(("PATCH", path, kw))
        self.course_published = True
        return 200, {}

    def posts(self, prefix=""):
        return [c for c in self.calls if c[0] == "POST" and c[1].startswith(prefix)]


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += timedelta(seconds=s)


class TickingClock(Clock):
    def __init__(self):
        super().__init__()
        self.slept: list[float] = []

    def sleep(self, s):
        self.slept.append(s)
        super().sleep(s)


def courses(*codes):
    return [batch.Course(code=c, title=c, yaml_name=f"{c.lower()}.yaml", doc={"modules": []}) for c in codes]


def built_meta(slug):
    return {"release_digest": "abc123", "catalogue_code": slug.removeprefix("arc2-").upper(), "auto_accepted": True}


def driver(api, cs, clock=None, own=None, **kw):
    clock = clock or Clock()
    built = []

    def builder(slug):
        built.append(slug)
        return b"tarball", built_meta(slug)

    kw.setdefault("test_host", True)
    d = batch.Driver(
        api, cs, builder=kw.pop("builder", builder), clock=clock, sleep=clock.sleep, log=lambda s: None, **kw
    )
    for code, slug in (own or {}).items():
        d.tracks[code].slug, d.tracks[code].created = slug, True
    d.built = built
    return d


def test_a_state_file_that_cannot_be_read_is_no_state(tmp_path):
    d = driver(FakeApi(), courses("C103"), resume=True)  # no --state at all
    assert d.tracks["C103"].status == "pending"
    corrupt = tmp_path / "state.json"
    corrupt.write_text("{not json")
    d = driver(FakeApi(), courses("C103"), state_path=corrupt, resume=True)
    assert d.tracks["C103"].status == "pending"
    d = driver(FakeApi(), courses("C103"), state_path=tmp_path, resume=True)  # a directory
    assert d.tracks["C103"].status == "pending"


def test_a_studio_answer_that_is_not_200_or_404_is_unavailable():
    class Down(FakeApi):
        def get(self, path, **kw):
            if path == "/arc2/runs":
                return 500, "exploded"
            return super().get(path, **kw)

    with pytest.raises(batch.StudioUnavailableError, match="answered 500: exploded"):
        driver(Down(), courses("C103")).step()


def test_the_dry_run_plan_carries_a_skipped_course_untouched():
    api = FakeApi()
    d = driver(api, courses("C105"))  # C105: kept as a draft
    assert d.plan() == [("C105", "kept as a draft (its release is a CI/CATAPULT fixture)")]
    assert not api.posts()


def test_a_step_for_a_run_the_studio_does_not_list_yet_waits_for_the_next_poll():
    api = FakeApi()  # the run exists (this driver queued it) but is not in the listing
    d = driver(api, courses("C103"), own={"C103": "arc2-c103"})
    assert d.step() is False
    t = d.tracks["C103"]
    assert t.status == "working" and "following this driver's run arc2-c103" in t.detail
    assert not api.posts()


class Full(FakeApi):
    def post(self, path, **kw):
        if path == "/arc2/runs":
            return 500, "queue is full"
        return super().post(path, **kw)


class OddSlug(FakeApi):
    def post(self, path, **kw):
        if path == "/arc2/runs":
            return 201, {"slug": "not a slug"}
        return super().post(path, **kw)


def test_a_refused_or_ill_named_queue_fails_the_course():
    d = driver(Full(), courses("C103"))
    assert d.step() is True  # the refusal is terminal for the course
    assert d.tracks["C103"].status == "failed" and "could not queue a run: 500 queue is full" in d.tracks["C103"].detail
    d = driver(OddSlug(), courses("C103"))
    d.step()
    assert d.tracks["C103"].status == "failed" and "could not queue a run: 201" in d.tracks["C103"].detail


class Retrying(FakeApi):
    def __init__(self, status):
        super().__init__([run("paused", "failed", "overloaded", name="C103", slug="arc2-c103")])
        self.status = status

    def post(self, path, **kw):
        if path.endswith("/retry"):
            return self.status, {"detail": "already running" if self.status == 409 else "no"}
        return super().post(path, **kw)


def test_a_retry_answered_409_is_followed_and_any_other_refusal_fails():
    for status, want in ((409, "retry not needed"), (500, "retry refused: 500")):
        api = Retrying(status)
        d = driver(api, courses("C103"), own={"C103": "arc2-c103"})
        d.step()  # the paused run puts the course in waiting_retry
        assert d.tracks["C103"].status == "waiting_retry"
        d.tracks["C103"].next_retry_at = "2020-01-01T00:00:00Z"  # due now
        d.step()
        assert d.tracks["C103"].status == ("working" if status == 409 else "failed")
        assert want in d.tracks["C103"].detail


# ── release → accept → publish refusals ─────────────────────────────────


class Refusal(FakeApi):
    """One endpoint answers with a canned reply; everything else is the happy path."""

    def __init__(self, method, path, reply):
        super().__init__([run("packaged", "done", name="C103")])
        self.method, self.path, self.reply = method, path, reply

    def _hits(self, method, path):
        return (method, path) == (self.method, self.path)

    def get(self, path, **kw):
        if self._hits("GET", path):
            return self.reply
        return super().get(path, **kw)

    def post(self, path, **kw):
        if self._hits("POST", path):
            return self.reply
        return super().post(path, **kw)

    def patch(self, path, **kw):
        if self._hits("PATCH", path):
            return self.reply
        return super().patch(path, **kw)


def test_release_refusals_up_to_the_accepted_state():
    api = Refusal("POST", "/course-releases", (413, "bundle too large"))
    d = driver(api, courses("C103"), own={"C103": "arc2-c103"})
    d.run()
    assert d.tracks["C103"].detail.startswith("upload refused: 413")

    api = Refusal("POST", "/course-releases/rel-1/accept", (409, {"detail": "gates changed"}))
    d = driver(api, courses("C103"), own={"C103": "arc2-c103"})
    assert d.run() == 1 and d.tracks["C103"].detail.startswith("accept refused: 409")

    accepted_not = dict(FakeApi().release, state="reviewing")
    api = Refusal("POST", "/course-releases/rel-1/accept", (200, accepted_not))
    d = driver(api, courses("C103"), own={"C103": "arc2-c103"})
    assert d.run() == 1 and "release v1 is reviewing, not accepted" in d.tracks["C103"].detail


def test_an_unreadable_or_unpublishable_course_stops_the_release():
    api = Refusal("GET", "/courses/course-1", (500, "boom"))
    d = driver(api, courses("C103"), own={"C103": "arc2-c103"})
    assert d.run() == 1 and "course course-1 not readable: 500" in d.tracks["C103"].detail

    api = Refusal("PATCH", "/courses/course-1", (500, "no"))
    d = driver(api, courses("C103"), own={"C103": "arc2-c103"})
    assert d.run() == 1 and "could not publish the TrueNorth course: 500 no" in d.tracks["C103"].detail


def test_a_chosen_platform_id_is_used_without_asking_the_registry():
    api = FakeApi([run("packaged", "done", name="C103")])
    d = driver(api, courses("C103"), platform_id="moodle-9", publish_platform="moodle", own={"C103": "arc2-c103"})
    assert d.run() == 0 and d.tracks["C103"].status == "published"
    pub = next(c for c in api.calls if c[1].endswith("/publications"))
    assert pub[2]["json"] == {"platform_id": "moodle-9"}
    assert ("GET", "/integrations/platforms") not in [(c[0], c[1]) for c in api.calls]


def test_a_refused_publication_fails_the_course():
    api = Refusal("POST", "/course-releases/rel-1/publications", (409, "no"))
    d = driver(api, courses("C103"), publish_platform="moodle", own={"C103": "arc2-c103"})
    assert d.run() == 1 and "publish refused: 409 no" in d.tracks["C103"].detail


def test_a_publication_that_never_settles_hits_the_timeout():
    class Staging(FakeApi):
        def get(self, path, **kw):
            if path.startswith("/course-publications/"):
                return 200, {"id": "pub-1", "state": "staging"}
            return super().get(path, **kw)

    clock = TickingClock()
    d = driver(Staging([run("packaged", "done", name="C103")]), courses("C103"), clock=clock,
               publish_platform="moodle", publish_timeout=30, own={"C103": "arc2-c103"})
    assert d.run() == 1
    t = d.tracks["C103"]
    assert t.status == "failed" and "still staging after the timeout" in t.detail
    assert clock.slept == [10, 10, 10]


def test_a_failed_publication_is_reported_with_its_state():
    api = FakeApi([run("packaged", "done", name="C103")])
    api.pub_states = ["failed"]
    d = driver(api, courses("C103"), publish_platform="moodle", own={"C103": "arc2-c103"})
    assert d.run() == 1 and d.tracks["C103"].detail.startswith("publication failed: ")


def test_run_polls_until_every_course_is_finished():
    class TwoPhase(FakeApi):
        polls = 0

        def get(self, path, **kw):
            if path == "/arc2/runs":
                self.polls += 1
                if self.polls >= 3:
                    self.runs[0] = run("packaged", "done", name="C103")
            return super().get(path, **kw)

    clock = TickingClock()
    d = driver(TwoPhase([run("running", "running", name="C103", slug="arc2-c103")]), courses("C103"),
               clock=clock, own={"C103": "arc2-c103"}, publish_platform="none")
    assert d.run() == 0 and d.tracks["C103"].status == "accepted"
    assert clock.slept == [60, 60]  # two polls between the first step and the packaged one


# ── main(): the guardrails, the connector and __main__ ──────────────────


def test_resume_without_a_state_file_is_an_argument_error():
    with pytest.raises(SystemExit) as exc:
        batch.main(["--admin", "a@example.test", "--resume"])
    assert exc.value.code == 2


def test_main_refuses_a_host_that_is_not_a_test_host(monkeypatch, capsys):
    monkeypatch.delenv("TN_TEST_HOST", raising=False)
    assert batch.main(["--admin", "a@example.test"]) == 2
    assert "not a test host" in capsys.readouterr().err


def test_main_refuses_a_course_spec_that_matches_nothing(monkeypatch, capsys):
    monkeypatch.setenv("TN_TEST_HOST", "true")
    assert batch.main(["--admin", "a@example.test", "--courses", "zzz-nope"]) == 2
    assert "no course matches --courses" in capsys.readouterr().err


def test_main_reports_a_connector_that_refuses(monkeypatch, capsys):
    monkeypatch.setenv("TN_TEST_HOST", "true")
    monkeypatch.setattr(_inprocess_api, "connect", lambda upn: None)
    assert batch.main(["--admin", "ghost@example.test"]) == 2


def test_main_dry_run_prints_each_request_it_would_send(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("TN_TEST_HOST", "true")
    monkeypatch.setenv("ARC2_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(_inprocess_api, "connect", lambda upn: (object(), FakeApi()))
    code = batch.main(["--admin", "a@example.test", "--courses", "C103", "--dry-run", "--print-requests"])
    assert code == 0
    out = capsys.readouterr().out
    assert "[C103]" in out and "catalogue_code C103" in out and "would queue a run" in out


def test_main_reports_a_studio_that_is_unavailable(monkeypatch, capsys):
    class Down(FakeApi):
        def get(self, path, **kw):
            if path == "/arc2/runs":
                return 500, "down"
            return super().get(path, **kw)

    monkeypatch.setenv("TN_TEST_HOST", "true")
    monkeypatch.setattr(_inprocess_api, "connect", lambda upn: (object(), Down()))
    assert batch.main(["--admin", "a@example.test", "--courses", "C103"]) == 2
    assert "arc2-batch:" in capsys.readouterr().err


def test_the_module_run_as_a_script_exits_with_the_batch_result(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("TN_TEST_HOST", "true")
    monkeypatch.setenv("ARC2_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(_inprocess_api, "connect", lambda upn: (object(), FakeApi()))
    monkeypatch.setattr(sys, "argv", [
        "arc2_batch_inprocess.py", "--admin", "a@example.test", "--courses", "C103", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "scripts" / "arc2_batch_inprocess.py"), run_name="__main__")
    assert exc.value.code == 0
    assert "would queue a run" in capsys.readouterr().out
