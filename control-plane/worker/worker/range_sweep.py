"""Scheduled reads over every active range: one provisioner and one login per endpoint.

``health_check_ranges`` and ``collect_range_metrics`` (tasks.py) used to build a new
provisioner per range per run, each logging in to vCenter and never logging out; vCenter
caps its sessions, and the runs come every minute. Here ranges are grouped by backend and
by the endpoint they are bound to (worker/connection_binding.py), and each group's checks
run inside the provisioner's ``session()`` scope, which logs in once and out at its end.

Kept out of tasks.py (ADR 0003: its line count is ratcheted).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

from . import base_tasks

logger = logging.getLogger("truenorth.worker")


def _output(raw) -> dict:
    try:
        out = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        out = {}
    return out if isinstance(out, dict) else {}


def health_checks(rows) -> dict[str, object]:
    """{range id: HealthResult, or the exception that range's check raised} for rows of
    db_ops.active_ranges: (id, name, provisioner_output, provisioner_backend). A range whose
    connection cannot be resolved (ConnectionBindingError) gets that error; the others are
    still checked."""
    return _sweep(rows, "health_check")


def metrics(rows) -> dict[str, object]:
    """{range id: MetricsResult, or the exception its read raised}: ``collect_metrics`` of
    every range, grouped and logged in once per endpoint exactly as ``health_checks``."""
    return _sweep(rows, "collect_metrics")


def read_group(provisioner, items: list[tuple[str, dict]]) -> dict[str, object]:
    """The drift reading of one endpoint's ranges (worker/range_drift.py): ``live_inventories``
    once for all of them, inside the provisioner's session (one login). {range id: reading,
    or the exception}."""
    async def run() -> dict[str, object]:
        scope = getattr(provisioner, "session", None)
        async with scope() if callable(scope) else contextlib.nullcontext():
            return await provisioner.live_inventories(items)

    try:
        got = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 — every range of the group says why
        return dict.fromkeys((rid for rid, _ in items), exc)
    return {rid: got.get(rid, RuntimeError("not read")) for rid, _ in items}


def backend_of(row) -> str:
    """The backend a db_ops.active_ranges row was built on (the worker's default if unset)."""
    return (row[3] if len(row) > 3 and row[3] else None) or os.getenv("PROVISIONER_BACKEND", "mock")


def groups(rows) -> tuple[list[tuple[object, list[tuple[str, dict]]]], dict[str, Exception]]:
    """Rows grouped by backend and the endpoint they are bound to: [(provisioner, [(range id,
    output)])], and {range id: why} for those whose provisioner could not be made."""
    errors: dict[str, Exception] = {}
    found: dict[tuple, tuple[object, list[tuple[str, dict]]]] = {}
    for row in rows:
        range_id, output = str(row[0]), _output(row[2])
        backend = backend_of(row)
        try:
            provisioner = base_tasks._get_backend(backend, range_id, output=output)
        except Exception as exc:  # noqa: BLE001 — reported for this range only
            errors[range_id] = exc
            continue
        record = getattr(provisioner, "connection_record", None)
        rec = record() if callable(record) else None
        key = (backend, json.dumps(rec if isinstance(rec, dict) else None, sort_keys=True))  # one connection row
        found.setdefault(key, (provisioner, []))[1].append((range_id, output))
    return list(found.values()), errors


def _sweep(rows, method: str) -> dict[str, object]:
    grouped, errors = groups(rows)
    results: dict[str, object] = dict(errors)
    for provisioner, items in grouped:
        try:
            asyncio.run(_check_group(provisioner, items, results, method))
        except Exception as exc:  # noqa: BLE001 — the scope itself failed (logout): keep what was read
            for range_id, _ in items:
                results.setdefault(range_id, exc)
    return results


async def _check_group(provisioner, items: list[tuple[str, dict]], results: dict,
                       method: str = "health_check") -> None:
    scope = getattr(provisioner, "session", None)  # BaseProvisioner's; a bare backend has none
    async with scope() if callable(scope) else contextlib.nullcontext():
        for range_id, output in items:
            try:
                results[range_id] = await getattr(provisioner, method)(range_id, output)
            except Exception as exc:  # noqa: BLE001
                results[range_id] = exc


def result(checks: dict, range_id: str):
    """The range's HealthResult; raises what its check raised (the callers' per-range except)."""
    got = checks.get(range_id)
    if got is None:
        raise RuntimeError(f"range {range_id} was not checked")
    if isinstance(got, BaseException):
        raise got
    return got
