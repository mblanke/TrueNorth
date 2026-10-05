"""The scheduled health check and metrics collection over every active range.

The Celery tasks are in worker/tasks.py (``health_check_ranges``, ``collect_range_metrics``);
this is what they run. worker.tasks owns the I/O seams (session, backend lookup, notify,
the telemetry task); they are looked up on it at call time (``_t()``), so a test that
patches one there patches it here too.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime

from . import db_ops

logger = logging.getLogger("truenorth.worker")


def _t():
    from . import tasks

    return tasks


# -- Periodic: health and metrics over the active ranges --------------------
# Every range is read through the backend that built it (provisioner_backend), not the
# worker's PROVISIONER_BACKEND: a vSphere range on a worker whose env says proxmox_api
# used to be asked about by the Proxmox provisioner.
#
# Each run is bounded three ways: a Redis lock (run_lock) skips a run while the previous one still
# holds it; a budget stops starting ranges once spent (the rest are counted `skipped`,
# not unhealthy) and caps each range's call at what is left of it; Celery's time limits
# end a run stuck past both (prefork pool only). The beat entries also expire, so runs
# queued behind a busy worker are dropped rather than run late, one after another.
HEALTH_CHECK_BUDGET = int(os.getenv("TN_HEALTH_CHECK_BUDGET", "45"))  # runs every 60s
METRICS_BUDGET = int(os.getenv("TN_METRICS_BUDGET", "20"))  # runs every 30s

_SKIPPED = object()  # a range the run's budget did not reach


@contextmanager
def run_lock(name: str, ttl: int):
    """Yield True when this run may go ahead, False while another run holds ``name``.

    Non-blocking: a run that finds the lock taken is skipped, not queued. The lock expires
    after ``ttl`` seconds, so a worker killed mid-run holds it no longer than that.
    Without Redis the run goes ahead unlocked: this only stops two runs of one schedule
    overlapping, which costs time, not correctness. Nothing that must not collide relies
    on Redis (VLANs and addresses: db_ops.reserve_values; range operations: the API).
    """
    lock = None
    acquired = True
    try:
        import redis

        client = redis.Redis.from_url(_t().REDIS_URL, socket_timeout=5, socket_connect_timeout=5)
        lock = client.lock(f"truenorth:periodic:{name}", timeout=ttl)
        acquired = bool(lock.acquire(blocking=False))
    except Exception as e:  # noqa: BLE001 — no Redis (tests, single worker): carry on unlocked
        logger.warning(f"[{name}] run lock unavailable, running without it: {e}")
        lock = None
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        if lock is not None:
            with contextlib.suppress(Exception):  # expired meanwhile: nothing to release
                lock.release()


def _active_ranges() -> list:
    """(id, name, provisioner_output, provisioner_backend) of every ready or running range."""
    with _t()._db_session() as db:
        return db_ops.active_ranges(db)


def group_by_backend(rows) -> dict[str, list]:
    """Active-range rows keyed by each range's own backend (_range_backend: the env is the fallback)."""
    groups: dict[str, list] = {}
    for row in rows:
        backend = _t()._range_backend((None, row[2], row[3] if len(row) > 3 else None))
        groups.setdefault(backend, []).append(row)
    return groups


async def _run_group(provisioner, rows, op, deadline: float) -> list[tuple]:
    """``op(provisioner, range_id, provision_output)`` over ``rows``, inside one provisioner session."""
    results: list[tuple] = []
    async with provisioner.session():
        for row in rows:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append((row, _SKIPPED))
                continue
            try:
                prov = json.loads(row[2]) if row[2] else {}
                res = await asyncio.wait_for(op(provisioner, str(row[0]), prov), timeout=remaining)
            except TimeoutError:
                res = TimeoutError("no answer before the run's time budget ran out")
            except Exception as e:  # noqa: BLE001 — one range's failure is that range's result
                res = e
            results.append((row, res))
    return results


