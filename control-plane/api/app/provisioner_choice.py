"""Which provisioner backend a new range is built with, and whether it may be.

A range records its backend when it is created (``PROVISIONER_BACKEND``); the worker
builds it with that backend's adapter (worker/provisioners, ADR 0001). The API cannot
import the worker's registry, so the names are listed here and a contract test keeps the
two in step (tests/contracts/test_provisioner_choice_matches_worker.py).

Supported: ``vsphere_api`` (ADR 0009) and ``mock``. ``proxmox_api`` and ``hyperv`` are
experimental: a range is not created on them unless ``EXPERIMENTAL_PROVISIONERS`` is true
(the worker refuses them the same way). The Terraform backends were removed.
"""

from __future__ import annotations

import os

from fastapi import HTTPException

SUPPORTED: frozenset[str] = frozenset({"mock", "vsphere_api"})
EXPERIMENTAL: frozenset[str] = frozenset({"proxmox_api", "hyperv"})


def experimental_enabled() -> bool:
    return os.getenv("EXPERIMENTAL_PROVISIONERS", "").strip().lower() in {"1", "true", "yes", "on"}


def refusal(name: str) -> str | None:
    """Why ``name`` may not build a range now, or None when it may."""
    if name in SUPPORTED or (name in EXPERIMENTAL and experimental_enabled()):
        return None
    if name in EXPERIMENTAL:
        return (f"Provisioner backend {name!r} is experimental and disabled. Supported: "
                f"{', '.join(sorted(SUPPORTED))}. Set EXPERIMENTAL_PROVISIONERS=true on the API "
                "and the workers to use it anyway.")
    return (f"PROVISIONER_BACKEND={name!r} is not a provisioner backend. Valid: "
            f"{', '.join(sorted(SUPPORTED | EXPERIMENTAL))}.")


def configured_backend() -> str:
    """The backend a new range gets: ``PROVISIONER_BACKEND`` (default mock). An unknown or
    switched-off experimental one is refused with a 409 that names the fix, before a range
    row exists that no worker would build."""
    name = os.getenv("PROVISIONER_BACKEND", "mock").strip() or "mock"
    reason = refusal(name)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    return name
