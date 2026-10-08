"""The session coordinator (.coord/coord.py): leases, locks, checkpoints, scan, handoff.

Every behaviour test drives the real script in a subprocess against a throwaway git repo
with a linked worktree per session, pointed at by COORD_ROOT, so nothing here touches the
real checkout's state. The script must also stay importable by macOS /usr/bin/python3 (3.9).
"""

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".coord" / "coord.py"
SETTINGS = REPO / ".claude" / "settings.json"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

CONFIG = {
    "base_refs": ["main"],
    "protected_branches": ["main", "master"],
    "strict_leases": True,
    "stale_minutes": 90,
    "checkpoint_runs_gate": True,
    "gate_from_stage": 3,
    "checkpoint_gate_max_stage": 4,
    "shared_paths": ["docs/**"],
    "serialized_paths": ["src/models.py", "migrations/**"],
    "stages": {str(i): n for i, n in enumerate(["spec", "scaffolded", "functional", "tested", "integrated"])},
    "gates": {
        "3": {"desc": "module tests", "commands": ["{py} {tests}"], "manual": []},
        "4": {"desc": "module tests", "commands": ["{py} {tests}"], "manual": []},
    },
    "modules": [
        {"id": "alpha", "name": "Alpha", "stage": 2, "priority": 2, "paths": ["src/alpha/**", "tests/test_alpha.py"]},
        {"id": "beta", "name": "Beta", "stage": 2, "priority": 1, "paths": ["src/beta/**", "tests/test_beta.py"]},
    ],
}