def _over_ranges(rows, op, deadline: float) -> dict[str, list[tuple]]:
    """Run ``op`` for every range through its own backend: {backend: [(row, result)]}.

    One provisioner instance and one session() per backend, so one hypervisor login per
    run however many ranges it covers. A result is the op's return value, the exception
    it raised, or _SKIPPED.
    """
    out: dict[str, list[tuple]] = {}
    for backend, members in group_by_backend(rows).items():
        try:
            provisioner = _t()._get_backend(backend)
            out[backend] = asyncio.run(_run_group(provisioner, members, op, deadline))
        except Exception as e:  # noqa: BLE001 — unknown backend, or its session could not open
            logger.error(f"[periodic] backend {backend!r} unavailable for {len(members)} ranges: {e}")
            out[backend] = [(row, e) for row in members]
    return out


def health_check_run(deadline: float) -> dict:
    try:
        active_ranges = _active_ranges()

        if not active_ranges:
            logger.info("[health] No active ranges to check")
            return {"status": "ok", "checked": 0, "healthy": 0, "unhealthy": 0}

        healthy = 0
        unhealthy = 0
        skipped = 0
        unhealthy_ids = []
        by_backend = {}

        grouped = _over_ranges(active_ranges, lambda p, rid, out: p.health_check(rid, out), deadline)
        for backend, results in grouped.items():
            by_backend[backend] = len(results)
            for row, result in results:
                range_id, name = str(row[0]), row[1]
                if result is _SKIPPED:
                    skipped += 1
                elif isinstance(result, Exception):
                    unhealthy += 1
                    unhealthy_ids.append(range_id)
                    logger.error(f"[health] Error checking range {name} ({backend}): {result}")
                elif result.healthy:
                    healthy += 1
                else:
                    unhealthy += 1
                    unhealthy_ids.append(range_id)
                    logger.warning(f"[health] Range {name} ({range_id}) is UNHEALTHY")
                    _t()._notify_api(
                        "range",
                        {
                            "id": range_id,
                            "event": "health_check_failed",
                        },
                    )

        if skipped:
            logger.warning(f"[health] Budget of {HEALTH_CHECK_BUDGET}s spent; {skipped} ranges not checked this run")

        summary = {
            "status": "ok",
            "checked": healthy + unhealthy,
            "healthy": healthy,
            "unhealthy": unhealthy,
            "unhealthy_ids": unhealthy_ids,
            "skipped": skipped,
            "by_backend": by_backend,
        }

        if unhealthy > 0:
            _t()._notify_api(
                "system",
                {
                    "event": "health_check_alert",
                    "unhealthy_count": unhealthy,
                    "unhealthy_ids": unhealthy_ids,
                },
            )

        logger.info(
            f"[health] Check complete: {healthy} healthy, {unhealthy} unhealthy, {skipped} skipped "
            f"out of {len(active_ranges)} ranges"
        )
        return summary

    except Exception as e:
        logger.error(f"[health] Health check error: {e}")
        raise


# -- Periodic: Collect Range Metrics ---------------------------------------
def _usage(vms: list[dict], used_key: str, cap_key: str) -> tuple[float | None, int | None, int | None]:
    """(percent, used, capacity) over the VMs that report both numbers; all None when none do."""
    pairs = [
        (v[used_key], v[cap_key])
        for v in vms
        if isinstance(v.get(used_key), int | float) and isinstance(v.get(cap_key), int | float) and v[cap_key] > 0
    ]
    if not pairs:
        return None, None, None
    used, cap = sum(u for u, _ in pairs), sum(c for _, c in pairs)
    return round(100.0 * used / cap, 1), used, cap


def _recorded_vm_count(prov_output: str | None) -> int:
    try:
        return len((json.loads(prov_output) if prov_output else {}).get("vms") or [])
    except (ValueError, AttributeError):
        return 0


