"""The agent loop: fetch plan, carry it out on time, report, repeat.

Plans are deterministic and overlap from one fetch to the next, so the runner remembers
what it has already done and never does an action twice.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from . import VERSION
from .activities import REGISTRY, AgentConfig, _host, dry_run, guard

log = logging.getLogger("tn-noise")

# An action more than this late (agent was down, VM was paused) is dropped, not replayed:
# a burst of stale activity is itself an anomaly a defender could spot.
STALE_SECONDS = 120
REPORT_BATCH = 500  # the controller's per-report cap


def _key(a: dict) -> tuple:
    return (a["at"], a.get("persona", ""), a["kind"], a.get("target", ""))


class Runner:
    def __init__(
        self,
        client,
        cfg: AgentConfig | None = None,
        *,
        dry: bool = False,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.cfg = cfg or AgentConfig()
        self.dry = dry
        self.clock = clock
        self.sleep = sleep
        self.done: dict[tuple, float] = {}
        self.poll = 60.0

    def _execute(self, action: dict) -> dict:
        fn = dry_run if self.dry else REGISTRY.get(action["kind"])
        # Echo the planned fields and signature verbatim: the controller records only
        # actions it can verify it planned.
        result = {
            "at": action["at"],
            "persona": action.get("persona", ""),
            "kind": action["kind"],
            "target": action.get("target", ""),
            "sig": action.get("sig", ""),
            "ok": True,
            "ran_at": datetime.fromtimestamp(self.clock(), tz=UTC).isoformat(),
            "detail": {},
        }
        if fn is None:
            result.update(ok=False, detail={"error": "unsupported on this agent"})
            return result
        try:
            if not self.dry and action["kind"] != "admin_scan":  # admin_scan guards each host itself
                guard(_host(action.get("target", "")), self.cfg)
            result["detail"] = fn(action, self.cfg) or {}
        except Exception as exc:  # noqa: BLE001 — any failure is a result, never a crash
            result.update(ok=False, detail={"error": f"{type(exc).__name__}: {exc}"[:200]})
        return result

    def cycle(self) -> float:
        """One poll: run everything due before the next poll. Returns seconds to wait."""
        plan = self.client.plan(minutes=max(2, int(self.poll // 60) + 2))
        self.poll = float(plan.get("poll_seconds", 60))
        horizon = self.clock() + self.poll
        results = []
        for action in sorted(plan.get("actions", []), key=lambda a: a["at"]):
            key = _key(action)
            if key in self.done:
                continue
            due = datetime.fromisoformat(action["at"]).timestamp()
            if due >= horizon:
                break
            now = self.clock()
            if now - due > STALE_SECONDS:
                self.done[key] = due
                continue
            if due > now:
                self.sleep(due - now)
            results.append(self._execute(action))
            self.done[key] = due
        for i in range(0, len(results), REPORT_BATCH):
            self.client.report(VERSION, results[i : i + REPORT_BATCH])
        cutoff = self.clock() - 3600
        self.done = {k: t for k, t in self.done.items() if t >= cutoff}
        return max(0.0, horizon - self.clock())

    def run_forever(self) -> None:
        while True:
            try:
                wait = self.cycle()
            except Exception as exc:  # noqa: BLE001 — controller unreachable: back off, keep going
                log.warning("cycle failed: %s", exc)
                wait = 30.0
            self.sleep(wait)
