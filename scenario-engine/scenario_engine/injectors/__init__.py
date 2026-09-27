"""TrueNorth Range — Injector registry (canonical).

This is the single source of truth for scenario injectors. It supports
two calling conventions:

1. **Registry / RangeContext** (preferred by the scenario runner):
   ``get_injector("dns_spike").execute(params, ctx)`` returns an
   ``InjectResult``. Injectors are registered via ``@register_injector``
   and discovered automatically.

2. **BaseInjector(params)** (used by tests and standalone callers):
   ``DnsSpikeInjector({"domains": [...]}).execute(context)`` returns a
   ``dict``. Every registered injector also subclasses ``BaseInjector``
   so both conventions work on the same class.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Range context / result (registry convention) ────────────────────────


@dataclass
class RangeContext:
    """Context about the range an inject is targeting."""

    range_id: str
    tenant_id: str
    vms: list[dict] = field(default_factory=list)  # [{"name": ..., "ip": ..., "role": ...}]
    networks: list[dict] = field(default_factory=list)
    opensearch_url: str = "http://opensearch:9200"
    exercise_id: str | None = None


@dataclass
class InjectResult:
    """Result of an inject execution (registry convention)."""

    success: bool
    action: str
    detail: str = ""
    artifacts: list[str] | None = None
    telemetry: list[dict] | None = None  # events to ship to OpenSearch
    mitre_technique: str | None = None
    raw: dict[str, Any] | None = None  # full dict result from BaseInjector


# ── BaseInjector (params-in-constructor convention) ─────────────────────


class BaseInjector(ABC):
    """Base class accepting params at construction.

    Subclasses define ``name`` and implement ``execute(context)`` returning
    a dict. ``action_name`` (the registry key) defaults to ``name`` but may
    be overridden.
    """

    name: str = ""
    description: str = ""
    required_params: list[str] = []

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}

    def validate_params(self) -> None:  # noqa: B027 — optional override
        pass

    @abstractmethod
    def execute(self, context: dict[str, Any]) -> dict[str, Any]: ...

    @property
    def action_name(self) -> str:
        return self.name


# ── Injector (registry convention) ──────────────────────────────────────


class Injector(ABC):
    """Base class for the registry convention.

    Subclasses define ``action_name`` and implement
    ``execute(params, ctx) -> InjectResult``.
    """

    @property
    @abstractmethod
    def action_name(self) -> str: ...

    @abstractmethod
    def execute(self, params: dict, ctx: RangeContext) -> InjectResult: ...


# ── Registry ────────────────────────────────────────────────────────────

_INJECTOR_REGISTRY: dict[str, type[BaseInjector]] = {}


def register_injector(cls: type[BaseInjector]) -> type[BaseInjector]:
    """Decorator to register an injector class under its ``action_name``.

    Key derivation tolerates both base classes: this package's ``BaseInjector``
    exposes ``action_name``; the plain ``base.BaseInjector`` subclasses only
    carry ``name``.
    """
    instance = cls()
    key = getattr(instance, "action_name", "") or getattr(instance, "name", "")
    if not key:
        raise ValueError(f"Injector {cls.__name__} has no action_name/name")
    _INJECTOR_REGISTRY[key] = cls
    logger.info("Registered injector: %s", key)
    return cls


def get_injector(action: str) -> BaseInjector | None:
    """Look up an injector by action name (returns an instance)."""
    cls = _INJECTOR_REGISTRY.get(action)
    return cls() if cls else None


def list_injectors() -> list[str]:
    """List all registered injector action names."""
    return sorted(_INJECTOR_REGISTRY.keys())


def run_inject(action: str, params: dict, ctx: RangeContext) -> InjectResult:
    """Execute an inject by action name, returning an ``InjectResult``.

    Bridges the two conventions: calls ``BaseInjector.execute(context)``
    and wraps the dict result into an ``InjectResult``. If no injector is
    registered for *action*, returns a failed ``InjectResult``.
    """
    inj = get_injector(action)
    if inj is None:
        return InjectResult(success=False, action=action, detail=f"No injector registered for action '{action}'")
    try:
        inj.validate_params()
        raw = inj.execute(
            {
                "range_id": ctx.range_id,
                "tenant_id": ctx.tenant_id,
                "vms": ctx.vms,
                "networks": ctx.networks,
                "opensearch_url": ctx.opensearch_url,
                "exercise_id": ctx.exercise_id,
            }
        )
    except Exception as exc:  # noqa: BLE001 — injectors must not crash the run
        logger.exception("Injector %s failed", action)
        return InjectResult(success=False, action=action, detail=f"Injector error: {exc}")
    return InjectResult(
        success=bool(raw.get("success", True)),
        action=action,
        detail=str(raw.get("detail", raw.get("injector", ""))),
        mitre_technique=raw.get("technique_id"),
        raw=raw,
    )


def _auto_discover() -> None:
    """Import all injector modules and register every concrete injector class.

    Registration is done here rather than by per-module decorators: every
    injector module subclasses ``base.BaseInjector`` and none carried the
    ``@register_injector`` decorator, so the registry was silently empty and
    ``run_inject`` failed for every action. Scanning at discovery time also
    means a future injector cannot forget to register itself.
    """
    import inspect
    import pathlib

    from .base import BaseInjector as _PlainBase

    pkg_dir = pathlib.Path(__file__).parent
    for f in pkg_dir.glob("*.py"):
        if f.name.startswith("_") or f.stem == "base":
            continue
        with contextlib.suppress(Exception):
            mod = importlib.import_module(f".{f.stem}", package="scenario_engine.injectors")
            for _, obj in inspect.getmembers(mod, inspect.isclass):
                if obj.__module__ != mod.__name__ or inspect.isabstract(obj):
                    continue
                if issubclass(obj, (_PlainBase, BaseInjector)):
                    with contextlib.suppress(Exception):
                        register_injector(obj)


_auto_discovered = False


def _ensure_discovered() -> None:
    global _auto_discovered
    if not _auto_discovered:
        _auto_discover()
        _auto_discovered = True


_ensure_discovered()

__all__ = [
    "BaseInjector",
    "Injector",
    "InjectResult",
    "RangeContext",
    "register_injector",
    "get_injector",
    "list_injectors",
    "run_inject",
]
