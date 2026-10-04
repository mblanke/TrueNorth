"""Contract-suite fixtures.

* Makes ``ai-orchestrator/app/backends`` importable as ``_ai_orch.backends`` by running
  the loader in ``tests/ai_orchestrator/conftest.py`` (its top-level ``app`` package
  collides with the control-plane ``app``). Reused, not copied, so there is one loader.
* Blocks outbound network for every contract test: adapter construction and the
  null/mock implementations must work offline.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest

if not hasattr(sys.modules.get("_ai_orch.backends"), "_REGISTRY"):
    _loader = Path(__file__).resolve().parents[1] / "ai_orchestrator" / "conftest.py"
    _spec = importlib.util.spec_from_file_location("_tn_contracts_ai_orch_loader", _loader)
    _spec.loader.exec_module(importlib.util.module_from_spec(_spec))  # type: ignore[union-attr]


class NetworkBlockedError(RuntimeError):
    """Raised when a contract test attempts an outbound connection."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _refuse(*args, **kwargs):
        raise NetworkBlockedError(f"contract tests must not touch the network: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
