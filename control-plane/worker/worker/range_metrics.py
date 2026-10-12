"""Range utilisation for ``collect_range_metrics`` (tasks.py): measured, or nothing.

Until 2026-10-11 the task published ``random.uniform`` CPU, memory, disk and network
figures as ``range_metrics`` telemetry. Now every figure comes from the range's backend
(``BaseProvisioner.collect_metrics``, read by range_sweep.metrics: one login per
endpoint) and an event carries the backend it came from (``source``).

Per range, the contract is:

- The backend measured something: one ``range_metrics`` event with the figures it gave.
  ``cpu_pct`` is the CPU in use over the CPU ceiling of the range's powered-on VMs;
  ``memory_pct`` is their active guest memory over their configured memory. A figure no
  VM reported is left out of the event, never filled in. There is no disk or network
  throughput: quickStats has none, and nothing here reads performance counters yet.
- The backend cannot report (``unsupported``), failed, read no figure, or its numbers
  are made up (``synthetic``, the mock backend): no event, and the range's usage is
  ``{"available": False, "reason": ...}``.

There is no ``resource_usage`` column on ranges (the old docstring said there was; it
only bumped ``updated_at``). The per-range usage is the task's return value.

Kept out of tasks.py (ADR 0003: its line count is ratcheted).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from . import range_sweep

logger = logging.getLogger("truenorth.worker")

EVENT_TYPE = "range_metrics"


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _share(vms: list[dict], used: str, total: str) -> tuple[float | None, int]:
    """sum(used) / sum(total) * 100 over the VMs that reported both (total > 0), and how many did."""
    pairs = [(vm[used], vm[total]) for vm in vms if _num(vm.get(used)) and _num(vm.get(total)) and vm[total] > 0]
    if not pairs:
        return None, 0
    return round(100.0 * sum(u for u, _ in pairs) / sum(t for _, t in pairs), 1), len(pairs)


def _reason(result, backend: str) -> str | None:
    """Why ``result`` carries no measurement, or None when it may."""
    if isinstance(result, BaseException):
        return f"metrics read failed: {str(result)[:300] or type(result).__name__}"
    status = getattr(result, "status", None)
    if status is None:
        return f"the {backend} backend returned no metrics"
    errors = list(getattr(result, "errors", None) or [])
    if status == "unsupported":
        return errors[0] if errors else f"the {backend} backend does not report VM metrics"
    if getattr(result, "synthetic", False):
        return f"the {getattr(result, 'source', '') or backend} backend's figures are made up; not published"
    if status == "failed":
        return "; ".join(errors)[:300] or "metrics read failed"
    return None


def summarise(range_id: str, name: str, backend: str, result, vm_count: int, now: str) -> tuple[dict | None, dict]:
    """(the range_metrics event to publish, or None; the range's usage)."""
    reason = _reason(result, backend)
    source = getattr(result, "source", "") or backend
    vms = [] if reason else [vm for vm in result.vms if vm.get("power_state") == "poweredOn"]
    cpu, cpu_n = _share(vms, "cpu_usage_mhz", "cpu_capacity_mhz")
    mem, mem_n = _share(vms, "memory_active_mb", "memory_configured_mb")
    if reason is None and cpu is None and mem is None:
        reason = "no powered-on VM reported CPU or memory figures"
    if reason:
        return None, {"available": False, "reason": reason, "source": source, "vm_count": vm_count,
                      "timestamp": now}
    usage: dict = {"available": True, "source": source, "vm_count": vm_count, "vms_powered_on": len(vms),
                   "vms_reported": max(cpu_n, mem_n), "timestamp": now}
    if cpu is not None:
        usage["cpu_pct"] = cpu
    if mem is not None:
        usage["memory_pct"] = mem
    if result.status == "partial":
        usage["partial"] = True
    event = {"@timestamp": now, "event_type": EVENT_TYPE, "range_id": range_id, "range_name": name,
             **{k: v for k, v in usage.items() if k != "available"}}
    return event, usage


def _recorded_vms(raw) -> int:
    vms = range_sweep._output(raw).get("vms")
    return len(vms) if isinstance(vms, list) else 0


def collect(active_ranges, publish=None) -> dict:
    """Read, summarise and publish the metrics of ``active_ranges`` (db_ops.active_ranges rows).

    ``publish(range_id, events)`` defaults to queueing ``ingest_telemetry_batch``."""
    if publish is None:
        from .telemetry_tasks import ingest_telemetry_batch

        publish = ingest_telemetry_batch.delay
    if not active_ranges:
        logger.info("[metrics] No active ranges for metrics collection")
        return {"status": "ok", "ranges_collected": 0, "ranges_unavailable": 0, "resource_usage": {}}

    now = datetime.now(UTC).isoformat()
    results = range_sweep.metrics(active_ranges)
    usage_by_range: dict[str, dict] = {}
    collected = 0
    for row in active_ranges:
        range_id, name = str(row[0]), row[1]
        backend = range_sweep.backend_of(row)
        result = results.get(range_id, RuntimeError(f"range {range_id} was not read"))
        event, usage = summarise(range_id, name, backend, result, _recorded_vms(row[2]), now)
        usage_by_range[range_id] = usage
        if event is None:
            logger.info("[metrics] Range %s (%s): not reported: %s", name, range_id, usage["reason"])
            continue
        try:
            publish(range_id, [event])
            collected += 1
        except Exception as exc:  # noqa: BLE001 — the other ranges are still published
            logger.warning("[metrics] Failed to queue telemetry for %s: %s", range_id, exc)
    unavailable = sum(1 for u in usage_by_range.values() if not u["available"])
    logger.info("[metrics] Published metrics for %d ranges; %d not reported", collected, unavailable)
    return {"status": "ok", "ranges_collected": collected, "ranges_unavailable": unavailable,
            "resource_usage": usage_by_range}