class Repo:
    def __init__(self, base: Path):
        self.base = base
        self.main = base / "main"
        self.wt_a = base / "main" / ".claude" / "worktrees" / "a"
        self.wt_b = base / "wt-b"
        gitcfg = base / "gitconfig"
        gitcfg.write_text("[user]\n\tname = t\n\temail = t@example.invalid\n[init]\n\tdefaultBranch = main\n"
                          "[commit]\n\tgpgsign = false\n")
        self.env = {**os.environ, "COORD_ROOT": str(self.main), "COORD_NO_BG": "1",
                    "GIT_CONFIG_GLOBAL": str(gitcfg), "GIT_CONFIG_NOSYSTEM": "1"}
        self.main.mkdir()
        self._run_git(self.main, "init", "-q", "-b", "main")
        files = {
            "src/alpha/core.py": "A = 1\n", "src/beta/core.py": "B = 1\n", "src/models.py": "M = 1\n",
            "docs/readme.md": "doc\n", "tests/test_alpha.py": "print('ok')\n", "tests/test_beta.py": "print('ok')\n",
            ".gitignore": ".coord/state.sqlite*\n.coord/dashboard.*\n.claude/worktrees/\n",
        }
        for rel, text in files.items():
            p = self.main / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        (self.main / ".coord").mkdir()
        shutil.copy(SCRIPT, self.main / ".coord" / "coord.py")
        (self.main / ".coord" / "modules.json").write_text(json.dumps(CONFIG, indent=2))
        self._run_git(self.main, "add", "-A")
        self._run_git(self.main, "commit", "-q", "-m", "init")
        self._run_git(self.main, "worktree", "add", "-q", "-b", "claude/a", str(self.wt_a))
        self._run_git(self.main, "worktree", "add", "-q", "-b", "claude/b", str(self.wt_b))
        self._run_git(self.main, "branch", "claude/idle")

    def _run_git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True, text=True)

    def git(self, cwd, *args):
        return self._run_git(cwd, *args).stdout.strip()

    def coord(self, cwd, *args, payload=None, python=None):
        return subprocess.run([python or sys.executable, str(self.main / ".coord" / "coord.py"), *args],
                              cwd=cwd, env=self.env, capture_output=True, text=True,
                              input=json.dumps(payload) if payload is not None else None,
                              stdin=None if payload is not None else subprocess.DEVNULL)

    def guard(self, cwd, rel):
        return self.coord(cwd, "guard", payload={"cwd": str(cwd), "session_id": f"s-{Path(cwd).name}",
                                                 "tool_name": "Edit", "tool_input": {"file_path": str(Path(cwd) / rel)}})

    def write(self, cwd, rel, text):
        p = Path(cwd) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def db(self):
        con = sqlite3.connect(str(self.main / ".coord" / "state.sqlite"))
        con.row_factory = sqlite3.Row
        return con

    def age(self, cwd, minutes=200):
        """Make a worktree's lease and locks look idle for `minutes`."""
        con = self.db()
        old = "2000-01-01T00:00:00+00:00"
        top = os.path.realpath(cwd)
        con.execute("UPDATE sessions SET heartbeat=? WHERE cwd=?", (old, top))
        con.execute("UPDATE presence SET seen=? WHERE cwd=?", (old, top))
        con.execute("UPDATE locks SET seen=? WHERE cwd=?", (old, top))
        con.commit()


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def _load_real_coord(monkeypatch):
    monkeypatch.setenv("COORD_ROOT", str(REPO))
    spec = importlib.util.spec_from_file_location("coord_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- config + matching
def test_real_config_maps_truenorth_paths(monkeypatch):
    coord = _load_real_coord(monkeypatch)
    cfg = coord.cfg()
    ids = {m["id"] for m in cfg["modules"]}
    assert ids == {"platform-core", "web-shell", "infra", "scenario-engine", "ai-arc2", "lms", "curriculum",
                   "detection", "reporting-aar", "content", "ranges-labs", "provisioners", "telemetry",
                   "scheduling", "noise-engine", "wiki-helpdesk", "greyspace"}
    expect = {
        "control-plane/worker/worker/exercise_run.py": ("module", "scenario-engine"),
        "control-plane/worker/worker/inject_dispatch.py": ("module", "scenario-engine"),
        "control-plane/worker/worker/aar_tasks.py": ("module", "reporting-aar"),
        "control-plane/worker/worker/telemetry_tasks.py": ("module", "telemetry"),
        "control-plane/worker/worker/range_alloc.py": ("module", "ranges-labs"),
        "control-plane/worker/worker/greyspace.py": ("module", "greyspace"),
        "control-plane/worker/worker/provisioners/vsphere_api.py": ("module", "provisioners"),
        "control-plane/api/app/routers/greyspace.py": ("module", "greyspace"),
        "control-plane/api/app/scheduler/service.py": ("module", "scheduling"),
        "control-plane/api/app/auth.py": ("module", "platform-core"),
        "control-plane/web/src/app/features/noise/noise.component.ts": ("module", "noise-engine"),
        "control-plane/web/src/app/core/services/api.service.ts": ("module", "web-shell"),
        "infra/platform/moodle/Dockerfile": ("module", "lms"),
        "infra/vsphere/README.md": ("module", "infra"),
        "tools/arc2/run.py": ("module", "ai-arc2"),
        "tests/api/test_greyspace.py": ("module", "greyspace"),
        "tests/api/test_crud.py": ("module", "platform-core"),
        "control-plane/api/app/models.py": ("serialized", "control-plane/api/app/models.py"),
        "control-plane/api/alembic/versions/x.py": ("serialized", "control-plane/api/alembic/**"),
        "docs/interfaces/openapi.json": ("serialized", "docs/interfaces/**"),
        "control-plane/web/src/app/app.routes.ts": ("serialized", "control-plane/web/src/app/app.routes.ts"),
        ".dod-ruff-baseline": ("serialized", ".dod-*-baseline"),
        "docs/adr/0001-adapter-registry.md": ("shared", None),
        "RESUME.md": ("shared", None),
    }
    for rel, want in expect.items():
        assert coord.classify(cfg, rel) == want, rel
    for m in cfg["modules"]:
        assert m["paths"], m["id"]
        assert m["stage"] == 4, m["id"]


def test_most_specific_pattern_wins(monkeypatch):
    coord = _load_real_coord(monkeypatch)
    cfg = {"modules": [{"id": "wide", "paths": ["a/**"]}, {"id": "narrow", "paths": ["a/b/**"]},
                       {"id": "file", "paths": ["a/b/c.py"]}, {"id": "glob", "paths": ["a/b/test_*.py"]}]}
    assert coord.owner_of(cfg, "a/x.py")["id"] == "wide"
    assert coord.owner_of(cfg, "a/b/x.py")["id"] == "narrow"
    assert coord.owner_of(cfg, "a/b/c.py")["id"] == "file"
    assert coord.owner_of(cfg, "a/b/test_q.py")["id"] == "glob"
    assert coord.owner_of(cfg, "ab/x.py") is None


def test_hook_settings_wrap_every_command():
    hooks = json.loads(SETTINGS.read_text())["hooks"]
    assert set(hooks) == {"PreToolUse", "SessionStart", "Stop"}
    assert hooks["PreToolUse"][0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    for event in hooks.values():
        cmd = event[0]["hooks"][0]["command"]
        assert '[ -f "$f" ] || exit 0;' in cmd
    assert hooks["PreToolUse"][0]["hooks"][0]["command"].endswith('python3 "$f" guard')


# ---------------------------------------------------------------- leases + guard
def test_claim_refused_on_protected_branch(repo):
    r = repo.coord(repo.main, "claim", "alpha")
    assert r.returncode != 0
    assert "protected" in r.stderr


def test_strict_lease_blocks_other_modules(repo):
    assert repo.coord(repo.wt_a, "claim", "alpha").returncode == 0
    assert repo.guard(repo.wt_a, "src/alpha/core.py").returncode == 0
    assert repo.guard(repo.wt_a, "docs/readme.md").returncode == 0          # shared
    assert repo.guard(repo.wt_a, "NOTES.txt").returncode == 0               # unowned
    r = repo.guard(repo.wt_a, "src/beta/core.py")
    assert r.returncode == 2
    assert "leased to 'alpha'" in r.stderr and "handoff add beta" in r.stderr

    assert repo.coord(repo.wt_b, "claim", "alpha").returncode != 0         # one live lease per module
    r = repo.guard(repo.wt_b, "src/alpha/core.py")                         # unleased dir, leased module
    assert r.returncode == 2 and "leased by another session" in r.stderr
    assert repo.guard(repo.wt_b, "src/beta/core.py").returncode == 0       # unleased module: allowed


def test_edit_inside_another_leased_worktree_is_blocked(repo):
    repo.coord(repo.wt_a, "claim", "alpha")
    r = repo.coord(repo.main, "guard", payload={"cwd": str(repo.main), "tool_input": {
        "file_path": str(repo.wt_a / "docs" / "readme.md")}})
    assert r.returncode == 2 and "inside the worktree leased to alpha" in r.stderr


def test_stale_lease_can_be_taken_over(repo):
    repo.coord(repo.wt_a, "claim", "alpha")
    repo.age(repo.wt_a)
    r = repo.coord(repo.wt_b, "claim", "alpha")
    assert r.returncode == 0, r.stderr
    assert repo.guard(repo.wt_b, "src/alpha/core.py").returncode == 0
    listing = repo.coord(repo.main, "session", "list").stdout
    assert str(os.path.realpath(repo.wt_b)) in listing and str(os.path.realpath(repo.wt_a)) not in listing


# ---------------------------------------------------------------- locks
def test_serialized_lock_acquire_block_release(repo):
    repo.coord(repo.wt_a, "claim", "alpha")
    repo.coord(repo.wt_b, "claim", "beta")
    assert repo.guard(repo.wt_a, "src/models.py").returncode == 0
    r = repo.guard(repo.wt_b, "src/models.py")
    assert r.returncode == 2
    assert "locked by alpha" in r.stderr and "handoff add alpha" in r.stderr
    assert repo.guard(repo.wt_a, "src/models.py").returncode == 0          # holder may keep editing
    assert repo.guard(repo.wt_b, "migrations/0002.py").returncode == 0     # a different serialized key

    repo.write(repo.wt_a, "src/models.py", "M = 2\n")
    r = repo.coord(repo.wt_a, "checkpoint")
    assert r.returncode == 0, r.stderr
    assert "src/models.py" in repo.git(repo.wt_a, "show", "--name-only", "--format=", "HEAD")
    assert repo.guard(repo.wt_b, "src/models.py").returncode == 0          # released by the checkpoint


def test_stale_lock_is_taken_over_and_release_drops_locks(repo):
    repo.coord(repo.wt_a, "claim", "alpha")
    repo.guard(repo.wt_a, "src/models.py")
    repo.age(repo.wt_a)
    assert repo.guard(repo.wt_b, "src/models.py").returncode == 0
    holder = repo.db().execute("SELECT cwd FROM locks WHERE key='src/models.py'").fetchone()[0]
    assert holder == os.path.realpath(repo.wt_b)
    repo.coord(repo.wt_b, "claim", "beta")
    assert "dropped 1 locks" in repo.coord(repo.wt_b, "release").stdout


# ---------------------------------------------------------------- checkpoint
def test_checkpoint_stages_only_owned_files(repo):
    repo.coord(repo.wt_a, "claim", "alpha")
    repo.write(repo.wt_a, "src/alpha/new.py", "N = 1\n")
    repo.write(repo.wt_a, "src/alpha/core.py", "A = 2\n")
    repo.write(repo.wt_a, "src/beta/core.py", "B = 2\n")      # foreign
    repo.write(repo.wt_a, "docs/readme.md", "doc 2\n")        # shared
    repo.write(repo.wt_a, "src/models.py", "M = 3\n")         # serialized, no lock held
    r = repo.coord(repo.wt_a, "checkpoint", "-m", "first")
    assert r.returncode == 0, r.stderr
    committed = set(repo.git(repo.wt_a, "show", "--name-only", "--format=", "HEAD").splitlines())
    assert committed == {"src/alpha/new.py", "src/alpha/core.py"}
    assert repo.git(repo.wt_a, "log", "-1", "--format=%s") == "wip(alpha): first"
    dirty = repo.git(repo.wt_a, "status", "--porcelain")
    assert "src/beta/core.py" in dirty and "docs/readme.md" in dirty and "src/models.py" in dirty
    r = repo.coord(repo.wt_a, "checkpoint")
    assert "nothing owned by alpha" in r.stdout
    (repo.wt_a / "src" / "alpha" / "new.py").unlink()                      # deletions are staged too
    assert repo.coord(repo.wt_a, "checkpoint").returncode == 0
    assert repo.git(repo.wt_a, "show", "--name-status", "--format=", "HEAD") == "D\tsrc/alpha/new.py"


def test_checkpoint_refusals(repo):
    r = repo.coord(repo.main, "checkpoint")
    assert r.returncode == 1 and "no lease" in r.stderr

    # a lease row pointing at the protected main checkout (claim refuses to create one)
    repo.coord(repo.wt_a, "claim", "alpha")
    con = repo.db()
    con.execute("UPDATE sessions SET cwd=? WHERE module='alpha'", (os.path.realpath(repo.main),))
    con.commit()
    repo.write(repo.main, "src/alpha/core.py", "A = 9\n")
    head = repo.git(repo.main, "rev-parse", "HEAD")
    r = repo.coord(repo.main, "checkpoint")
    assert r.returncode == 1 and "protected" in r.stderr
    assert repo.git(repo.main, "rev-parse", "HEAD") == head


def test_checkpoint_secret_scan_and_quiet_mode(repo):
    repo.coord(repo.wt_a, "claim", "alpha")
    repo.write(repo.wt_a, "src/alpha/keys.py", "KEY = 'AKIA" + "ABCDEFGHIJKLMNOP'\n")
    head = repo.git(repo.wt_a, "rev-parse", "HEAD")
    r = repo.coord(repo.wt_a, "checkpoint", "--quiet", payload={"cwd": str(repo.wt_a)})
    assert r.returncode == 0                                   # the Stop hook never blocks
    assert "possible secret" in json.loads(r.stdout)["systemMessage"]
    assert repo.git(repo.wt_a, "rev-parse", "HEAD") == head


def test_checkpoint_runs_module_gate_from_stage_3(repo):
    repo.coord(repo.wt_a, "claim", "alpha")
    repo.coord(repo.main, "module", "set", "alpha", "--stage", "3")
    repo.write(repo.wt_a, "tests/test_alpha.py", "raise SystemExit(3)\n")
    head = repo.git(repo.wt_a, "rev-parse", "HEAD")
    r = repo.coord(repo.wt_a, "checkpoint")
    assert r.returncode == 1 and "gate FAIL" in r.stderr
    assert repo.git(repo.wt_a, "rev-parse", "HEAD") == head
    repo.write(repo.wt_a, "tests/test_alpha.py", "print('fixed')\n")
    r = repo.coord(repo.wt_a, "checkpoint")
    assert r.returncode == 0, r.stderr
    assert "gate PASS" in repo.git(repo.wt_a, "log", "-1", "--format=%b")


# ---------------------------------------------------------------- scan / brief / handoff / dashboard
def test_scan_maps_worktrees_and_branches(repo):
    repo.write(repo.wt_a, "src/alpha/x.py", "X = 1\n")
    repo.git(repo.wt_a, "add", "-A")
    repo.git(repo.wt_a, "commit", "-q", "-m", "alpha work")
    repo.write(repo.wt_b, "src/beta/y.py", "Y = 1\n")
    repo.write(repo.wt_b, "src/models.py", "M = 5\n")
    data = json.loads(repo.coord(repo.main, "scan", "--json").stdout)
    by_name = {it["name"]: it for it in data["items"]}
    a, b = by_name["a"], by_name["wt-b"]
    assert a["modules"] == {"alpha": 1} and a["ahead"] == 1 and a["flags"] == []
    assert b["modules"] == {"beta": 1} and b["serialized"] == ["src/models.py"] and "dirty" in b["flags"]
    assert by_name["claude/idle"]["flags"] == ["empty"]


def test_session_start_brief_suggests_claim(repo):
    repo.write(repo.wt_b, "src/beta/y.py", "Y = 1\n")
    r = repo.coord(repo.wt_b, "session-start", payload={"cwd": str(repo.wt_b), "session_id": "x"})
    assert r.returncode == 0
    assert "No lease" in r.stdout and "coord claim beta" in r.stdout
    repo.coord(repo.wt_b, "claim", "beta")
    r = repo.coord(repo.wt_b, "session-start", payload={"cwd": str(repo.wt_b)})
    assert "Leased to module **beta**" in r.stdout


def test_handoff_add_list_done(repo):
    repo.coord(repo.wt_a, "claim", "alpha")
    r = repo.coord(repo.wt_a, "handoff", "add", "beta", "expose B on the API", "--why", "alpha needs it")
    assert r.returncode == 0 and "#1" in r.stdout
    assert repo.coord(repo.main, "handoff", "add", "nope", "x").returncode != 0
    listing = repo.coord(repo.main, "handoff", "list").stdout
    assert "expose B on the API" in listing and "alpha" in listing
    repo.coord(repo.wt_b, "claim", "beta")
    brief = repo.coord(repo.wt_b, "session-start", payload={"cwd": str(repo.wt_b)}).stdout
    assert "#1 from alpha: expose B on the API" in brief
    assert repo.coord(repo.wt_b, "handoff", "done", "1").returncode == 0
    assert "no handoff items" in repo.coord(repo.main, "handoff", "list").stdout
    assert "done" in repo.coord(repo.main, "handoff", "list", "--all").stdout


def test_dashboard_and_status(repo):
    repo.coord(repo.wt_a, "claim", "alpha")
    repo.guard(repo.wt_a, "src/models.py")
    d = json.loads(repo.coord(repo.main, "dashboard", "--json").stdout)
    alpha = next(m for m in d["modules"] if m["id"] == "alpha")
    assert alpha["lease"]["branch"] == "claude/a" and alpha["locks"][0]["key"] == "src/models.py"
    assert repo.coord(repo.main, "dashboard").returncode == 0
    page = (repo.main / ".coord" / "dashboard.html").read_text()
    assert "prefers-color-scheme: dark" in page and "Alpha" in page
    r = repo.coord(repo.main, "status")
    assert r.returncode == 0 and "alpha" in r.stdout
    assert repo.git(repo.main, "status", "--porcelain") == ""             # state never dirties main


def test_hook_wrapper_preserves_exit_2_and_noops_elsewhere(repo, tmp_path):
    cmd = json.loads(SETTINGS.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    repo.coord(repo.wt_a, "claim", "alpha")
    payload = json.dumps({"cwd": str(repo.wt_a), "tool_input": {"file_path": str(repo.wt_a / "src/beta/core.py")}})
    r = subprocess.run(["sh", "-c", cmd], cwd=repo.wt_a, env=repo.env, input=payload, capture_output=True, text=True)
    assert r.returncode == 2 and "BLOCKED" in r.stderr
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    r = subprocess.run(["sh", "-c", cmd], cwd=elsewhere, env=repo.env, input=payload, capture_output=True, text=True)
    assert r.returncode == 0


# ---------------------------------------------------------------- macOS system python
SYSTEM_PY = "/usr/bin/python3"


@pytest.mark.skipif(not os.path.exists(SYSTEM_PY), reason="no /usr/bin/python3")
def test_runs_on_system_python(repo, tmp_path):
    r = subprocess.run([SYSTEM_PY, "-c", "import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)",
                        str(SCRIPT), str(tmp_path / "coord.pyc")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert repo.coord(repo.wt_a, "claim", "alpha", python=SYSTEM_PY).returncode == 0
    assert repo.guard(repo.wt_a, "src/alpha/core.py").returncode == 0
    r = repo.coord(repo.main, "modules", python=SYSTEM_PY)
    assert r.returncode == 0 and "alpha" in r.stdout
