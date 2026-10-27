"""scripts/taz_guard.py: the CI check that keeps a TAZ pull request inside its task card.

Each test builds a small repository with a `main`, a Claude-written `taz-base/7` (the
oracle test) and a TAZ branch on top, then runs the guard as CI would. Every rule must
fail its own red-team change, and an honest change must pass.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("taz_guard", ROOT / "scripts" / "taz_guard.py")
guard = importlib.util.module_from_spec(_spec)
sys.modules["taz_guard"] = guard  # dataclasses look their module up
_spec.loader.exec_module(guard)

POLICY = json.loads((ROOT / "tools" / "taz" / "hooks" / "policy.json").read_text())
ORACLE = "control-plane/web/src/app/features/x/x.component.spec.ts"
CARD = {"allowed_paths": ["control-plane/web/src/app/features/x/**"], "oracle_tests": [ORACLE],
        "diff_budget": 40, "base_ref": "taz-base/7"}


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                        "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _write(repo: Path, files: dict) -> None:
    for rel, text in files.items():
        p = repo / rel
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _commit(repo: Path, files: dict, msg: str) -> None:
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _commit(r, {
        "control-plane/web/src/app/features/x/x.component.ts": "export class X {}\n",
        "control-plane/web/package-lock.json": "{}\n",
        "control-plane/api/app/rbac.py": "ROLES = []\n",
        ".dod-ruff-baseline": "17\n",
        ".dod-mosa-baseline": json.dumps({"worker_raw_sql": 0, "lines_api_models_py": 1816}) + "\n",
        "docs/readme.md": "# docs\n",
    }, "main")
    _git(r, "checkout", "-q", "-b", "taz-base/7")
    _commit(r, {ORACLE: "describe('X', () => { it('works', () => { expect(1).toBe(1); }); });\n"}, "oracle")
    _git(r, "checkout", "-q", "-b", "taz/7")
    return r


def run(repo: Path, card: dict = CARD):
    return guard.check(repo, "main", "HEAD", guard.validate_card(card), POLICY)


def failed(rep) -> set[str]:
    return {c for c, _ in rep.failures}


def test_an_honest_change_passes(repo):
    _commit(repo, {"control-plane/web/src/app/features/x/x.component.ts":
                   "export class X {\n  works = true;\n}\n"}, "implement")
    rep = run(repo)
    assert rep.failures == []


def test_scope_outside_allowed_paths(repo):
    _commit(repo, {"control-plane/web/src/app/features/y/y.ts": "export const y = 1;\n"}, "stray")
    assert failed(run(repo)) == {"scope"}


def test_scope_protected_path_even_when_allowed(repo):
    _commit(repo, {"control-plane/api/app/rbac.py": "ROLES = ['admin']\n"}, "rbac")
    card = {**CARD, "allowed_paths": ["**"]}
    rep = run(repo, card)
    assert "scope" in failed(rep) and any("protected" in m for _, m in rep.failures)


def test_scope_workflow_and_guard_are_protected(repo):
    _commit(repo, {".github/workflows/taz-guard.yml": "on: push\n", "scripts/taz_guard.py": "pass\n"}, "ci")
    rep = run(repo, {**CARD, "allowed_paths": ["**"]})
    assert len([m for c, m in rep.failures if c == "scope"]) == 2


def test_budget(repo):
    body = "".join(f"export const v{i} = {i};\n" for i in range(41))
    _commit(repo, {"control-plane/web/src/app/features/x/big.ts": body}, "big")
    assert failed(run(repo)) == {"budget"}


def test_binary_file_fails_the_budget(repo):
    p = repo / "control-plane/web/src/app/features/x/logo.png"
    p.write_bytes(b"\x89PNG\x00\x01\x02")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bin")
    assert failed(run(repo)) == {"budget"}


def test_oracle_edited(repo):
    _commit(repo, {ORACLE: "describe('X', () => { xit('works', () => {}); });\n"}, "weaken oracle")
    assert "oracle" in failed(run(repo))


def test_oracle_deleted(repo):
    _commit(repo, {ORACLE: None}, "drop oracle")
    assert "oracle" in failed(run(repo))


def test_oracle_without_base_ref_must_be_untouched(repo):
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {ORACLE: "describe('X', () => {});\n"}, "oracle on main")
    _git(repo, "checkout", "-q", "-b", "taz/8")
    _commit(repo, {ORACLE: "describe('X', () => { it('x', () => {}); });\n"}, "edit")
    card = {**CARD, "base_ref": None}
    rep =guard.check(repo, "main", "HEAD", guard.validate_card(card), POLICY)
    assert "oracle" in failed(rep)


def test_oracle_missing_from_base_ref(repo):
    card = {**CARD, "oracle_tests": ["control-plane/web/src/app/features/x/missing.spec.ts"]}
    _commit(repo, {"control-plane/web/src/app/features/x/x.component.ts": "export class X { a = 1; }\n"}, "x")
    assert "oracle" in failed(run(repo, card))


@pytest.mark.parametrize("line", [
    "@pytest.mark.skip(reason='later')", "@pytest.mark.xfail", "pytest.skip('no')",
    "x = 1  # noqa: F401", "y = f()  # type: ignore[attr-defined]",
    "// eslint-disable-next-line no-console", "/* eslint-disable */", "// @ts-ignore",
    "xit('works', () => {});", "fdescribe('X', () => {});", "fit('only this', () => {});",
])
def test_new_suppression_lines(repo, line):
    _commit(repo, {"control-plane/web/src/app/features/x/x.component.ts": f"export class X {{}}\n{line}\n"},
            "suppress")
    assert failed(run(repo)) == {"suppression"}


def test_moved_suppression_and_prose_are_not_new(repo):
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"control-plane/web/src/app/features/x/x.component.ts":
                   "// eslint-disable-next-line no-console\nconsole.log(1);\nexport class X {}\n"}, "existing")
    _git(repo, "checkout", "-q", "-b", "taz/9")
    _commit(repo, {"control-plane/web/src/app/features/x/x.component.ts":
                   "export class X {}\n// eslint-disable-next-line no-console\nconsole.log(1);\n"}, "move")
    card = {**CARD, "base_ref": None, "oracle_tests": [], "allowed_paths": CARD["allowed_paths"] + ["docs/**"]}
    rep = guard.check(repo, "main", "HEAD", guard.validate_card(card), POLICY)
    assert rep.failures == []
    _commit(repo, {"docs/readme.md": "Never add `# noqa` or eslint-disable.\n"}, "prose")
    assert guard.check(repo, "main", "HEAD", guard.validate_card(card), POLICY).failures == []


def test_baseline_raised(repo):
    _commit(repo, {".dod-ruff-baseline": "18\n"}, "raise ruff")
    assert failed(run(repo)) == {"baseline"}


def test_mosa_baseline_raised_or_new_key(repo):
    _commit(repo, {".dod-mosa-baseline": json.dumps({"worker_raw_sql": 0, "lines_api_models_py": 1816,
                                                      "new_debt": 1})}, "new key")
    assert failed(run(repo)) == {"baseline"}


def test_baseline_lowered_is_the_ratchet_working(repo):
    _commit(repo, {".dod-ruff-baseline": "16\n"}, "lower")
    assert run(repo).failures == []


def test_lockfile_changed(repo):
    _commit(repo, {"control-plane/web/package-lock.json": '{"x": 1}\n'}, "lock")
    rep = run(repo, {**CARD, "allowed_paths": ["**"]})
    assert "lockfile" in failed(rep)


def test_symlink_added(repo):
    (repo / "control-plane/web/src/app/features/x/link.ts").symlink_to("/etc/passwd")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "link")
    assert "mode" in failed(run(repo))


# ── card resolution ───────────────────────────────────────────────────────

ISSUE_BODY = "Goal: make X work.\n\n```taz-card\n" + json.dumps(CARD) + "\n```\n"


def issue(**kw):
    base = {"number": 7, "body": ISSUE_BODY, "labels": [{"name": "taz-ready"}], "user": {"login": "mblanke"}}
    base.update(kw)
    return base


def resolve(body, iss=None, pr_author="truenorth-taz", allow_inline=False):
    calls = []

    def fetch(repo, number, token):
        calls.append((repo, number))
        return iss if iss is not None else issue()

    card = guard.resolve_card(body, "mblanke/TrueNorth", pr_author, {"mblanke"}, allow_inline, fetch=fetch)
    return card, calls


def test_card_from_linked_issue():
    card, calls = resolve("Implements the card.\n\ntaz-card: #7\n")
    assert calls == [("mblanke/TrueNorth", 7)] and card["allowed_paths"] == CARD["allowed_paths"]
    card, _ = resolve("taz-card: https://github.com/mblanke/TrueNorth/issues/7")
    assert card["issue"] == 7


def test_card_labeled_json_block_form():
    body = "taz-card:\n```json\n" + json.dumps(CARD) + "\n```\n"
    card, _ = resolve("taz-card: #7", iss=issue(body=body))
    assert card["diff_budget"] == 40


@pytest.mark.parametrize("iss,why", [
    (issue(labels=[]), "taz-ready"),
    (issue(user={"login": "truenorth-taz"}), "trusted"),
    (issue(body="no card here"), "exactly one"),
    (issue(body=ISSUE_BODY + ISSUE_BODY), "exactly one"),
    (issue(pull_request={"url": "x"}), "pull request"),
])
def test_card_issue_must_be_a_ready_trusted_card(iss, why):
    with pytest.raises(guard.GuardInputError, match=why):
        resolve("taz-card: #7", iss=iss)


def test_inline_card_must_match_the_issue_and_is_not_enough_alone():
    widened = {**CARD, "allowed_paths": ["**"]}
    body = "taz-card: #7\n\n```taz-card\n" + json.dumps(widened) + "\n```\n"
    with pytest.raises(guard.GuardInputError, match="differs"):
        resolve(body)
    same = "taz-card: #7\n\n```taz-card\n" + json.dumps(CARD) + "\n```\n"
    assert resolve(same)[0]["allowed_paths"] == CARD["allowed_paths"]
    inline_only = "```taz-card\n" + json.dumps(widened) + "\n```\n"
    with pytest.raises(guard.GuardInputError, match="no task card"):
        resolve(inline_only)
    assert resolve(inline_only, allow_inline=True)[0]["allowed_paths"] == ["**"]


@pytest.mark.parametrize("body", ["taz-card: https://github.com/evil/TrueNorth/issues/7",
                                  "taz-card: #7\ntaz-card: #8", "nothing"])
def test_card_link_errors(body):
    with pytest.raises(guard.GuardInputError):
        resolve(body)


@pytest.mark.parametrize("card", [
    {}, {"allowed_paths": []}, {"allowed_paths": ["x"], "diff_budget": 0},
    {"allowed_paths": ["x"], "diff_budget": True}, {"allowed_paths": ["x"], "base_ref": "../x"},
    {"allowed_paths": ["x"], "oracle_tests": "x"},
])
def test_invalid_cards(card):
    with pytest.raises(guard.GuardInputError):
        guard.validate_card(card)


# ── CLI as CI runs it ─────────────────────────────────────────────────────


def test_main_with_an_event_file(repo, tmp_path, capsys):
    _commit(repo, {"control-plane/web/src/app/features/x/x.component.ts": "export class X { ok = 1; }\n"}, "x")
    event = {"pull_request": {"body": "taz-card: #7", "user": {"login": "truenorth-taz"},
                              "base": {"sha": _git(repo, "rev-parse", "main")}}}
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event))
    rc = guard.main(["--repo-dir", str(repo), "--event", str(ev), "--head", "HEAD",
                     "--repo", "mblanke/TrueNorth"], fetch=lambda *a: issue())
    assert rc == 0, capsys.readouterr().out
    _commit(repo, {"README.md": "stray\n"}, "stray")
    rc = guard.main(["--repo-dir", str(repo), "--event", str(ev), "--head", "HEAD",
                     "--repo", "mblanke/TrueNorth"], fetch=lambda *a: issue())
    out = capsys.readouterr().out
    assert rc == 1 and "::error title=taz-guard scope::" in out


def test_main_input_error_is_exit_2(repo, tmp_path, capsys):
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"pull_request": {"body": "", "base": {"sha": "main"}}}))
    rc = guard.main(["--repo-dir", str(repo), "--event", str(ev), "--head", "HEAD"], fetch=lambda *a: issue())
    assert rc == 2 and "no task card" in capsys.readouterr().out


def test_workflow_runs_the_base_branch_guard_without_executing_the_pr():
    wf = (ROOT / ".github" / "workflows" / "taz-guard.yml").read_text()
    assert "pull_request_target:" in wf
    assert "ref: ${{ github.event.pull_request.base.sha }}" in wf
    assert "refs/pull/${PR_NUMBER}/head:refs/taz/head" in wf
    assert "head.sha" not in wf and "head.ref }}" not in wf.split("steps:")[1]
    assert "github.event.pull_request.body" not in wf  # read from the event file, never interpolated
    assert "contains(github.event.pull_request.head.repo.full_name, 'taz')" in wf
    assert "startsWith(github.event.pull_request.head.ref, 'taz/')" in wf
