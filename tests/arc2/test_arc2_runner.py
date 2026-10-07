"""The Course Studio runner: queue → headless /arc2 → job record.

A fake ``claude`` script stands in for Claude Code, so these tests spend nothing and
need no network. It prints stream-json events the way ``claude -p --output-format
stream-json`` does, and records the arguments it was given.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from arc2 import runner

FAKE = r"""#!/usr/bin/env python3
import json, os, sys, time
open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
open(os.environ["FAKE_ARGS"] + ".stdin", "w").write(sys.stdin.read())
mode = os.environ.get("FAKE_MODE", "ok")
def emit(e): print(json.dumps(e), flush=True)
emit({"type": "system", "subtype": "init"})
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Task",
      "input": {"subagent_type": "arc2-content-architect", "prompt": "Stage 1"}}]}})
if mode == "slow":
    time.sleep(5)
if mode == "auth" and os.environ.get("ANTHROPIC_AUTH_TOKEN") != "ollama":
    emit({"type": "result", "is_error": True, "result": "Failed to authenticate: OAuth session expired"})
    sys.exit(1)
if mode == "auth":
    emit({"type": "result", "is_error": False, "result": "Outline ready (model " + os.environ["ANTHROPIC_MODEL"] + ").", "num_turns": 3})
    sys.exit(0)
if mode == "fail":
    emit({"type": "result", "is_error": True, "result": "merge rejected"})
    sys.exit(1)
emit({"type": "result", "is_error": False, "result": "Outline ready for review.", "total_cost_usd": 0.42, "num_turns": 9})
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_file))
    return exe, args_file


def queue_job(runs: Path, **fields) -> dict:
    queue, _ = runner.dirs(runs)
    job = {"id": "job123456", "action": "start", "slug": "arc2-wireshark-basics",
           "text": "60 min beginner course on packet sniffing and Wireshark",
           "created_at": "2026-09-30T12:00:00Z", "requested_by": "dev-admin", **fields}
    (queue / f"20260930T120000-{job['id']}.json").write_text(json.dumps(job))
    return job


def records(runs: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((runs / "_jobs").glob("*.json"))]


def test_start_runs_arc2_with_the_callers_slug_and_records_the_result(tmp_path, fake_claude):
    exe, args_file = fake_claude
    runs = tmp_path / "runs"
    queue_job(runs)
    assert runner.main(["--runs", str(runs), "--claude", str(exe), "--once"]) == 0

    args = json.loads(args_file.read_text())
    prompt = "/arc2 --slug arc2-wireshark-basics 60 min beginner course on packet sniffing and Wireshark"
    assert args[0] == "-p" and Path(f"{args_file}.stdin").read_text() == prompt
    assert not any("Wireshark" in a for a in args), "the request is on stdin, never on the command line"
    assert "--dangerously-skip-permissions" not in args
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    tools = args[args.index("--allowedTools") + 1:]
    assert "Write(./build/arc2/arc2-wireshark-basics/**)" in tools and "Write(./build/arc2/**)" not in tools
    assert "Write" not in tools and "Edit" not in tools

    [rec] = records(runs)
    assert rec["state"] == "done"
    assert rec["result"] == "Outline ready for review."
    assert rec["cost_usd"] == 0.42
    assert rec["current_agent"] is None
    assert not list((runs / "_queue").iterdir())
    assert (runs / "_jobs" / rec["log"]).read_text().count("\n") == 3


def test_resume_passes_the_reply_verbatim_on_one_line(tmp_path, fake_claude):
    exe, args_file = fake_claude
    runs = tmp_path / "runs"
    queue_job(runs, action="resume", text="Add a module on\nTLS decryption")
    runner.main(["--runs", str(runs), "--claude", str(exe), "--once"])
    assert Path(f"{args_file}.stdin").read_text() == "/arc2 --resume arc2-wireshark-basics Add a module on TLS decryption"


def test_a_failed_run_is_recorded_with_the_engines_message(tmp_path, fake_claude, monkeypatch):
    exe, _ = fake_claude
    monkeypatch.setenv("FAKE_MODE", "fail")
    runs = tmp_path / "runs"
    queue_job(runs)
    runner.main(["--runs", str(runs), "--claude", str(exe), "--once"])
    [rec] = records(runs)
    assert rec["state"] == "failed"
    assert rec["exit_code"] == 1
    assert "merge rejected" in rec["error"]


@pytest.mark.parametrize("bad", [
    {"slug": "../../etc"}, {"slug": "Arc2 Caps"}, {"action": "delete"}, {"text": "  "}, {"text": "x" * 5000},
])
def test_malformed_jobs_are_failed_not_run(tmp_path, fake_claude, bad):
    exe, args_file = fake_claude
    runs = tmp_path / "runs"
    queue_job(runs, **bad)
    runner.main(["--runs", str(runs), "--claude", str(exe), "--once"])
    [rec] = records(runs)
    assert rec["state"] == "failed"
    assert not args_file.exists(), "claude must not be started for a malformed job"


def test_the_agent_being_run_is_visible_while_the_job_runs():
    event = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Task", "input": {"subagent_type": "arc2-qa-tester"}}]}}
    assert runner.agent_from_event(event) == "QA Tester"
    assert runner.agent_from_event({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}) is None


