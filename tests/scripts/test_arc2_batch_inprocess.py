"""scripts/arc2_batch_inprocess.py: request text, run state machine, backoff, idempotency,
the release → accept → publish path, and the shared in-process harness (fakes only)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("arc2_batch_inprocess", ROOT / "scripts" / "arc2_batch_inprocess.py")
batch = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("arc2_batch_inprocess", batch)  # dataclasses look their module up
_spec.loader.exec_module(batch)
import _inprocess_api  # noqa: E402

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


# ── courses and request text ────────────────────────────────────────────


def test_every_shipped_course_loads_in_programme_order():
    courses = batch.load_courses()
    codes = [c.code for c in courses]
    assert len(codes) == len(list((ROOT / "content/courses").glob("*.yaml")))
    assert codes[0] == "C101" and codes.index("C110") < codes.index("C201") < codes.index("C301") < codes.index("C401")
    assert codes.index("C404") < codes.index("C304") < codes.index("RMC C201")  # IoT and RMC last
    assert all(c.startswith("RMC") for c in codes[codes.index("RMC C201") :])


def test_the_seven_courses_held_for_a_cleared_author_are_the_registers():
    held = {c.code for c in batch.load_courses() if c.held}
    assert held == {"C302", "C305", "C306", "RMC C202", "RMC C205", "RMC C207", "RMC C211"}


def test_select_by_code_and_by_glob():
    courses = batch.load_courses()
    assert [c.code for c in batch.select(courses, "rmc c202, C103")] == ["C103", "RMC C202"]
    assert {c.code for c in batch.select(courses, "c1*")} == {f"C1{n:02d}" for n in range(1, 11)}
    assert [c.code for c in batch.select(courses, "iot-*")] == ["C304"]
    assert batch.select(courses, None) == courses


def test_request_text_names_the_catalogue_course_and_fits_the_studio():
    for c in batch.load_courses():
        text = batch.request_text(c)
        assert f"catalogue_code {c.code})" in text
        assert len(text) <= 4000 and "\n" not in text
        assert "--slug" not in text and "--resume" not in text
    c105 = next(c for c in batch.load_courses() if c.code == "C105")
    text = batch.request_text(c105, hours=15)
    assert "15 student hours" in text and "Pattern: theory (T)" in text
    assert "Module 1: Security Principles and the CIA Triad." in text and "Objectives:" in text


def test_request_text_sheds_detail_rather_than_running_over():
    long = "x" * 300
    doc = {
        "duration_hours": 40,
        "difficulty": "intermediate",
        "modules": [
            {"ordinal": i, "title": f"M{i}", "objectives": [long] * 2, "topics": [long] * 3, "lab": "--slug " + long}
            for i in range(1, 7)
        ],
    }
    c = batch.Course(code="C999", title="Big", yaml_name="c999.yaml", doc=doc, pattern="R")
    text = batch.request_text(c, limit=2000)
    assert len(text) <= 2000 and "catalogue_code C999" in text
    assert "--slug" not in text
    assert "Topics:" not in text  # dropped first


# ── state machine and backoff ───────────────────────────────────────────


def run(phase="new", job_state=None, error=None, name="C103", slug="arc2-c103"):
    return {
        "slug": slug,
        "name": name,
        "phase": phase,
        "phase_text": phase,
        "job": {"state": job_state, "error": error} if job_state else None,
    }


@pytest.mark.parametrize(
    ("summary", "kind"),
    [
        (run("queued", "queued"), "working"),
        (run("running", "running"), "working"),
        (run("stopped", "failed", "STOP"), "held"),
        (run("takeover", "done"), "held"),
        (run("paused", "failed", "Claude AI usage limit reached|1760112000"), "retry"),
        (run("failed", "failed", "timed out after 2h"), "retry"),
        (run("paused", "failed", "claude exited with 1"), "failed"),
        (run("packaged", "done"), "packaged"),
        (run("outline", "done"), "gate"),
        (run("paused", "done"), "paused"),
    ],
)
def test_classify(summary, kind):
    assert batch.classify(summary)[0] == kind


def test_reset_hints():
    assert batch.reset_hint("usage limit reached|1760112000", T0) == datetime.fromtimestamp(1760112000, UTC)
    assert batch.reset_hint("5-hour limit reached ∙ resets 3pm", T0) == T0.replace(hour=15)
    assert batch.reset_hint("limit reached, resets at 9:30", T0) == (T0 + timedelta(days=1)).replace(hour=9, minute=30)
    assert batch.reset_hint("rate limit: try again in 20 minutes", T0) == T0 + timedelta(minutes=20)
    assert batch.reset_hint("claude exited with 1", T0) is None


def test_next_retry_uses_the_hint_then_exponential_backoff():
    assert batch.next_retry("usage limit reached ∙ resets 3pm", 1, T0) == T0.replace(hour=15, minute=2)
    assert batch.next_retry("overloaded", 1, T0, base=300) == T0 + timedelta(seconds=300)
    assert batch.next_retry("overloaded", 3, T0, base=300) == T0 + timedelta(seconds=1200)
    assert batch.next_retry("overloaded", 10, T0, base=300, cap=7200) == T0 + timedelta(seconds=7200)
    # A hint in the past is ignored.
    assert batch.next_retry("usage limit reached|1000000000", 2, T0, base=60) == T0 + timedelta(seconds=120)


# ── the driver against a fake API ───────────────────────────────────────


class FakeApi:
    def __init__(self, runs=None):
        self.runs = runs or []
        self.calls: list[tuple[str, str, object]] = []
        self.release = {
            "id": "rel-1",
            "course_id": "course-1",
            "version": 1,
            "state": "candidate",
            "open_actions": [{"id": "xapi:ARC2-C103", "category": "standards", "text": "t"}],
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
            return 200, [
                {"id": "fake-1", "platform_type": "fake", "is_active": True},
                {"id": "moodle-1", "platform_type": "moodle", "is_active": True},
            ]
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


def courses(*codes):
    return [batch.Course(code=c, title=c, yaml_name=f"{c.lower()}.yaml", doc={"modules": []}) for c in codes]


def driver(api, cs, clock=None, **kw):
    clock = clock or Clock()
    built = []

    def builder(slug):
        built.append(slug)
        return b"tarball", {"release_digest": "abc123"}

    d = batch.Driver(
        api, cs, builder=kw.pop("builder", builder), clock=clock, sleep=clock.sleep, log=lambda s: None, **kw
    )
    d.built = built
    return d


def test_an_existing_run_is_followed_never_queued_again():
    api = FakeApi([run("running", "running", name="C103")])
    d = driver(api, courses("C103"))
    d.step()
    assert not api.posts("/arc2/runs")
    assert d.tracks["C103"].slug == "arc2-c103" and d.tracks["C103"].status == "working"


def test_concurrency_limits_new_runs():
    api = FakeApi()
    d = driver(api, courses("C101", "C102", "C103"), concurrency=2)
    d.step()
    assert [c[2]["json"]["name"] for c in api.posts("/arc2/runs")] == ["C101", "C102"]
    d.step()  # both still working: nothing more
    assert len(api.posts("/arc2/runs")) == 2


def test_held_courses_are_skipped_unless_included():
    cs = courses("C302")
    cs[0].held = True
    api = FakeApi()
    assert driver(api, cs).run() == 0
    assert not api.posts()
    d = driver(FakeApi(), cs, include_held=True)
    d.step()
    assert d.tracks["C302"].status == "working"


def test_stop_and_takeover_are_held_without_retrying():
    api = FakeApi(
        [run("stopped", "failed", "STOP: refused", name="C101"), run("takeover", "done", name="C102", slug="arc2-c102")]
    )
    d = driver(api, courses("C101", "C102"))
    assert d.run() == 0
    assert {t.status for t in d.tracks.values()} == {"held"}
    assert not api.posts()


def test_usage_limit_waits_for_the_reset_then_retries_and_caps_attempts():
    clock = Clock()
    api = FakeApi([run("paused", "failed", "Claude usage limit reached ∙ resets 3pm", name="C101", slug="arc2-c101")])
    d = driver(api, courses("C101"), clock=clock, max_attempts=2)
    d.step()
    t = d.tracks["C101"]
    assert t.status == "waiting_retry" and t.next_retry_at == "2026-10-10T15:02:00Z"
    clock.now = T0.replace(hour=14)
    d.step()
    assert not api.posts("/arc2/runs/arc2-c101/retry")  # not yet
    clock.now = T0.replace(hour=15, minute=3)
    d.step()
    assert len(api.posts("/arc2/runs/arc2-c101/retry")) == 1 and t.status == "working"
    d.step()  # fails again (the fake still says so): attempt 2
    assert t.status == "waiting_retry" and t.attempts == 2
    clock.now += timedelta(days=1)
    d.step()  # retried
    d.step()  # third failure: over the cap
    assert t.status == "failed" and "gave up after 2" in t.detail
    assert d.run() == 1


def test_a_non_retryable_failure_fails_the_course():
    api = FakeApi([run("paused", "failed", "claude exited with 1", name="C101", slug="arc2-c101")])
    d = driver(api, courses("C101"))
    assert d.run() == 1 and not api.posts()


def test_a_run_stuck_at_a_gate_fails_after_the_stall_timeout():
    clock = Clock()
    api = FakeApi([run("outline", "done", name="C101", slug="arc2-c101")])
    d = driver(api, courses("C101"), clock=clock, stall_timeout=600)
    d.step()
    assert d.tracks["C101"].status == "working"
    clock.now += timedelta(minutes=11)
    d.step()
    assert d.tracks["C101"].status == "failed" and "ARC2_AUTO_ACCEPT_GATES" in d.tracks["C101"].detail


def test_packaged_run_is_released_accepted_and_published():
    api = FakeApi([run("packaged", "done", name="C103")])
    api.pub_states = ["verifying", "published"]
    d = driver(api, courses("C103"))
    assert d.run() == 0
    t = d.tracks["C103"]
    assert t.status == "published" and t.release_id == "rel-1" and d.built == ["arc2-c103"]
    upload = api.posts("/course-releases")[0]
    assert upload[1] == "/course-releases" and upload[2]["files"]["file"][1] == b"tarball"
    accept = next(c for c in api.calls if c[1].endswith("/accept"))
    assert accept[2]["json"] == {
        "acknowledge_actions": ["xapi:ARC2-C103"],
        "notes": "auto-accepted test content (staging)",
    }
    assert ("PATCH", "/courses/course-1", {"json": {"is_published": True}}) in api.calls
    pub = next(c for c in api.calls if c[1].endswith("/publications"))
    assert pub[2]["json"] == {"platform_id": "moodle-1"} and pub[2]["params"] == {"wait": "true"}


def test_an_already_accepted_release_is_not_accepted_again_and_none_skips_lms():
    api = FakeApi([run("packaged", "done", name="C103")])
    api.release["state"] = "accepted"
    api.course_published = True
    d = driver(api, courses("C103"), publish_platform="none")
    assert d.run() == 0
    assert d.tracks["C103"].status == "accepted"
    assert not any(c[1].endswith(("/accept", "/publications")) for c in api.calls)
    assert not any(c[0] == "PATCH" for c in api.calls)


def test_a_refused_build_fails_the_course_without_uploading():
    def refuse(slug):
        raise batch.ReleaseBuildError("QA is fail, not pass")

    api = FakeApi([run("packaged", "done", name="C103")])
    d = driver(api, courses("C103"), builder=refuse)
    assert d.run() == 1 and "QA is fail" in d.tracks["C103"].detail
    assert not api.posts("/course-releases")


def test_no_moodle_platform_fails_after_accepting():
    api = FakeApi([run("packaged", "done", name="C103")])
    d = driver(api, courses("C103"), platform_id=None)
    d._platform = lambda: None
    assert d.run() == 1 and "no active Moodle" in d.tracks["C103"].detail


def test_dry_run_queues_nothing():
    api = FakeApi([run("running", "running", name="C101", slug="arc2-c101")])
    d = driver(api, courses("C101", "C102"))
    assert d.run(dry_run=True) == 0
    assert not api.posts()
    assert "follow existing run arc2-c101" in d.tracks["C101"].detail
    assert d.tracks["C102"].detail.startswith("would queue")


def test_resume_keeps_retry_counts_and_times(tmp_path):
    state = tmp_path / "state.json"
    clock = Clock()
    api = FakeApi([run("paused", "failed", "overloaded", name="C101", slug="arc2-c101")])
    d = driver(api, courses("C101"), clock=clock, state_path=state)
    d.step()
    saved = json.loads(state.read_text())["courses"]["C101"]
    assert saved["status"] == "waiting_retry" and saved["attempts"] == 1
    d2 = driver(api, courses("C101"), clock=clock, state_path=state, resume=True)
    assert d2.tracks["C101"].status == "waiting_retry" and d2.tracks["C101"].attempts == 1
    d2.step()  # not due yet: no retry
    assert not api.posts("/arc2/runs/arc2-c101/retry")


def test_studio_off_is_reported():
    class Off(FakeApi):
        def get(self, path, **kw):
            return 404, {"detail": "Not Found"}

    with pytest.raises(batch.StudioUnavailableError, match="ARC2_STUDIO_ENABLED"):
        driver(Off(), courses("C101")).step()


# ── the shared harness ──────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeClient:
    """Rotates the CSRF cookie on every response, as the middleware does."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.cookies = {"truenorth_csrf": "tok-0"}
        self.sent: list[tuple[str, dict]] = []
        self.n = 0

    def request(self, method, path, content=None, files=None, params=None, headers=None):
        self.sent.append((method, dict(headers or {})))
        self.n += 1
        self.cookies["truenorth_csrf"] = f"tok-{self.n}"
        status = self.statuses.pop(0)
        return FakeResponse(status, {"retry_after": 5} if status == 429 else {"ok": True})


def test_harness_reads_the_csrf_cookie_before_every_attempt_and_backs_off_on_429():
    slept = []
    client = FakeClient([200, 429, 201])
    api = _inprocess_api.InProcessApi(client, sleep=slept.append)
    api.get("/arc2/runs")
    status, body = api.post("/arc2/runs", json={"name": "C101"})
    assert status == 201 and body == {"ok": True}
    assert "x-csrf-token" not in client.sent[0][1]  # GET: none needed
    assert client.sent[1][1]["x-csrf-token"] == "tok-1"  # the cookie the GET set
    assert client.sent[2][1]["x-csrf-token"] == "tok-2"  # the cookie the 429 set
    assert client.sent[1][1]["Content-Type"] == "application/json"
    assert slept == [6]
