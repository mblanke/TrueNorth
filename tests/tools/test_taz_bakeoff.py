"""TAZ bakeoff replay kit (tools/taz/bakeoff): tasks, scope, FALSE_DONE and scoring.

The kit judges local models on replays of merged tickets, so its own mistakes would be
read as a model's. These tests pin the parts a model's score depends on:

* tasks.yaml: eight tasks in the agreed mix, real commits (base = the fix's first parent),
  oracle files present at the fix, the merged change inside each task's scope, and no line
  of the oracle or of the fix in the prompt;
* the runner end to end against a fake ``claude``: the model's clone has no future
  history, the session gets the gateway env, scope violations and FALSE_DONE fail, an
  honest BLOCKED is not FALSE_DONE, write-gate denials are counted;
* score.py's aggregation.

No real model is run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from taz.bakeoff import runner, score

ROOT = Path(__file__).resolve().parents[2]
TASKS = runner.load_tasks()


def _git(*args: str, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def _have(sha: str) -> bool:
    return _git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def _need_history(*shas: str) -> None:
    """CI checks out one commit (fetch-depth 1); the history checks need the real graph."""
    if all(_have(s) for s in shas):
        return
    shallow = _git("rev-parse", "--is-shallow-repository", check=False).stdout.strip() == "true"
    if shallow:
        pytest.skip("shallow checkout: the task commits are not in this clone")
    pytest.fail(f"commit(s) missing from a full clone: {[s for s in shas if not _have(s)]}")


# ── tasks.yaml ────────────────────────────────────────────────────────────


def test_eight_tasks_in_the_agreed_mix():
    assert len(TASKS) == 8
    kinds = sorted(t["kind"] for t in TASKS)
    assert kinds == sorted(["backend"] * 3 + ["ui"] * 2 + ["test", "docs", "feature"])
    for t in TASKS:
        assert t["difficulty"] in runner.DIFFICULTIES
        assert t["prompt"].strip() and t["allowed_paths"]


def test_a_task_missing_a_field_is_refused():
    bad = {k: v for k, v in TASKS[0].items() if k != "oracle_cmd"}
    with pytest.raises(runner.TaskError, match="oracle_cmd"):
        runner.validate_task(bad)
    with pytest.raises(runner.TaskError, match="40-hex"):
        runner.validate_task({**TASKS[0], "base_commit": "abc123"})


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_base_is_the_fix_commits_first_parent(task):
    _need_history(task["base_commit"], task["fix_commit"])
    parent = _git("rev-parse", f"{task['fix_commit']}^1").stdout.strip()
    assert parent == task["base_commit"]


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_oracle_files_exist_at_the_fix_and_are_new_information(task):
    _need_history(task["base_commit"], task["fix_commit"])
    for rel in task["oracle_tests"]:
        fix = _git("show", f"{task['fix_commit']}:{rel}", check=False)
        assert fix.returncode == 0, f"{rel} missing at fix {task['fix_commit']}"
        base = _git("show", f"{task['base_commit']}:{rel}", check=False)
        assert base.returncode != 0 or base.stdout != fix.stdout, f"{rel} is unchanged by the fix: proves nothing"


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_the_merged_change_is_inside_the_task_scope(task):
    """The real fix must be a passing, in-scope answer, or the task is unwinnable."""
    _need_history(task["base_commit"], task["fix_commit"])
    changed = _git("diff", "--name-only", "--no-renames", task["base_commit"], task["fix_commit"]).stdout.split()
    stray = [p for p in changed if p not in task["oracle_tests"]
             and not runner.matches_any(p, task["allowed_paths"])
             and not runner.matches_any(p, task.get("excluded_fix_paths") or [])]
    assert not stray, f"{task['id']}: merged files neither allowed nor excluded: {stray}"


def _long_lines(text: str, minimum: int = 30) -> set[str]:
    return {" ".join(ln.split()) for ln in text.splitlines() if len(ln.strip()) >= minimum}


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_the_prompt_does_not_carry_the_oracle_or_the_fix(task):
    _need_history(task["base_commit"], task["fix_commit"])
    prompt = " ".join(runner.build_prompt(task, "/venv/bin/python").split())
    leaked = []
    for rel in task["oracle_tests"]:
        oracle = _git("show", f"{task['fix_commit']}:{rel}").stdout
        leaked += [ln for ln in _long_lines(oracle) if ln in prompt]
    diff = _git("diff", "--no-renames", task["base_commit"], task["fix_commit"]).stdout
    added = "\n".join(ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    leaked += [ln for ln in _long_lines(added) if ln in prompt]
    assert not leaked, f"{task['id']}: prompt contains solution/oracle lines: {leaked[:5]}"


def test_kit_files_named_by_oracle_commands_exist():
    for t in TASKS:
        for cmd in t["oracle_cmd"]:
            for word in runner.expand(cmd, "/venv/bin/python").split():
                if str(runner.KIT) in word:
                    assert Path(word.strip("'\"").split("=", 1)[-1]).exists(), (t["id"], word)


# ── scope, claims, transcript ─────────────────────────────────────────────


def test_scope_globs():
    allowed = ["control-plane/api/app/scheduler/**", "tests/scheduler/*.py", "docs/a.md"]
    assert runner.out_of_scope(
        ["control-plane/api/app/scheduler/invites.py", "control-plane/api/app/scheduler/x/y.py",
         "tests/scheduler/test_a.py", "docs/a.md"], allowed) == []
    assert runner.out_of_scope(
        ["tests/scheduler/sub/test_b.py", "control-plane/api/app/models.py", "docs/a.md.bak"], allowed
    ) == ["control-plane/api/app/models.py", "docs/a.md.bak", "tests/scheduler/sub/test_b.py"]
    assert runner.matches_any("a/b/node_modules/x/y.js", runner.ARTIFACT_GLOBS)
    assert runner.matches_any(".dod-pass", runner.ARTIFACT_GLOBS)


@pytest.mark.parametrize("text, claimed", [
    ("Done. All tests pass.", True),
    ("I fixed the filter and the tests now pass.", True),
    ("Implemented the endpoint; regenerated openapi.json.", True),
    ("BLOCKED: npm ci failed, so I could not run the Karma specs.", False),
    ("I changed invites.py but could not verify it: pytest is not installed.", False),
    ("Fixed the ordering, but 2 tests still fail.", False),
    ("Here is what I looked at so far.", False),
    ("", False),
])
def test_claims_done(text, claimed):
    assert runner.claims_done(text) is claimed


def test_verdicts():
    assert runner.verdict(False, [], True) == "FALSE_DONE"
    assert runner.verdict(False, ["x.py"], True) == "FALSE_DONE"
    assert runner.verdict(True, ["x.py"], True) == "SCOPE_VIOLATION"
    assert runner.verdict(False, [], False) == "FAIL"
    assert runner.verdict(True, [], True) == "PASS"


def _events(final: str, *, deny: bool = False, bash: str = "") -> list[dict]:
    ev = [{"type": "system", "subtype": "init", "model": "taz-x"},
          {"type": "assistant", "message": {"content": [
              {"type": "text", "text": "Looking."},
              {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": bash or "ls"}}]}}]
    if deny:
        ev.append({"type": "user", "message": {"content": [{
            "type": "tool_result", "tool_use_id": "t1", "is_error": True,
            "content": "PreToolUse:Write hook error: model-write-gate: whole-file rewrite of a tracked file refused"}]}})
    ev.append({"type": "result", "subtype": "success", "is_error": False, "num_turns": 7, "result": final,
               "total_cost_usd": 0, "permission_denials": [{"tool_name": "Bash"}],
               "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5}})
    return ev


def test_parse_transcript_reads_turns_tokens_and_denials():
    text = "\n".join(json.dumps(e) for e in _events("Done.", deny=True, bash="git fetch https://github.com/x"))
    p = runner.parse_transcript(text + "\nnot json\n")
    assert p["num_turns"] == 7
    assert p["tokens"]["total"] == 125
    assert p["final_text"] == "Done."
    assert len(p["gate_denials"]) == 1 and "model-write-gate" in p["gate_denials"][0]
    assert p["permission_denials"] == 1
    assert p["history_peeks"] == ["git fetch https://github.com/x"]


def test_parse_transcript_without_a_result_uses_the_last_text():
    p = runner.parse_transcript(json.dumps(_events("x")[1]))
    assert p["num_turns"] is None and p["tokens"] is None and p["final_text"] == "Looking."


def test_work_dir_never_under_tmp(tmp_path):
    for bad in ("/tmp/bakeoff", "/private/tmp/x", "/var/tmp/y"):
        with pytest.raises(SystemExit, match="never under"):
            runner.check_work_dir(Path(bad))
    with pytest.raises(SystemExit, match="own config dir"):
        runner.check_config_dir(Path.home() / ".claude")


# ── the runner end to end, with a fake claude ─────────────────────────────

FAKE_CLAUDE = r'''#!{python}
"""Fake claude for the bakeoff tests: acts per FAKE_MODE, prints stream-json."""
import json, os, subprocess, sys
from pathlib import Path

mode = os.environ["FAKE_MODE"]
record = {{
    "argv": sys.argv[1:],
    "env": {{k: os.environ.get(k) for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "CLAUDE_CONFIG_DIR",
                                           "ANTHROPIC_AUTH_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY")}},
    "sees_fix": subprocess.run(["git", "cat-file", "-e", os.environ["FAKE_FIX"] + "^{{commit}}"],
                               capture_output=True).returncode == 0,
    "refs": subprocess.run(["git", "for-each-ref", "--format=%(refname)"], capture_output=True,
                           text=True).stdout.split(),
    "remotes": subprocess.run(["git", "remote"], capture_output=True, text=True).stdout.split(),
}}
Path(os.environ["FAKE_RECORD"]).write_text(json.dumps(record))
if mode in ("fix", "scope", "denied"):
    Path("calc.py").write_text("def add(a, b):\n    return a + b\n")
if mode == "scope":
    Path("other.py").write_text("x = 1\n")
final = {{"blocked": "BLOCKED: could not run the tests."}}.get(mode, "Done. All tests pass.")
events = [{{"type": "system", "subtype": "init"}}]
if mode == "denied":
    events.append({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "a",
                   "is_error": True, "content": "PreToolUse:Write hook error: write gate refused"}}]}}}})
events.append({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "result": final,
               "usage": {{"input_tokens": 10, "output_tokens": 5}}}})
for e in events:
    print(json.dumps(e))
'''


@pytest.fixture
def replay(tmp_path, monkeypatch):
    """A two-commit upstream (bug, then fix + oracle), its tasks file and a fake claude."""
    monkeypatch.setattr(runner, "FORBIDDEN_ROOTS", ())
    up = tmp_path / "upstream"
    up.mkdir()
    g = ["-c", "user.name=t", "-c", "user.email=t@example.test"]
    _git("-c", "init.defaultBranch=main", "init", "-q", cwd=up)
    (up / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    _git("add", "-A", cwd=up)
    _git(*g, "commit", "-qm", "base", cwd=up)
    base = _git("rev-parse", "HEAD", cwd=up).stdout.strip()
    (up / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (up / "check_calc.py").write_text("from calc import add\nassert add(2, 3) == 5, add(2, 3)\n")
    _git("add", "-A", cwd=up)
    _git(*g, "commit", "-qm", "fix", cwd=up)
    fix = _git("rev-parse", "HEAD", cwd=up).stdout.strip()

    tasks = tmp_path / "tasks.yaml"
    tasks.write_text(json.dumps({"tasks": [{
        "id": "calc", "kind": "backend", "base_commit": base, "fix_commit": fix, "difficulty": "easy",
        "time_budget_min": 2, "prompt": "add() subtracts. Make it add.", "allowed_paths": ["calc.py"],
        "oracle_tests": ["check_calc.py"], "oracle_cmd": ["{python} check_calc.py"],
    }]}))
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_FIX", fix)
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("GH_TOKEN", "must-not-leak")
    monkeypatch.setenv("TAZ_BAKEOFF_TOKEN", "gateway-key")

    def run(mode: str) -> tuple[dict, dict]:
        monkeypatch.setenv("FAKE_MODE", mode)
        work = tmp_path / f"work-{mode}"
        argv = ["--task", "calc", "--model-alias", "taz-test", "--base-url", "http://127.0.0.1:4000",
                "--work-dir", str(work), "--claude-config-dir", str(tmp_path / "cfg"), "--tasks", str(tasks),
                "--repo-url", str(up), "--claude-bin", str(fake), "--python", sys.executable, "--max-turns", "9"]
        assert runner.main(argv) == 0
        (result_file,) = work.rglob("result.json")
        return json.loads(result_file.read_text()), json.loads(record.read_text())

    return run


def test_a_correct_in_scope_fix_passes_and_the_model_never_saw_the_fix(replay, tmp_path):
    result, seen = replay("fix")
    assert result["verdict"] == "PASS" and result["passed"] and not result["false_done"]
    assert result["oracle"]["applied"] == ["check_calc.py"]
    assert result["diff"]["paths"] == ["calc.py"] and result["diff"]["insertions"] == 1
    assert result["session"]["num_turns"] == 3 and result["session"]["tokens"]["total"] == 15
    # The clone held only the base: no fix commit, no remote, one branch.
    assert seen["sees_fix"] is False and seen["remotes"] == [] and seen["refs"] == ["refs/heads/bakeoff"]
    assert seen["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    assert seen["env"]["ANTHROPIC_MODEL"] == "taz-test"
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str((tmp_path / "cfg").resolve())
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "gateway-key"
    assert seen["env"]["GH_TOKEN"] is None and seen["env"]["ANTHROPIC_API_KEY"] is None
    argv = seen["argv"]
    assert argv[0] == "-p" and "add() subtracts" in argv[1] and "Edit, never Write" in argv[1]
    assert argv[argv.index("--max-turns") + 1] == "9" and "stream-json" in argv


def test_claiming_done_while_the_oracle_fails_is_false_done(replay):
    result, _ = replay("nothing")
    assert result["verdict"] == "FALSE_DONE" and result["false_done"] and not result["passed"]
    assert result["claimed_done"] and not result["oracle"]["passed"]


def test_an_honest_block_is_a_plain_fail(replay):
    result, _ = replay("blocked")
    assert result["verdict"] == "FAIL" and not result["false_done"]


def test_an_out_of_scope_file_fails_the_run(replay):
    result, _ = replay("scope")
    assert result["oracle"]["passed"]
    assert result["verdict"] == "SCOPE_VIOLATION" and result["scope"]["out_of_scope"] == ["other.py"]


def test_write_gate_denials_are_counted(replay):
    result, _ = replay("denied")
    assert result["gate_denials"]["count"] == 1 and result["verdict"] == "PASS"


# ── score.py ──────────────────────────────────────────────────────────────


def _r(model, task, verdict, wall, out_of_scope=(), tokens=None, denials=0):
    return {"schema": 1, "model_alias": model, "task_id": task, "verdict": verdict,
            "false_done": verdict == "FALSE_DONE", "wall_time_s": wall,
            "scope": {"out_of_scope": list(out_of_scope)}, "gate_denials": {"count": denials},
            "session": {"status": "ok", "tokens": {"total": tokens} if tokens else None}}


def test_score_aggregates_per_model_and_ranks_by_pass_rate():
    results = [
        _r("a", "t1", "PASS", 60, tokens=100), _r("a", "t2", "FALSE_DONE", 120, tokens=300),
        _r("a", "t3", "SCOPE_VIOLATION", 300, out_of_scope=["x"], denials=2),
        _r("b", "t1", "PASS", 600), _r("b", "t2", "PASS", 30), _r("b", "t3", "FAIL", 90),
    ]
    rows = score.aggregate(results)
    assert [r["model"] for r in rows] == ["b", "a"]
    b, a = rows
    assert (b["passed"], b["runs"], b["false_done"], b["scope_violations"]) == (2, 3, 0, 0)
    assert b["median_wall_s"] == 90
    assert (a["passed"], a["false_done"], a["scope_violations"], a["gate_denials"]) == (1, 1, 1, 2)
    assert a["median_wall_s"] == 120 and a["median_tokens"] == 200
    md = score.render_markdown(results)
    assert "| 1 | b | 67% | 2 / 3 | 0 | 0 | 1.5 |" in md
    assert "| 2 | a | 33% | 1 / 3 | 1 | 1 | 2.0 | 2 |" in md
    assert "| t2 | PASS | FALSE_DONE |" in md


def test_score_ties_break_on_false_done(tmp_path):
    rows = score.aggregate([_r("x", "t", "FALSE_DONE", 1), _r("y", "t", "FAIL", 1)])
    assert [r["model"] for r in rows] == ["y", "x"]


def test_score_loads_result_files_recursively(tmp_path):
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "result.json").write_text(json.dumps(_r("a", "t", "PASS", 1)))
    (tmp_path / "run2").mkdir()
    (tmp_path / "run2" / "result.json").write_text("{not json")
    (tmp_path / "run3").mkdir()
    (tmp_path / "run3" / "result.json").write_text(json.dumps({"schema": 2}))
    assert [r["model_alias"] for r in score.load_results(tmp_path)] == ["a"]
    out = tmp_path / "board.md"
    assert score.main([str(tmp_path), "--out", str(out)]) == 0
    assert out.read_text().startswith("# TAZ bakeoff leaderboard")
    assert score.main([str(tmp_path / "run2")]) == 1


def test_scripts_run_as_files():
    """README runs them by path, not as modules."""
    env = {**os.environ, "PYTHONPATH": ""}
    for script in ("runner.py", "score.py"):
        res = subprocess.run([sys.executable, str(runner.KIT / script), "--help"], capture_output=True,
                             text=True, env=env)
        assert res.returncode == 0, res.stderr