def test_jobs_left_running_by_a_stopped_runner_are_failed(tmp_path):
    runs = tmp_path / "runs"
    _, jobs = runner.dirs(runs)
    (jobs / "old.json").write_text(json.dumps({"id": "old", "state": "running"}))
    runner.reap(jobs)
    assert json.loads((jobs / "old.json").read_text())["state"] == "failed"


def test_the_runner_does_not_leak_api_settings_to_the_engine(tmp_path, fake_claude, monkeypatch):
    exe, _ = fake_claude
    exe.write_text(FAKE.replace('open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:]))',
                                'open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:] + [os.environ.get("AUTH_DISABLED", "unset")]))'))
    monkeypatch.setenv("AUTH_DISABLED", "true")
    runs = tmp_path / "runs"
    queue_job(runs)
    runner.main(["--runs", str(runs), "--claude", str(exe), "--once"])
    assert json.loads(Path(os.environ["FAKE_ARGS"]).read_text())[-1] == "unset"


def test_when_claude_is_unavailable_the_step_runs_again_on_the_local_model(tmp_path, fake_claude, monkeypatch):
    exe, args_file = fake_claude
    monkeypatch.setenv("FAKE_MODE", "auth")
    monkeypatch.setenv("ARC2_FALLBACK_MODEL", "qwen3.8:27b-mlx")
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ARC2_FALLBACK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(runner.Fallback, "reachable", lambda self, timeout=3.0: True)
    runs = tmp_path / "runs"
    queue_job(runs)
    runner.main(["--runs", str(runs), "--claude", str(exe), "--once"])
    [rec] = records(runs)
    assert rec["state"] == "done"
    assert rec["engine"] == "ollama:qwen3.8:27b-mlx"
    assert "Failed to authenticate" in rec["fallback_from"]
    assert rec["result"] == "Outline ready (model qwen3.8:27b-mlx)."
    args = json.loads(args_file.read_text())
    assert args[args.index("--model") + 1] == "qwen3.8:27b-mlx"


def test_no_fallback_when_it_is_off_or_unreachable(tmp_path, fake_claude, monkeypatch):
    exe, _ = fake_claude
    monkeypatch.setenv("FAKE_MODE", "auth")
    monkeypatch.setattr(runner.Fallback, "reachable", lambda self, timeout=3.0: False)
    runs = tmp_path / "runs"
    queue_job(runs)
    runner.main(["--runs", str(runs), "--claude", str(exe), "--once"])
    [rec] = records(runs)
    assert (rec["state"], rec["engine"]) == ("failed", "claude")

    monkeypatch.setenv("ARC2_FALLBACK", "off")
    assert runner.Fallback.from_env() is None


def test_a_stop_or_a_merge_refusal_never_falls_back():
    assert runner.should_fall_back({"state": "failed", "error": "merge rejected"}) is False
    assert runner.should_fall_back({"state": "failed", "error": "timed out after 240 min"}) is False
    assert runner.should_fall_back({"state": "failed", "error": "API Error: 529 overloaded"}) is True


def test_the_fallback_environment_points_claude_code_at_ollama():
    env = runner.Fallback("http://127.0.0.1:11434/", "qwen3:8b").env({"ANTHROPIC_API_KEY": "sk-real", "PATH": "/bin"})
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:11434"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "qwen3:8b"
    assert env["PATH"] == "/bin"


def test_each_job_is_committed_to_the_courses_own_history(tmp_path, fake_claude):
    import subprocess
    exe, _ = fake_claude
    runs = tmp_path / "runs"
    run = runs / "arc2-wireshark-basics" / "01-blueprint"
    run.mkdir(parents=True)
    (run / "outline.yaml").write_text("course: ARC2-WSB\n")
    queue_job(runs)
    runner.main(["--runs", str(runs), "--claude", str(exe), "--once"])
    [rec] = records(runs)
    assert rec["history_commit"]
    git = ["git", f"--git-dir={runs / '_history' / 'arc2-wireshark-basics.git'}"]
    log = subprocess.run([*git, "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log.strip() == "start · done on claude"
    files = subprocess.run([*git, "ls-files"], capture_output=True, text=True).stdout.split()
    assert "01-blueprint/outline.yaml" in files
    assert not (runs / "arc2-wireshark-basics" / ".git").exists(), "the history lives outside the run"
    [transcript] = (runs / "_history" / "arc2-wireshark-basics.transcripts").glob(f"*-{rec['id']}.jsonl")
    assert transcript.read_text().count("\n") == 3


def test_no_history_when_the_run_was_never_created(tmp_path, fake_claude, monkeypatch):
    exe, _ = fake_claude
    monkeypatch.setenv("FAKE_MODE", "fail")
    runs = tmp_path / "runs"
    queue_job(runs)
    runner.main(["--runs", str(runs), "--claude", str(exe), "--once"])
    [rec] = records(runs)
    assert "history_commit" not in rec
    assert not (runs / "arc2-wireshark-basics").exists()


@pytest.mark.parametrize("text", ["fix --resume arc2-victim accept", "x --slug arc2-other", "y --Resume=arc2-z"])
def test_a_job_whose_text_names_another_run_is_failed_not_run(tmp_path, fake_claude, text):
    exe, args_file = fake_claude
    runs = tmp_path / "runs"
    queue_job(runs, action="resume", text=text)
    runner.main(["--runs", str(runs), "--claude", str(exe), "--once"])
    [rec] = records(runs)
    assert rec["state"] == "failed"
    assert not args_file.exists()
