"""Test-host auto-accept (tools/arc2/runner.py maybe_auto_accept, ARC2_AUTO_ACCEPT_GATES).

On a test host the runner accepts the outline and preview gates itself when a job stops
at one with nothing failing, and says so: the accept job is requested by
"auto (test host)", carries ``auto_accept``, and its engine is told to record
``accepted_by`` on the gate. A fake ``claude`` plays the engine: on ``accept`` it moves
the gate on as ``check.py gate`` would, recording ARC2_GATE_ACCEPTED_BY.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from arc2 import runner

SLUG = "arc2-auto-test"

FAKE = r"""#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps({"prompt": prompt, "accepted_by": os.environ.get("ARC2_GATE_ACCEPTED_BY")}) + "\n")
run = os.path.join(os.environ["FAKE_RUNS"], prompt.split()[2])
manifest_path = os.path.join(run, "manifest.json")
if prompt.endswith(" accept") and os.environ.get("FAKE_ENGINE") != "stuck":
    m = json.load(open(manifest_path))
    gate = "outline" if m["gates"]["outline"]["state"] == "pending" else "preview"
    m["gates"][gate]["state"] = "accepted"
    if os.environ.get("ARC2_GATE_ACCEPTED_BY"):
        m["gates"][gate]["accepted_by"] = os.environ["ARC2_GATE_ACCEPTED_BY"]
    if gate == "outline":  # stages 2-5 and QA run, then the preview gate opens
        for k in m["stages"]:
            if k != "package-builder":
                m["stages"][k]["state"] = "done"
        m["qa"]["result"] = "pass"
        m["gates"]["preview"]["state"] = "pending"
    else:
        m["stages"]["package-builder"]["state"] = "done"
    json.dump(m, open(manifest_path, "w"))
if os.environ.get("FAKE_ENGINE") == "fail":
    print(json.dumps({"type": "result", "is_error": True, "result": "merge rejected"})); sys.exit(1)
