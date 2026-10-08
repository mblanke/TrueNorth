"""API modules must import when laid out as in the Docker image.

The image copies control-plane/api/app to /app/app, so a module that looks "up" the
repository tree at import time (``Path(__file__).parents[4]``) raises IndexError there
while every repo-checkout test passes. That crash-looped the API container in CI once
(routers/software_catalogue.py). Same check as tests/worker/test_image_layout_import.py.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

API_PKG = Path(__file__).resolve().parents[2] / "control-plane" / "api" / "app"
_WORKER_CHECK = Path(__file__).resolve().parents[1] / "worker" / "test_image_layout_import.py"
_spec = importlib.util.spec_from_file_location("worker_image_layout_check", _WORKER_CHECK)
_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_check)


def test_no_unguarded_import_time_parent_lookups_in_api():
    offenders = {}
    for path in sorted(API_PKG.rglob("*.py")):
        lines = _check._unguarded_module_level_parents(ast.parse(path.read_text(), filename=str(path)))
        if lines:
            offenders[str(path.relative_to(API_PKG))] = lines
    assert not offenders, f"import-time Path.parents[n] breaks the /app/app image layout: {offenders}"


def test_software_catalogue_router_imports_at_the_image_path(monkeypatch):
    import types

    name = "app.routers.software_catalogue_image"
    mod = types.ModuleType(name)
    mod.__file__ = "/app/app/routers/software_catalogue.py"
    mod.__package__ = "app.routers"
    monkeypatch.setitem(sys.modules, name, mod)
    src = (API_PKG / "routers" / "software_catalogue.py").read_text()
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)  # noqa: S102 - our own module
    assert mod.catalogue_path() is not None
