"""Worker modules must import when laid out as in the Docker image.

The image copies control-plane/worker/worker to /app/worker, so a module that looks
"up" the repository tree at import time (``Path(__file__).parents[3]``) raises
IndexError there while every repo-checkout test passes. That took down
worker-provision and worker-scenario in CI once (software_catalogue.py).
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

WORKER_PKG = Path(__file__).resolve().parents[2] / "control-plane" / "worker" / "worker"


def _unguarded_module_level_parents(tree: ast.Module) -> list[int]:
    """Line numbers of `<x>.parents[<n>]` evaluated at import time without a guard."""
    hits: list[int] = []

    def visit(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return  # runs later, not at import (class bodies are rare here; skip conservatively)
        if isinstance(node, ast.IfExp):
            guarded = True
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "parents"
            and not guarded
        ):
            hits.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    for stmt in tree.body:
        visit(stmt, False)
    return hits


def test_no_unguarded_import_time_parent_lookups_in_worker():
    offenders = {}
    for path in sorted(WORKER_PKG.rglob("*.py")):
        lines = _unguarded_module_level_parents(ast.parse(path.read_text(), filename=str(path)))
        if lines:
            offenders[str(path.relative_to(WORKER_PKG))] = lines
    assert not offenders, f"import-time Path.parents[n] breaks the /app/worker image layout: {offenders}"


def test_software_catalogue_imports_at_the_image_path(monkeypatch):
    name = "software_catalogue_image"
    mod = types.ModuleType(name)
    mod.__file__ = "/app/worker/software_catalogue.py"
    monkeypatch.setitem(sys.modules, name, mod)  # dataclasses look the module up
    src = (WORKER_PKG / "software_catalogue.py").read_text()
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)  # noqa: S102 - our own module
    assert mod.catalogue_path() is not None