print(json.dumps({"type": "result", "is_error": False, "result": "Stopped at a gate."}))
"""

STAGES = ("content-architect", "code-generator", "range-engineer", "artifact-creator", "sensor-gateway",
          "qa-tester", "package-builder")


def manifest(outline="pending", preview="n/a", done=1, qa="not_run", failed: str | None = None) -> dict:
    stages = {k: {"state": "done" if i < done else "pending"} for i, k in enumerate(STAGES)}
    if failed:
        stages[failed]["state"] = "failed"
    return {"stages": stages, "gates": {"outline": {"state": outline}, "preview": {"state": preview}},
            "qa": {"result": qa, "cycle": 0}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    runs = tmp_path / "runs"
    runner.dirs(runs)
    monkeypatch.setenv("FAKE_CALLS", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("FAKE_RUNS", str(runs))
    for var in ("ARC2_AUTO_ACCEPT_GATES", "ARC2_GATE_ACCEPTED_BY", "ARC2_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARC2_FALLBACK", "off")
    return exe, runs, tmp_path / "calls.jsonl"


def setup_run(runs: Path, m: dict, text: str = "a course") -> None:
    (runs / SLUG).mkdir(parents=True, exist_ok=True)
    (runs / SLUG / "manifest.json").write_text(json.dumps(m))
    job = {"id": "human00001", "action": "start", "slug": SLUG, "text": text, "created_at": "2026-10-09T12:00:00Z",
           "requested_by": "author@example.test", "tenant_id": "00000000-0000-0000-0000-000000000001"}
    (runs / "_queue" / f"20261009T120000-{job['id']}.json").write_text(json.dumps(job))


def go(exe: Path, runs: Path) -> list[dict]:
    assert runner.main(["--runs", str(runs), "--claude", str(exe), "--once"]) == 0
    jobs = [json.loads(p.read_text()) for p in (runs / "_jobs").glob("*.json")]
    return sorted(jobs, key=lambda j: j.get("started_at") or "")


def calls(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ── Which stops are accepted ────────────────────────────────────────────
@pytest.mark.parametrize(("m", "gate"), [
    (manifest(), "outline"),
    (manifest(outline="accepted", preview="pending", done=6, qa="pass"), "preview"),
    (manifest(outline="accepted", preview="pending", done=5, qa="fail"), None),  # QA failing
    (manifest(outline="accepted", preview="pending", done=3, qa="pass"), None),  # stages 4-5 not done
    (manifest(outline="accepted", preview="pending", done=6, qa="human_takeover"), None),
    (manifest(failed="content-architect"), None),  # a STOP
    (manifest(outline="feedback"), None),
    (manifest(outline="accepted", preview="accepted", done=7, qa="pass"), None),  # packaged
    (None, None),
])
def test_only_a_clean_stop_at_a_gate_is_accepted(m, gate):
    assert runner.pending_gate(m) == gate


def test_range_stages_that_do_not_apply_count_as_complete():
    m = manifest(outline="accepted", preview="pending", done=6, qa="pass")
    m["stages"]["range-engineer"]["state"] = m["stages"]["sensor-gateway"]["state"] = "not_applicable"
    assert runner.pending_gate(m) == "preview"


# ── The runner ──────────────────────────────────────────────────────────
def test_off_by_default_the_run_waits_for_a_person(env):
    exe, runs, calls_file = env
    setup_run(runs, manifest())
    [rec] = go(exe, runs)
    assert rec["state"] == "done" and "auto_accept_queued" not in rec
    assert not list((runs / "_queue").glob("*.json"))


def test_on_a_test_host_both_gates_are_accepted_and_marked_as_automatic(env, monkeypatch):
    exe, runs, calls_file = env
    monkeypatch.setenv("ARC2_AUTO_ACCEPT_GATES", "true")
    setup_run(runs, manifest())
    start, outline, preview = go(exe, runs)

    assert start["auto_accept_queued"]["gate"] == "outline"
    assert start["auto_accept_queued"]["job"] == outline["id"]
    for job, gate, after in ((outline, "outline", start), (preview, "preview", outline)):
        assert (job["action"], job["text"], job["state"]) == ("resume", "accept", "done")
        assert job["requested_by"] == "auto (test host)"
        assert job["tenant_id"] == start["tenant_id"], "the run's owner, for the API and the audit"
        assert job["auto_accept"]["gate"] == gate and job["auto_accept"]["accepted_by"] == "auto (test host)"
        assert job["auto_accept"]["after_job"] == after["id"] and job["auto_accept"]["at"]
    assert "auto_accept_queued" not in preview, "packaged: nothing left to accept"

    m = json.loads((runs / SLUG / "manifest.json").read_text())
    assert m["gates"]["outline"]["accepted_by"] == m["gates"]["preview"]["accepted_by"] == "auto (test host)"
    seen = calls(calls_file)
    assert [c["prompt"] for c in seen] == [f"/arc2 --slug {SLUG} a course", f"/arc2 --resume {SLUG} accept",
                                           f"/arc2 --resume {SLUG} accept"]
    assert [c["accepted_by"] for c in seen] == [None, "auto (test host)", "auto (test host)"]


@pytest.mark.parametrize("m", [
    manifest(failed="content-architect"),
    manifest(outline="accepted", preview="pending", done=6, qa="human_takeover"),
    manifest(outline="accepted", preview="pending", done=5, qa="fail"),
])
def test_never_past_a_stop_a_takeover_or_failing_qa(env, monkeypatch, m):
    exe, runs, _ = env
    monkeypatch.setenv("ARC2_AUTO_ACCEPT_GATES", "1")
    setup_run(runs, m)
    [rec] = go(exe, runs)
    assert "auto_accept_queued" not in rec


def test_never_after_a_failed_job(env, monkeypatch):
    exe, runs, _ = env
    monkeypatch.setenv("ARC2_AUTO_ACCEPT_GATES", "1")
    monkeypatch.setenv("FAKE_ENGINE", "fail")
    setup_run(runs, manifest())
    [rec] = go(exe, runs)
    assert rec["state"] == "failed" and "auto_accept_queued" not in rec


def test_a_persons_queued_reply_wins(env, monkeypatch):
    exe, runs, _ = env
    monkeypatch.setenv("ARC2_AUTO_ACCEPT_GATES", "1")
    (runs / SLUG).mkdir(parents=True)
    (runs / SLUG / "manifest.json").write_text(json.dumps(manifest()))
    record = {"id": "job1", "slug": SLUG, "state": "done"}
    (runs / "_queue" / "20261009T130000-human2.json").write_text(json.dumps({"id": "human2", "slug": SLUG}))
    assert runner.maybe_auto_accept(runs, record) is None


def test_a_run_that_keeps_stopping_at_a_gate_is_accepted_a_bounded_number_of_times(env, monkeypatch):
    exe, runs, _ = env
    monkeypatch.setenv("ARC2_AUTO_ACCEPT_GATES", "1")
    monkeypatch.setenv("FAKE_ENGINE", "stuck")  # accept never moves the gate on
    setup_run(runs, manifest())
    jobs = go(exe, runs)
    assert sum(1 for j in jobs if j.get("auto_accept")) == runner.AUTO_ACCEPT_LIMIT
    assert len(jobs) == runner.AUTO_ACCEPT_LIMIT + 1


def test_a_linked_run_or_manifest_is_not_read(env, monkeypatch, tmp_path):
    _, runs, _ = env
    monkeypatch.setenv("ARC2_AUTO_ACCEPT_GATES", "1")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "manifest.json").write_text(json.dumps(manifest()))
    (runs / SLUG).symlink_to(elsewhere)
    assert runner.read_manifest(runs, SLUG) is None
    (runs / SLUG).unlink()
    (runs / SLUG).mkdir()
    (runs / SLUG / "manifest.json").symlink_to(elsewhere / "manifest.json")
    assert runner.read_manifest(runs, SLUG) is None
    assert runner.maybe_auto_accept(runs, {"id": "j", "slug": SLUG, "state": "done"}) is None


def test_a_persons_accept_never_carries_the_automatic_marker(env, monkeypatch):
    """Even if the runner's own environment has it, only the runner's accept job gets it."""
    exe, runs, calls_file = env
    monkeypatch.setenv("ARC2_GATE_ACCEPTED_BY", "auto (test host)")
    setup_run(runs, manifest())
    (runs / "_queue" / "20261009T120000-human00001.json").write_text(json.dumps({
        "id": "human00001", "action": "resume", "slug": SLUG, "text": "accept", "created_at": "2026-10-09T12:00:00Z",
        "requested_by": "author@example.test"}))
    go(exe, runs)
    [call] = calls(calls_file)
    assert call["accepted_by"] is None
    assert "accepted_by" not in json.loads((runs / SLUG / "manifest.json").read_text())["gates"]["outline"]
