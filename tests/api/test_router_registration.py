"""Guard that every router in app.routers is actually registered on the app.

On 2026-08-19 every `app.include_router(...)` call was deleted from main.py while
the `from .routers import (...)` block was left in place. The app imported fine and
pytest collected fine; the damage surfaced only as ~93 unrelated-looking 404s.

This test fails at the point of the deletion instead.
"""

import app.routers as routers_pkg
from app.main import app


def _registered_paths() -> set[str]:
    return {r.path for r in app.routes if hasattr(r, "path")}


def _router_objects() -> dict[str, object]:
    """Every APIRouter re-exported by app.routers."""
    from fastapi import APIRouter

    return {name: obj for name, obj in vars(routers_pkg).items() if isinstance(obj, APIRouter)}


def test_routers_are_exported():
    assert len(_router_objects()) >= 25, "app.routers exports far fewer routers than expected"


def test_every_exported_router_is_registered():
    """A router that is imported but never included serves nothing."""
    registered = _registered_paths()
    unregistered = []
    for name, router in _router_objects().items():
        paths = {r.path for r in router.routes if hasattr(r, "path")}
        if paths and not (paths & registered):
            unregistered.append(name)
    assert not unregistered, f"routers imported but never included in main.py: {sorted(unregistered)}"


def test_core_endpoints_are_present():
    """Health and the websocket are load-bearing for deploys and dashboards."""
    registered = _registered_paths()
    for path in ("/health", "/health/ready", "/health/deep", "/ws/{channel}"):
        assert path in registered, f"{path} is not registered"


def test_no_unauthenticated_static_docs_mount():
    """`docs/` holds CAF material; it must never be served as static files.

    Reverses the 2026-08-19 `/static-docs` StaticFiles mount, which exposed the
    whole docs directory (SimSpace PDF, DND runbook .docx/.xlsx) without auth.
    """
    mounted = [r.path for r in app.routes if r.__class__.__name__ == "Mount"]
    assert not any("doc" in p.lower() for p in mounted), f"docs served statically: {mounted}"
