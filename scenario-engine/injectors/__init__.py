"""TrueNorth Range — Injector base class and registry."""
from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RangeContext:
    """Context about the range an inject is targeting."""
    range_id: str
    tenant_id: str
    vms: list[dict]  # [{"name": ..., "ip": ..., "role": ...}]
    networks: list[dict]
    opensearch_url: str = "http://opensearch:9200"


@dataclass
class InjectResult:
    """Result of an inject execution."""
    success: bool
    action: str
    detail: str = ""
    artifacts: list[str] | None = None  # paths to generated artifacts


class Injector(ABC):
    """Base class for all scenario injectors."""

    @property
    @abstractmethod
    def action_name(self) -> str:
        """The action string that maps to this injector in scenario YAML."""
        ...

    @abstractmethod
    def execute(self, params: dict, ctx: RangeContext) -> InjectResult:
        """Execute the inject action."""
        ...


# ── Registry ───────────────────────────────────────────────────────────
_INJECTOR_REGISTRY: dict[str, type[Injector]] = {}


def register_injector(cls: type[Injector]) -> type[Injector]:
    """Decorator to register an injector class."""
    instance = cls()
    _INJECTOR_REGISTRY[instance.action_name] = cls
    logger.info(f"Registered injector: {instance.action_name}")
    return cls


def get_injector(action: str) -> Injector | None:
    """Look up an injector by action name."""
    cls = _INJECTOR_REGISTRY.get(action)
    return cls() if cls else None


def list_injectors() -> list[str]:
    """List all registered injector action names."""
    return list(_INJECTOR_REGISTRY.keys())


def _auto_discover():
    """Import all injector modules to trigger registration."""
    import pathlib
    pkg_dir = pathlib.Path(__file__).parent
    for f in pkg_dir.glob("*.py"):
        if f.name.startswith("_"):
            continue
        module_name = f"scenario-engine.injectors.{f.stem}"
        try:
            importlib.import_module(f".{f.stem}", package="scenario-engine.injectors")
        except Exception:
            pass


_auto_discover()
