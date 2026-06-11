"""AI Orchestrator test configuration.

Registers ai-orchestrator/app/backends/ under the ``_ai_orch.backends``
namespace so tests can import from it without colliding with the
control-plane ``app`` package that the root conftest already loads.

Tests use::

    from _ai_orch.backends.mock import MockAIBackend
    from _ai_orch.backends import get_cloud_backend, _reset_backends
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_AI_APP = os.path.normpath(os.path.join(_HERE, "..", "..", "ai-orchestrator", "app"))
_AI_BACKENDS = os.path.join(_AI_APP, "backends")

_NS = "_ai_orch"
_NS_BACKENDS = f"{_NS}.backends"


def _load_submodule(name: str) -> None:
    """Load ai-orchestrator/app/backends/<name>.py under _ai_orch.backends.<name>."""
    full = f"{_NS_BACKENDS}.{name}"
    if full in sys.modules:
        return
    path = os.path.join(_AI_BACKENDS, f"{name}.py")
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod  # register BEFORE exec (handles potential circular refs)
    spec.loader.exec_module(mod)


# ── 1. Register _ai_orch parent stub ────────────────────────────────────
if _NS not in sys.modules:
    _orch_pkg = types.ModuleType(_NS)
    _orch_pkg.__path__ = [_AI_APP]  # type: ignore[assignment]
    _orch_pkg.__package__ = _NS
    sys.modules[_NS] = _orch_pkg

# ── 2. Register _ai_orch.backends package stub ──────────────────────────
if _NS_BACKENDS not in sys.modules:
    _backends_pkg = types.ModuleType(_NS_BACKENDS)
    _backends_pkg.__path__ = [_AI_BACKENDS]  # type: ignore[assignment]
    _backends_pkg.__package__ = _NS_BACKENDS
    sys.modules[_NS_BACKENDS] = _backends_pkg
else:
    _backends_pkg = sys.modules[_NS_BACKENDS]  # type: ignore[assignment]

# ── 3. Load leaf modules (dependency order: base first) ─────────────────
for _leaf in ("base", "mock", "openai", "anthropic", "vllm"):
    _load_submodule(_leaf)

# ── 4. Execute __init__.py into the _backends_pkg stub ──────────────────
_init_spec = importlib.util.spec_from_file_location(
    _NS_BACKENDS,
    os.path.join(_AI_BACKENDS, "__init__.py"),
    submodule_search_locations=[_AI_BACKENDS],
)
_init_spec.loader.exec_module(_backends_pkg)  # type: ignore[union-attr]

# Wire up on parent
sys.modules[_NS].backends = _backends_pkg  # type: ignore[attr-defined]
