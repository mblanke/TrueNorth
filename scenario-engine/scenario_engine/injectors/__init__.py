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
from datetime import UTC, datetime
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
    # True when the inject was deliberately not run (e.g. it needs range hosts and the
    # range has none). A skipped inject is neither a success nor an injector failure.
    skipped: bool = False
    # "simulated" (synthetic records, nothing touched) or "live"; None when nothing ran.
    execution_mode: str | None = None


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
    # Same contract flags as base.BaseInjector (see there).
    touches_range_hosts: bool = True
    execution_mode: str = "simulated"

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


INJECT_PREFIX = "inject."
# Where inject telemetry carries its labels. The API's detection grammar refuses it
# (control-plane/api/app/search_backends/detection_query.py, same constant; a test pins both).
GROUND_TRUTH_FIELD = "tn_ground_truth"


def canonical_action(action: str) -> str:
    """The registry key for a timeline action.

    Two dialects name the same injector: the engine's examples say ``dns_spike``; authored
    scenarios (the ai-orchestrator prompt, QSP paths, ``/scenarios/validate`` fixtures) say
    ``inject.dns_spike``. This is the one adapter between them. Anything else is returned
    as is and fails as an unknown action.
    """
    a = str(action or "").strip()
    return a[len(INJECT_PREFIX) :] if a.startswith(INJECT_PREFIX) else a


def injector_profile(action: str) -> dict[str, Any] | None:
    """The contract flags of the injector registered for *action*, or None if there is none."""
    cls = _INJECTOR_REGISTRY.get(canonical_action(action))
    if cls is None:
        return None
    return {
        "touches_range_hosts": bool(getattr(cls, "touches_range_hosts", True)),
        "execution_mode": str(getattr(cls, "execution_mode", "simulated")),
    }


def inject_telemetry(action: str, raw: dict[str, Any], ctx: RangeContext, *, execution_mode: str) -> list[dict]:
    """Telemetry for one executed inject: the injector's own events, else one summary event.

    Every platform label (which exercise and inject, the technique, the target, that the
    record is synthetic) goes under ``GROUND_TRUTH_FIELD``, never beside the observable
    fields. That object is stored but not indexed (``telemetry/pipelines/bootstrap.py``) and
    the detection grammar refuses it, so a Student cannot earn detection credit by querying
    the labels instead of the attack (ADR 0005, security sweep H4). Before 2026-10-08 the
    labels were flat (``inject_action``, ``exercise_id``, ``truenorth_simulated``,
    ``event.module: truenorth.inject``, ``threat.technique.id``) and searchable by anyone.

    A summary event is nothing but labels: it records that the inject ran.
    """
    own = raw.get("telemetry")
    events = [dict(e) for e in own if isinstance(e, dict)] if isinstance(own, list) else []
    summary = {
        "module": "truenorth.inject",
        "action": action,
        "message": f"inject {action} ({execution_mode})",
        "technique_id": raw.get("technique_id") or raw.get("technique"),
        "target": raw.get("target") or raw.get("target_host") or raw.get("target_dc"),
    }
    if not events:
        events = [{"event.kind": "event"}]
    for e in events:
        e.setdefault("@timestamp", datetime.now(UTC).isoformat())
        e.update({"range_id": ctx.range_id, "tenant_id": ctx.tenant_id})
        e[GROUND_TRUTH_FIELD] = {
            **summary,
            "exercise_id": ctx.exercise_id,
            "inject_action": action,
            "simulated": execution_mode != "live",
        }
    return events


def run_inject(action: str, params: dict, ctx: RangeContext, *, allow_host_effects: bool = True) -> InjectResult:
    """Execute an inject by action name, returning an ``InjectResult``. Never raises.

    Bridges the two conventions: builds the ``BaseInjector`` with *params*, calls
    ``execute(context)`` and wraps the dict result into an ``InjectResult`` carrying the
    telemetry to ship. An unknown *action*, invalid params or an injector exception give
    a failed result. With ``allow_host_effects=False`` (a range with no hosts, e.g. the
    mock backend) an injector whose contract touches range hosts is not run: the result
    is ``skipped``.
    """
    cls = _INJECTOR_REGISTRY.get(canonical_action(action))
    if cls is None:
        return InjectResult(success=False, action=action, detail=f"No injector registered for action '{action}'")
    action = canonical_action(action)
    profile = injector_profile(action) or {}
    mode = profile.get("execution_mode", "simulated")
    if profile.get("touches_range_hosts", True) and not allow_host_effects:
        return InjectResult(
            success=False,
            action=action,
            detail="skipped: mock backend (injector needs range hosts)",
            skipped=True,
        )
    try:
        inj = cls(dict(params or {}))
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
        if not isinstance(raw, dict):
            raise TypeError(f"injector returned {type(raw).__name__}, expected dict")
        telemetry = inject_telemetry(action, raw, ctx, execution_mode=mode)
    except AssertionError as exc:  # validate_params() uses assert for missing params
        return InjectResult(success=False, action=action, detail=f"Invalid params: {exc or 'missing required param'}")
    except Exception as exc:  # noqa: BLE001 — injectors must not crash the run
        logger.exception("Injector %s failed", action)
        return InjectResult(success=False, action=action, detail=f"Injector error: {exc}")
    return InjectResult(
        success=bool(raw.get("success", True)),
        action=action,
        detail=str(raw.get("detail", raw.get("injector", ""))),
        telemetry=telemetry,
        mitre_technique=raw.get("technique_id") or raw.get("technique"),
        raw=raw,
        execution_mode=mode,
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
    "canonical_action",
    "injector_profile",
    "inject_telemetry",
    "run_inject",
    "GROUND_TRUTH_FIELD",
]
