"""The restore drill's audit archive step runs without Celery (PR #168, CI `drill` lane).

scripts/backup/drill.sh runs scripts/backup/drill_audit_archive.py on the host's python3,
which has cryptography and SQLAlchemy but not Celery. Its export, verify and purge code
lives in worker/audit_jobs.py; worker/audit_tasks.py is only the Celery wrappers. These run
the drill script itself, end to end on SQLite, with celery, kombu and billiard made
unimportable, and check that the worker modules it reaches never import them.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRILL = ROOT / "scripts/backup/drill_audit_archive.py"
WORKER = ROOT / "control-plane/worker/worker"
BLOCKED = ("celery", "kombu", "billiard")

# Installed before anything else runs: importing a blocked package fails as on the runner.
_BLOCKER = f"""
import importlib.abc, sys
class _NoCelery(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {BLOCKED!r}:
            raise ModuleNotFoundError(f"No module named {{name!r}} (blocked: the drill runner has no Celery)")
        return None
sys.meta_path.insert(0, _NoCelery())
"""


def _run_drill(*argv: str) -> subprocess.CompletedProcess:
    code = _BLOCKER + f"""
import runpy
sys.argv = {[str(DRILL), *argv]!r}
runpy.run_path({str(DRILL)!r}, run_name="__main__")
"""
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)


def test_the_drill_seeds_verifies_and_extends_an_archive_without_celery(tmp_path):
    pytest.importorskip("cryptography")
    for step in ("seed", "verify", "extend", "verify"):
        run = _run_drill(step, str(tmp_path))
        assert run.returncode == 0, f"drill {step}: {run.stderr[-2000:]}"
        assert "celery" not in run.stderr.lower(), run.stderr[-2000:]


def _import_time_nodes(tree: ast.AST):
    """Every node that runs when the module is imported: not inside a function body (a
    function-local import, such as audit_verify's --db path, runs only when called, and the
    drill's calls are covered end to end above)."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                stack.append(child)


def _worker_imports(module: str) -> set[str]:
    tree = ast.parse((WORKER / f"{module}.py").read_text())
    out: set[str] = set()
    for node in _import_time_nodes(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # from . import x / from .x import y
                base = node.module or ""
                out.update(f".{base}" if base else f".{a.name}" for a in node.names)
            elif node.module:
                out.add(node.module)
    return out


def test_audit_jobs_and_what_it_imports_never_import_celery():
    seen: set[str] = set()
    todo = ["audit_jobs", "audit_signing", "audit_archive", "audit_export", "audit_verify", "tables"]
    while todo:
        mod = todo.pop()
        if mod in seen or not (WORKER / f"{mod}.py").is_file():
            continue
        seen.add(mod)
        for name in _worker_imports(mod):
            assert name.split(".")[0] not in BLOCKED, f"worker/{mod}.py imports {name}"
            assert name.lstrip(".") not in ("celery_app", "base_tasks", "task_plumbing", "audit_tasks"), \
                f"worker/{mod}.py imports {name}, which pulls in Celery"
            if name.startswith("."):
                todo.append(name.lstrip(".").split(".")[0])
    assert "audit_jobs" in seen


def test_audit_tasks_is_only_the_celery_wrappers():
    tasks = (WORKER / "audit_tasks.py").read_text()
    assert "from .audit_jobs import" in tasks
    defs = [n.name for n in ast.parse(tasks).body if isinstance(n, ast.FunctionDef)]
    assert defs == ["export_audit_days", "enforce_audit_retention"]
