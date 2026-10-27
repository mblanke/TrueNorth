"""TAZ write gate (rings 1-6) and DoD stop gate: tools/taz/hooks.

Every test runs the hook the way Claude Code does: a JSON payload on stdin, a fake
transcript on disk, and a scrubbed environment. Each ring must refuse its target, and
the cases a working session needs (prose, scratch dirs, test runners) must pass.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "tools" / "taz" / "hooks"
GATE = HOOKS / "write_gate.py"
STOP = HOOKS / "dod_stop_gate.py"
POLICY = HOOKS / "policy.json"

CLAUDE = "claude-opus-4-1"
GPT_OSS = "gpt-oss-120b"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = (tmp_path / "work" / "repo").resolve()
    r.mkdir(parents=True)
    _git(r, "init", "-q")
    files = {
        "big.py": "".join(f"x{i} = {i}\n" for i in range(151)),
        "edge.py": "".join(f"y{i} = {i}\n" for i in range(150)),
        "src/app.py": "print('app')\n",
        "src/tmp/helper.py": "pass\n",
        "tests/test_oracle.py": "def test_x():\n    assert True\n",
        ".dod-ruff-baseline": "17\n",
        ".github/workflows/ci.yml": "name: CI\n",
        "tools/taz/hooks/policy.json": "{}\n",
        "control-plane/api/alembic/versions/abc_init.py": "pass\n",
        "control-plane/web/package-lock.json": "{}\n",
        "control-plane/api/app/rbac.py": "pass\n",
        "control-plane/worker/worker/provisioners/vsphere.py": "pass\n",
        "control-plane/api/app/result_transfer/service.py": "pass\n",
        "control-plane/api/app/classes/onboarding.py": "pass\n",
        "scripts/dod.sh": "#!/usr/bin/env bash\n",
        "docs/notes.md": "# notes\n",
    }
    for rel, text in files.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def transcript(tmp_path: Path, *records: dict) -> str:
    path = tmp_path / f"transcript-{len(list(tmp_path.glob('transcript-*')))}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(path)


def said(model: str, *reads: str) -> dict:
    """An assistant turn served by `model`, optionally Reading files."""
    content = [{"type": "tool_use", "name": "Read", "input": {"file_path": f}} for f in reads]
    return {"type": "assistant", "message": {"model": model, "content": content or [{"type": "text", "text": "ok"}]}}


COMPACT = {"type": "system", "subtype": "compact_boundary"}


def run_gate(tmp_path: Path, payload, *, env: dict | None = None, policy: dict | None = None,
             card: dict | None = None, raw: str | None = None, args: list[str] | None = None):
    """Run write_gate.py as a hook. Returns (decision, reason)."""
    e = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home")}
    if policy is not None:
        merged = json.loads(POLICY.read_text())
        merged.update(policy)
        pf = tmp_path / "policy.json"
        pf.write_text(json.dumps(merged))
        e["TAZ_GATE_POLICY"] = str(pf)
    if card is not None:
        cf = tmp_path / "card.json"
        cf.write_text(json.dumps(card) if isinstance(card, dict) else card)
        e["TAZ_CARD"] = str(cf)
    e.update(env or {})
    stdin = raw if raw is not None else json.dumps(payload)
    r = subprocess.run([sys.executable, str(GATE), *(args or [])], input=stdin, capture_output=True,
                       text=True, env=e, timeout=30)
    assert r.returncode == 0, r.stderr
    if not r.stdout.strip():
        return "allow", ""
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    return out["permissionDecision"], out["permissionDecisionReason"]


def write(path: Path | str, transcript_path: str, cwd: Path, tool: str = "Write") -> dict:
    ti = {"file_path": str(path)}
    if tool == "Write":
        ti["content"] = "x = 1\n"
    elif tool == "Edit":
        ti.update(old_string="x0 = 0", new_string="x0 = 1")
    return {"session_id": "s1", "tool_name": tool, "tool_input": ti,
            "transcript_path": transcript_path, "cwd": str(cwd)}


def bash(cmd: str, cwd: Path, transcript_path: str = "") -> dict:
    return {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": cmd},
            "transcript_path": transcript_path, "cwd": str(cwd)}


# ── policy ────────────────────────────────────────────────────────────────


def test_default_policy_allowlists_only_claude_and_records_the_owner_rule():
    policy = json.loads(POLICY.read_text())
    assert policy["write_allowlist"] == ["claude-"]
    assert "no Chinese models except GLM" in policy["_comment"]
    assert all(policy["rings"].values())
    assert not any(r.startswith((".", "/tmp", "/scratchpad")) for r in policy["exempt_roots"])


# ── ring 1: capability ────────────────────────────────────────────────────


def test_ring1_refuses_an_unlisted_served_model(repo, tmp_path):
    t = transcript(tmp_path, said(GPT_OSS))
    decision, reason = run_gate(tmp_path, write(repo / "src/app.py", t, repo, "Edit"))
    assert decision == "deny" and "ring capability" in reason and GPT_OSS in reason


def test_ring1_served_model_beats_the_requested_alias(repo, tmp_path):
    t = transcript(tmp_path, said(GPT_OSS))
    decision, _ = run_gate(tmp_path, write(repo / "src/app.py", t, repo, "Edit"),
                           env={"ANTHROPIC_MODEL": CLAUDE, "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000"})
    assert decision == "deny"


def test_ring1_refuses_an_unknown_model_behind_a_gateway(repo, tmp_path):
    t = transcript(tmp_path)  # nothing served yet, no ANTHROPIC_MODEL
    decision, reason = run_gate(tmp_path, write(repo / "src/app.py", t, repo, "Edit"),
                                env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:4000"})
    assert decision == "deny" and "could not determine" in reason


def test_ring1_matches_the_allowlist_case_insensitively(repo, tmp_path):
    t = transcript(tmp_path, said("Claude-Opus-4-1"))
    assert run_gate(tmp_path, write(repo / "src/app.py", t, repo, "Edit"))[0] == "allow"
    t2 = transcript(tmp_path, said("zai-org/GLM-4.6"))
    assert run_gate(tmp_path, write(repo / "src/app.py", t2, repo, "Edit"),
                    policy={"write_allowlist": ["claude-", "glm-4.6"]})[0] == "allow"


def test_ring1_lets_any_model_write_prose(repo, tmp_path):
    t = transcript(tmp_path, said(GPT_OSS))
    assert run_gate(tmp_path, write(repo / "docs/notes.md", t, repo, "Edit"))[0] == "allow"


def test_empty_allowlist_entries_do_not_match_everything(repo, tmp_path):
    t = transcript(tmp_path, said(GPT_OSS))
    decision, _ = run_gate(tmp_path, write(repo / "src/app.py", t, repo, "Edit"),
                           policy={"write_allowlist": ["", " "]})
    assert decision == "deny"


# ── ring 2: whole-file rewrite ────────────────────────────────────────────


def test_ring2_refuses_a_whole_file_write_over_151_tracked_lines(repo, tmp_path):
    t = transcript(tmp_path, said(CLAUDE, str(repo / "big.py")))
    decision, reason = run_gate(tmp_path, write(repo / "big.py", t, repo))
    assert decision == "deny" and "ring whole-file" in reason and "151 lines" in reason


def test_ring2_allows_150_lines_an_edit_and_a_new_file(repo, tmp_path):
    t = transcript(tmp_path, said(CLAUDE, str(repo / "edge.py"), str(repo / "big.py")))
    assert run_gate(tmp_path, write(repo / "edge.py", t, repo))[0] == "allow"
    assert run_gate(tmp_path, write(repo / "big.py", t, repo, "Edit"))[0] == "allow"
    assert run_gate(tmp_path, write(repo / "src/new.py", t, repo))[0] == "allow"


# ── ring 3: post-compaction amnesia ───────────────────────────────────────


def test_ring3_refuses_an_edit_after_compaction_without_a_reread(repo, tmp_path):
    t = transcript(tmp_path, said(CLAUDE, str(repo / "big.py")), COMPACT, said(CLAUDE))
    decision, reason = run_gate(tmp_path, write(repo / "big.py", t, repo, "Edit"))
    assert decision == "deny" and "ring amnesia" in reason


def test_ring3_allows_the_edit_once_the_file_is_read_again(repo, tmp_path):
    t = transcript(tmp_path, said(CLAUDE, str(repo / "big.py")), COMPACT, said(CLAUDE, str(repo / "big.py")))
    assert run_gate(tmp_path, write(repo / "big.py", t, repo, "Edit"))[0] == "allow"


# ── ring 4: task card ─────────────────────────────────────────────────────


def card_for(repo: Path, **extra) -> dict:
    return {"repo_root": str(repo), "allowed_paths": ["src/**"],
            "oracle_tests": ["tests/test_oracle.py"], **extra}


def test_ring4_allows_writes_inside_the_card(repo, tmp_path):
    t = transcript(tmp_path, said(CLAUDE))
    assert run_gate(tmp_path, write(repo / "src/app.py", t, repo, "Edit"), card=card_for(repo))[0] == "allow"


@pytest.mark.parametrize("target", ["edge.py", "docs/notes.md", "srcx/app.py", "../outside.py"])
def test_ring4_refuses_a_path_outside_the_card(repo, tmp_path, target):
    t = transcript(tmp_path, said(CLAUDE))
    decision, reason = run_gate(tmp_path, write(repo / target, t, repo), card=card_for(repo))
    assert decision == "deny" and "ring card" in reason


def test_ring4_refuses_a_symlink_that_leaves_the_card(repo, tmp_path):
    (repo / "src" / "escape").symlink_to(repo / "docs")
    t = transcript(tmp_path, said(CLAUDE))
    decision, _ = run_gate(tmp_path, write(repo / "src/escape/notes.md", t, repo), card=card_for(repo))
    assert decision == "deny"


@pytest.mark.parametrize("card", [
    "not json",
    {"allowed_paths": ["src/**"]},                          # no repo_root
    {"repo_root": "relative", "allowed_paths": ["src/**"]},
    {"repo_root": "/x", "allowed_paths": []},
    {"repo_root": "/x", "allowed_paths": ["src/**"], "oracle_tests": "tests/x.py"},
])
def test_ring4_fails_closed_on_a_broken_card(repo, tmp_path, card):
    t = transcript(tmp_path, said(CLAUDE))
    decision, _ = run_gate(tmp_path, write(repo / "src/app.py", t, repo, "Edit"), card=card)
    assert decision == "deny"
    assert run_gate(tmp_path, bash("ls", repo), card=card)[0] == "deny"


# ── ring 5: protected paths ───────────────────────────────────────────────


def test_ring5_refuses_the_cards_oracle_test(repo, tmp_path):
    t = transcript(tmp_path, said(CLAUDE, str(repo / "tests/test_oracle.py")))
    card = card_for(repo, allowed_paths=["src/**", "tests/**"])
    decision, reason = run_gate(tmp_path, write(repo / "tests/test_oracle.py", t, repo, "Edit"), card=card)
    assert decision == "deny" and "oracle test" in reason


@pytest.mark.parametrize("target", [
    ".dod-ruff-baseline", ".github/workflows/ci.yml", ".GitHub/workflows/new.yml",
    "tools/taz/hooks/policy.json", "control-plane/api/alembic/versions/abc_init.py",
    "control-plane/web/package-lock.json", "control-plane/api/app/rbac.py",
    "control-plane/worker/worker/provisioners/vsphere.py",
    "control-plane/api/app/result_transfer/service.py", "control-plane/api/app/classes/onboarding.py",
    "scripts/dod.sh", ".git/config", "src/.git/config",
])
def test_ring5_refuses_protected_paths_even_for_claude_and_even_when_the_card_allows_them(repo, tmp_path, target):
    t = transcript(tmp_path, said(CLAUDE, str(repo / target)))
    assert run_gate(tmp_path, write(repo / target, t, repo, "Edit"))[0] == "deny"
    card = card_for(repo, allowed_paths=["**"])
    decision, reason = run_gate(tmp_path, write(repo / target, t, repo, "Edit"), card=card)
    assert decision == "deny" and "ring protected" in reason


def test_ring5_follows_symlinks(repo, tmp_path):
    (repo / "src" / "ci.yml").symlink_to(repo / ".github/workflows/ci.yml")
    t = transcript(tmp_path, said(CLAUDE))
    assert run_gate(tmp_path, write(repo / "src/ci.yml", t, repo, "Edit"))[0] == "deny"


def test_ring5_list_is_configurable(repo, tmp_path):
    t = transcript(tmp_path, said(CLAUDE))
    decision, _ = run_gate(tmp_path, write(repo / "src/app.py", t, repo, "Edit"),
                           policy={"protected_paths": ["src/app.py"]})
    assert decision == "deny"


# ── exempt roots: exact prefix, never the work dir ────────────────────────


def test_exempt_root_is_an_exact_prefix(repo, tmp_path):
    scratch = (tmp_path / "scratch").resolve()
    scratch.mkdir()
    t = transcript(tmp_path, said(GPT_OSS))
    pol = {"exempt_roots": [str(scratch)]}
    assert run_gate(tmp_path, write(scratch / "x.py", t, repo), policy=pol)[0] == "allow"
    assert run_gate(tmp_path, write(scratch / "deep/er/x.py", t, repo), policy=pol)[0] == "allow"
    sibling = tmp_path / "scratch-evil" / "x.py"
    assert run_gate(tmp_path, write(sibling, t, repo), policy=pol)[0] == "deny"


def test_a_repo_path_containing_tmp_is_not_exempt(repo, tmp_path):
    """The original matched "/tmp/" as a substring, so this path bypassed every ring."""
    t = transcript(tmp_path, said(GPT_OSS))
    pol = {"exempt_roots": ["/tmp", "/private/tmp", "~/.claude/plans"]}
    decision, _ = run_gate(tmp_path, write(repo / "src/tmp/helper.py", t, repo, "Edit"), policy=pol)
    assert decision == "deny"


def test_a_work_dir_under_an_exempt_root_is_never_exempt(repo, tmp_path):
    t = transcript(tmp_path, said(GPT_OSS))
    pol = {"exempt_roots": [str(tmp_path)]}  # contains the repo
    assert run_gate(tmp_path, write(repo / "src/app.py", t, repo, "Edit"), policy=pol)[0] == "deny"
    card = card_for(repo, allowed_paths=["**"])
    assert run_gate(tmp_path, write(repo / ".github/workflows/ci.yml", t, repo, "Edit"),
                    policy=pol, card=card)[0] == "deny"


def test_relative_and_root_exempt_entries_are_ignored(repo, tmp_path):
    t = transcript(tmp_path, said(GPT_OSS))
    pol = {"exempt_roots": ["/", ".agent-patches/", "scratchpad/"]}
    assert run_gate(tmp_path, write(tmp_path / "elsewhere" / "x.py", t, repo), policy=pol)[0] == "deny"


# ── fail closed ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["not json", "[]", '{"tool_name": "Write", "tool_input": "x"}',
                                 '{"tool_name": "Write", "tool_input": {}}', '{"tool_input": {}}',
                                 '{"tool_name": "Bash", "tool_input": {"command": 7}}'])
def test_malformed_input_is_refused(tmp_path, raw):
    assert run_gate(tmp_path, None, raw=raw)[0] == "deny"


def test_a_broken_policy_file_is_refused(repo, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    t = transcript(tmp_path, said(CLAUDE))
    assert run_gate(tmp_path, write(repo / "docs/notes.md", t, repo),
                    env={"TAZ_GATE_POLICY": str(bad)})[0] == "deny"
    assert run_gate(tmp_path, write(repo / "docs/notes.md", t, repo),
                    env={"TAZ_GATE_POLICY": str(tmp_path / "missing.json")})[0] == "deny"
    assert run_gate(tmp_path, write(repo / "docs/notes.md", t, repo), args=["--bogus"])[0] == "deny"


def test_policy_via_argument(repo, tmp_path):
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps({"write_allowlist": ["gpt-oss"]}))
    t = transcript(tmp_path, said(GPT_OSS))
    assert run_gate(tmp_path, write(repo / "src/app.py", t, repo, "Edit"), args=["--policy", str(pf)])[0] == "allow"


def test_logging_is_optional_and_goes_where_configured(repo, tmp_path):
    log = tmp_path / "logs" / "gate.jsonl"
    t = transcript(tmp_path, said(GPT_OSS))
    run_gate(tmp_path, write(repo / "src/app.py", t, repo, "Edit"), env={"TAZ_GATE_LOG": str(log)})
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["decision"] == "deny" and entry["ring"] == "capability"


# ── ring 6: Bash ──────────────────────────────────────────────────────────

BASH_WRITES = [
    "echo hi > src/app.py", "echo hi >> src/app.py", "printf x >| out", "ls &> out", "ls 2> err.log",
    "cat x | tee out", "tee -a out < x", "sed -i 's/a/b/' src/app.py", "sed -i.bak s/a/b/ f",
    "sed --in-place s/a/b/ f", "sed -Ei s/a/b/ f", "sed 's/a/b/w out' f", "sed -n '1w out' f",
    "cp a b", "mv a b", "rm -rf src", "dd if=/dev/zero of=f", "install -m 644 a b",
    "python -c \"open('f', 'w').write('x')\"", "python3 -c \"open('f', mode='a')\"",
    "python -c \"import pathlib; pathlib.Path('f').write_text('x')\"",
    "python -c \"from pathlib import Path; Path('f').open('w')\"",
    "python -c \"import os; os.remove('f')\"", "python -c \"__import__('os').system('rm f')\"",
    "python - <<'EOF'\nopen('f','w')\nEOF", "python script.py", "git apply fix.diff", "git am x.patch",
    "git checkout -- src/app.py", "git restore src", "git stash", "git -c core.pager=sh log",
    "git diff --output=f", "patch -p1 < fix.diff", "cat > f <<EOF\nx\nEOF",
    "cat <<EOF > f\nx\nEOF", "tee f <<EOF\nx\nEOF", "bash -c 'echo > f'", "sh x.sh", "./x.sh",
    "echo $(touch f)", "echo `touch f`", "diff <(ls) x", "cat <<EOF\n$(touch f)\nEOF",
    "ls; rm f", "ls && rm f", "ls || rm f", "ls | xargs rm", "ls\nrm f", "ls a#; rm f",
    "find . -delete", "find . -exec rm {} ;", "awk '{print > \"f\"}' x", "awk 'BEGIN{system(\"rm f\")}'",
    "PATH=/evil ls", "export PATH=/evil", "eval 'rm f'", "exec 3>f", "touch f", "mkdir d", "ln -s a b",
    "chmod +x f", "curl -o f http://x", "wget http://x", "npm install", "npm ci", "npx prettier --write .",
    "ruff check --fix .", "ruff format .", "npm run build", "python scripts/other.py", "ng test --karma-config=evil.js", "pytest --junitxml=f",
    "pytest -p evil", "sort -o f x", "uniq a b", "rg --pre ./x foo", "sudo ls", "PY=./evil bash scripts/dod.sh",
    "sed -n \"$X\" f", "for f in a; do cp $f b; done", "if true; then rm f; fi", "{ rm f; }", "(rm f)",
    "f() { rm x; }; f",
]


@pytest.mark.parametrize("cmd", BASH_WRITES)
def test_ring6_refuses_bash_that_writes(repo, tmp_path, cmd):
    decision, reason = run_gate(tmp_path, bash(cmd, repo))
    assert decision == "deny", cmd
    assert "ring bash" in reason


BASH_READS = [
    "ls -la", "cat src/app.py | head -5", "grep -rn foo src | wc -l", "git status", "git diff HEAD~1 -- src",
    "git log --oneline -5", "git -C . show HEAD:src/app.py", "find . -name '*.py'", "sed -n '1,20p' f",
    "sed 's/a/b/g' f", "awk '{print $1}' f", "echo hi 2>/dev/null", "ls 2>&1 | head", "ls > /dev/null 2>&1",
    "python -m pytest -q tests/x", ".venv/bin/python -m pytest tests/tools -k gate", "pytest -x -q",
    "python3 -c 'import json; print(json.load(open(\"package.json\"))[\"name\"])'",
    "python -c \"print('a-b'.replace('-', '_'))\"", "ruff check .", "python -m ruff check tools",
    "ruff format --check .", "npm test", "npm run test -- --watch=false",
    "npx ng test --watch=false --browsers=ChromeHeadless", "npx ng lint", "ng build",
    "CHROME_BIN=/x DOD_WEB=1 bash scripts/dod.sh", "bash scripts/dod.sh", "./scripts/dod.sh",
    "[ -f x ] && echo y", "cat <<'EOF' | grep x\n$(not run)\nEOF", "for f in a b; do cat $f; done",
    "ls # ; rm f", "jq .name package.json", ".venv/bin/python scripts/export_openapi.py", "npm run gen:api", "diff a b", "wc -l < src/app.py",
]


@pytest.mark.parametrize("cmd", BASH_READS)
def test_ring6_allows_read_only_commands_and_test_runners(repo, tmp_path, cmd):
    decision, reason = run_gate(tmp_path, bash(cmd, repo))
    assert decision == "allow", (cmd, reason)


def test_ring6_dod_script_must_be_the_repos_own(repo, tmp_path):
    (repo / "docs" / "scripts").mkdir()
    (repo / "docs" / "scripts" / "dod.sh").write_text("rm -rf /\n")
    assert run_gate(tmp_path, bash("bash scripts/dod.sh", repo / "docs"))[0] == "deny"
    assert run_gate(tmp_path, bash("cd docs && bash scripts/dod.sh", repo))[0] == "deny"


def test_ring6_applies_to_every_model_and_can_be_switched_off(repo, tmp_path):
    t = transcript(tmp_path, said(CLAUDE))
    assert run_gate(tmp_path, bash("rm -rf src", repo, t))[0] == "deny"
    pol = {"rings": {"bash": False}}
    assert run_gate(tmp_path, bash("rm -rf src", repo, t), policy=pol)[0] == "allow"


def test_other_tools_pass_through(repo, tmp_path):
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(repo / "big.py")}, "cwd": str(repo)}
    assert run_gate(tmp_path, payload)[0] == "allow"


# ── DoD stop gate ─────────────────────────────────────────────────────────


def run_stop(tmp_path: Path, cwd: Path, session: str = "s1", raw: str | None = None):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"),
           "TAZ_STOP_STATE": str(tmp_path / "stop-state")}
    stdin = raw if raw is not None else json.dumps({"session_id": session, "cwd": str(cwd)})
    return subprocess.run([sys.executable, str(STOP)], input=stdin, capture_output=True, text=True,
                          env=env, timeout=60)


def _dod_pass_recipe() -> str:
    """The block of scripts/dod.sh that writes .dod-pass, verbatim."""
    text = (ROOT / "scripts" / "dod.sh").read_text()
    m = re.search(r"^\{\n(.*?)\n\} > \"\$ROOT/\.dod-pass\"$", text, re.S | re.M)
    assert m, "scripts/dod.sh no longer writes .dod-pass the way dod_stop_gate.py expects"
    return "{\n" + m.group(1) + "\n} > \"$ROOT/.dod-pass\"\n"


def test_stop_gate_fingerprint_is_byte_identical_to_dod_sh(repo, tmp_path):
    (repo / "src/app.py").write_text("print('changed')\n")      # unstaged
    (repo / "edge.py").write_text("y = 1\n")
    _git(repo, "add", "edge.py")                                  # staged
    subprocess.run(["bash", "-c", "set -euo pipefail\n" + _dod_pass_recipe()], cwd=repo,
                   env={**os.environ, "ROOT": str(repo)}, check=True)
    sys.path.insert(0, str(HOOKS))
    try:
        import dod_stop_gate
    finally:
        sys.path.remove(str(HOOKS))
    first = (repo / ".dod-pass").read_text().splitlines()[0]
    assert first == dod_stop_gate.fingerprint(str(repo))
    assert re.fullmatch(r"[0-9a-f]{40}:[0-9a-f]{16}", first)


def test_stop_gate_reminds_once_then_stands_down(repo, tmp_path):
    (repo / "src/app.py").write_text("print('changed')\n")
    r = run_stop(tmp_path, repo)
    assert r.returncode == 2 and "src/app.py" in r.stderr and "dod.sh" in r.stderr
    assert run_stop(tmp_path, repo).returncode == 0                 # same session, same state
    assert run_stop(tmp_path, repo, session="s2").returncode == 2   # another session
    (repo / "src/app.py").write_text("print('changed again')\n")
    assert run_stop(tmp_path, repo).returncode == 2                 # a new change


def test_stop_gate_is_quiet_after_a_green_dod_on_this_state(repo, tmp_path):
    (repo / "src/app.py").write_text("print('changed')\n")
    subprocess.run(["bash", "-c", "set -euo pipefail\n" + _dod_pass_recipe()], cwd=repo,
                   env={**os.environ, "ROOT": str(repo)}, check=True)
    assert run_stop(tmp_path, repo).returncode == 0
    (repo / "src/app.py").write_text("print('after the run')\n")
    assert run_stop(tmp_path, repo).returncode == 2


def test_stop_gate_counts_new_untracked_code_and_angular_files(repo, tmp_path):
    (repo / "src" / "new.component.ts").write_text("export class X {}\n")
    r = run_stop(tmp_path, repo)
    assert r.returncode == 2 and "new.component.ts" in r.stderr


def test_stop_gate_ignores_prose_repos_without_dod_and_bad_input(repo, tmp_path):
    (repo / "docs/notes.md").write_text("# changed\n")
    assert run_stop(tmp_path, repo).returncode == 0
    (repo / "src/app.py").write_text("print('changed')\n")
    (repo / "scripts/dod.sh").unlink()
    assert run_stop(tmp_path, repo).returncode == 0
    assert run_stop(tmp_path, repo, raw="not json").returncode == 0
