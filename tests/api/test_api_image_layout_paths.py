"""API code must not index Path.parents past what the api image has.

The image copies control-plane/api to /app, so /app/app/routers/x.py has parents up to
[3]. A repo-relative lookup like ``Path(__file__).parents[4]`` passes every checkout test
and raises IndexError in the image. ARC² Studio did that on every call on staging (v1.1.0):
``runs_dir()`` built its repo default before reading ARC2_RUNS_DIR.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

API_APP = Path(__file__).resolve().parents[2] / "control-plane" / "api" / "app"


def _is_len_parents_guard(test: ast.AST) -> bool:
    """`len(<x>.parents) > n` (or >=, <, <=) anywhere in an if/ternary test."""
    for node in ast.walk(test):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "len" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Attribute) and arg.attr == "parents":
                return True
    return False


def _unguarded_parent_indexes(tree: ast.Module) -> list[int]:
    hits: list[int] = []

    def visit(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, (ast.If, ast.IfExp)) and _is_len_parents_guard(node.test):
            for child in ast.iter_child_nodes(node):
                visit(child, True)
            return
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "parents"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)
            and node.slice.value >= 3  # /app/app/<pkg>/<file>.py has parents[0..3]
            and not guarded
        ):
            hits.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    visit(tree, False)
    return hits


def test_no_unguarded_deep_parent_lookups_in_api_code():
    offenders = {}
    for path in sorted(API_APP.rglob("*.py")):
        lines = _unguarded_parent_indexes(ast.parse(path.read_text(), filename=str(path)))
        if lines:
            offenders[str(path.relative_to(API_APP))] = lines
    assert not offenders, f"Path.parents[n>=3] without a len() guard breaks the /app image layout: {offenders}"


@pytest.mark.parametrize("configured", [True, False])
def test_arc2_runs_dir_at_the_image_path(monkeypatch, tmp_path, configured):
    from app.routers import arc2_studio

    monkeypatch.setattr(arc2_studio, "__file__", "/app/app/routers/arc2_studio.py")
    if configured:
        monkeypatch.setenv("ARC2_RUNS_DIR", str(tmp_path))
        assert arc2_studio.runs_dir() == tmp_path
    else:
        monkeypatch.delenv("ARC2_RUNS_DIR", raising=False)
        assert arc2_studio.runs_dir() == Path("build") / "arc2"