def _range_metrics_event(range_id: str, name: str, backend: str, now: str, result, recorded_vms: int) -> dict:
    """The ``range_metrics`` telemetry event for one range (index ``range-<id>``).

    The keys the random-number version sent are all kept. cpu_pct and memory_pct are
    measured now (CPU in use over the VMs' ceilings; active over configured guest
    memory; powered-on VMs only); the disk and network rates no backend reads yet are
    null, never an estimate. ``synthetic`` is True for mock numbers.
    """
    on = [v for v in result.vms if v.get("power_state") == "poweredOn"]
    cpu_pct, cpu_used, cpu_cap = _usage(on, "cpu_usage_mhz", "cpu_capacity_mhz")
    mem_pct, mem_used, mem_cfg = _usage(on, "memory_active_mb", "memory_configured_mb")
    return {
        "@timestamp": now,
        "event_type": "range_metrics",
        "range_id": range_id,
        "range_name": name,
        "timestamp": now,
        "cpu_pct": cpu_pct,
        "memory_pct": mem_pct,
        "disk_read_mbps": None,
        "disk_write_mbps": None,
        "network_in_mbps": None,
        "network_out_mbps": None,
        "vm_count": len(result.vms) or recorded_vms,
        "vms_powered_on": len(on),
        "vms_tools_running": sum(1 for v in result.vms if v.get("tools_status") == "guestToolsRunning"),
        "cpu_usage_mhz": cpu_used,
        "cpu_capacity_mhz": cpu_cap,
        "memory_active_mb": mem_used,
        "memory_configured_mb": mem_cfg,
        "backend": backend,
        "metrics_source": result.source or backend,
        "metrics_status": result.status,
        "synthetic": bool(result.synthetic),
        "metrics_errors": list(result.errors)[:20],
        "vm_metrics": result.vms,
    }


def collect_metrics_run(deadline: float) -> dict:
    from .provisioners.results import MetricsResult

    try:
        active_ranges = _active_ranges()

        if not active_ranges:
            logger.info("[metrics] No active ranges for metrics collection")
            return {"status": "ok", "ranges_collected": 0}

        now = datetime.now(UTC).isoformat()
        collected = []  # ids as the database returned them, for the updated_at bump
        failed = unsupported = skipped = 0

        grouped = _over_ranges(active_ranges, lambda p, rid, out: p.collect_metrics(rid, out), deadline)
        for backend, results in grouped.items():
            for row, result in results:
                range_id = str(row[0])
                if result is _SKIPPED:
                    skipped += 1
                    continue
                if isinstance(result, Exception):
                    result = MetricsResult(
                        status="failed", source=backend, errors=[str(result) or type(result).__name__]
                    )
                if result.status == "unsupported":
                    unsupported += 1
                    continue
                if result.status in ("ok", "partial"):
                    collected.append(row[0])
                else:
                    failed += 1
                    logger.warning(f"[metrics] Range {row[1]} ({backend}): {'; '.join(result.errors)}")

                event = _range_metrics_event(range_id, row[1], backend, now, result, _recorded_vm_count(row[2]))
                try:
                    _t().ingest_telemetry_batch.delay(range_id, [event])
                except Exception as ingest_err:
                    logger.warning(f"[metrics] Failed to queue telemetry for {range_id}: {ingest_err}")

        if collected:
            with _t()._db_session() as db:
                db_ops.touch_ranges(db, collected)

        if skipped:
            logger.warning(f"[metrics] Budget of {METRICS_BUDGET}s spent; {skipped} ranges not read this run")
        logger.info(
            f"[metrics] Collected metrics for {len(collected)} ranges "
            f"({failed} failed, {unsupported} unsupported, {skipped} skipped)"
        )
        return {
            "status": "ok",
            "ranges_collected": len(collected),
            "failed": failed,
            "unsupported": unsupported,
            "skipped": skipped,
        }

    except Exception as e:
        logger.error(f"[metrics] Metrics collection error: {e}")
        raise
