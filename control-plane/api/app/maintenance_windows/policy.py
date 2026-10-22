"""What a maintenance window refuses, what always bypasses it, and which ranges it covers.

Pure functions, no database. The worker re-checks windows at a range task's entry with
its own copy of ``applies`` and ``template_tags`` (worker/worker/maintenance_windows.py:
the worker cannot import the API, ADR 0003); tests/contracts/test_maintenance_window_policy.py
keeps the two equal.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

# Refused inside a window, unless the operation carries a bypass (destroy only).
#   provision  every build: the ranges router, batch, exercises, the scheduler clock, labs
#   restore    restoring a snapshot, and a lab's reset (a snapshot restore, or a rebuild)
#   destroy    a person's normal destroy
REFUSED_ACTIONS = ("provision", "restore", "destroy")

# Teardowns that always run, inside a window or not (ADR 0013 §7). Recorded on the range
# operation (range_operations.window_bypass), so the worker's re-check lets them through.
# Lab sessions send their own teardowns (expiry, end, a failed build) without an operation
# row; the worker lets every destroy through that it cannot identify as a person's normal
# one, so cleanup is never blocked by a missing marker.
BYPASS_EMERGENCY = "emergency"  # range:admin, a mandatory reason, audited, certification record
BYPASS_BOOKING_ENDED = "booking_ended"  # the scheduler's teardown when a booking ends (lease expiry)
BYPASS_CANARY = "canary"  # the nightly canary tearing its own range down (#245's second half)
BYPASS_KINDS = (BYPASS_EMERGENCY, BYPASS_BOOKING_ENDED, BYPASS_CANARY)
# Automated failure cleanup needs no marker: a lab's failed build is torn down by its session
# (no operation row; the worker identifies the range as a lab's), and a failed build's own
# leftovers are discarded inside the build task, which is never re-checked.

# What the worker writes on a range whose teardown it held without an operation to park
# (worker/maintenance_windows.py): such a range may be torn down although it is destroying.
HELD_MARKER = "is held by the worker: a maintenance window"


def template_tags(template_yaml: str | None) -> set[str]:
    """The tags a range template declares at its top level (``tags: [lab, soc]``),
    lowercased. Node-level tags (a VM's role) are not range tags. Unreadable YAML: none."""
    if not template_yaml:
        return set()
    try:
        from ..safe_yaml import load

        doc = load(template_yaml)
    except Exception:  # noqa: BLE001 — a template that does not parse declares no tags
        return set()
    raw = doc.get("tags") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        return set()
    return {str(t).strip().lower() for t in raw if isinstance(t, str | int) and str(t).strip()}


def applies(
    *,
    window_tenant: uuid.UUID | str | None,
    scope: str,
    range_ids: Iterable[str] | None,
    tags: Iterable[str] | None,
    range_id: uuid.UUID | str | None,
    range_tenant: uuid.UUID | str | None,
    range_tags: Iterable[str] | None,
) -> bool:
    """Whether a window covers a range. ``range_id`` None: a range not created yet (a lab
    about to launch), which only an ``all`` window covers."""
    if window_tenant is not None and str(window_tenant) != str(range_tenant):
        return False
    if scope == "all":
        return True
    if range_id is None:
        return False
    if scope == "ranges":
        return str(range_id) in {str(r) for r in (range_ids or [])}
    if scope == "tags":
        wanted = {str(t).strip().lower() for t in (tags or [])}
        return bool(wanted & {str(t).lower() for t in (range_tags or [])})
    return False  # an unknown scope covers nothing; the API never writes one
