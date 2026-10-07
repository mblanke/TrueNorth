"""Turn personas + the dial into a concrete, timestamped action plan for one node.

Deterministic: the plan for a given (seed, node, window) is the same however often and
whenever it is asked for, so an agent that re-fetches after a restart does not double
up, and an AAR can reconstruct exactly what the background was supposed to be.

Time is cut into fixed WINDOW_MINUTES windows aligned to the epoch; each window is
planned independently from its own seeded RNG.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import dial

WINDOW_MINUTES = 5
MAX_PLAN_MINUTES = 60

# Which target pool each activity draws from (see PlanContext.targets).
TARGET_POOL: dict[str, str] = {
    "web_browse": "web",
    "dns_lookup": "dns",
    "email_send": "mail",
    "email_read": "mail",
    "file_share": "share",
    "bulk_upload": "share",
    "ad_logon": "dc",
    "bad_password": "dc",
    "ssh_admin": "ssh",
    "ntp_sync": "ntp",
    "admin_scan": "subnet",
    "admin_remote_exec": "hosts",
}


@dataclass(frozen=True)
class PersonaSpec:
    handle: str
    node: str
    work_start: int = 8
    work_end: int = 17
    habits: dict[str, float] = field(default_factory=dict)
    lookalikes: bool = False
    contacts: tuple[str, ...] = ()  # mail addresses this persona writes to


@dataclass(frozen=True)
class PlanContext:
    seed: int
    level: int  # already resolved for this node (overrides, pause applied)
    utc_offset: int = 0
    # pool name -> candidate targets, e.g. {"web": ["intranet.corp.local"], "mail": ["mail.corp.local"]}
    targets: dict[str, list[str]] = field(default_factory=dict)


def _rng(*parts: object) -> random.Random:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _unit(*parts: object) -> float:
    """A stable number in [0, 1) for the given key."""
    return _rng(*parts).random()


def is_active(seed: int, handle: str, fraction: float) -> bool:
    """Whether a persona is part of today's active set.

    Each persona has a fixed draw; raising the dial raises the bar it is compared to,
    so turning the noise up only ever adds people, never swaps them.
    """
    return _unit(seed, "active", handle) < fraction


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _weighted(rng: random.Random, weights: dict[str, float]) -> str | None:
    items = [(k, w) for k, w in sorted(weights.items()) if w > 0]
    if not items:
        return None
    total = sum(w for _, w in items)
    r = rng.random() * total
    for k, w in items:
        r -= w
        if r <= 0:
            return k
    return items[-1][0]


def window_start(t: datetime) -> datetime:
    t = t.astimezone(UTC)
    epoch_min = int(t.timestamp() // 60)
    return datetime.fromtimestamp((epoch_min - epoch_min % WINDOW_MINUTES) * 60, tz=UTC)


def plan_window(ctx: PlanContext, personas: list[PersonaSpec], start: datetime) -> list[dict]:
    settings = dial.settings_for(ctx.level)
    if settings.level == 0:
        return []
    start = window_start(start)
    local_hour = (start + timedelta(hours=ctx.utc_offset)).hour + start.minute / 60
    out: list[dict] = []
    for p in sorted(personas, key=lambda p: p.handle):
        if not is_active(ctx.seed, p.handle, settings.active_fraction):
            continue
        rng = _rng(ctx.seed, p.node, p.handle, start.isoformat())
        curve = dial.diurnal(local_hour, p.work_start, p.work_end, settings.diurnal_amplitude)
        lam = settings.actions_per_hour * curve * WINDOW_MINUTES / 60
        mix = {k: w * float(p.habits.get(k, 1.0)) for k, w in settings.mix.items()}
        for _ in range(_poisson(rng, lam)):
            offset = rng.uniform(0, WINDOW_MINUTES * 60)
            lookalike = p.lookalikes and rng.random() < settings.lookalike_share
            kind = _weighted(rng, dial.LOOKALIKES if lookalike else mix)
            if kind is None:
                continue
            pool = ctx.targets.get(TARGET_POOL.get(kind, ""), [])
            if not pool:
                continue  # nothing in this range to do it against; skip, don't invent
            action = {
                "at": (start + timedelta(seconds=offset)).isoformat(),
                "persona": p.handle,
                "kind": kind,
                "target": pool[rng.randrange(len(pool))],
                "lookalike": lookalike,
                "params": {},
            }
            if kind == "email_send" and p.contacts:
                action["params"]["to"] = p.contacts[rng.randrange(len(p.contacts))]
            out.append(action)
    out.sort(key=lambda a: (a["at"], a["persona"]))
    return out


def plan(ctx: PlanContext, personas: list[PersonaSpec], start: datetime, minutes: int) -> list[dict]:
    """Every action due in [start, start + minutes), across the windows it spans."""
    minutes = max(1, min(MAX_PLAN_MINUTES, minutes))
    start = start.astimezone(UTC)
    end = start + timedelta(minutes=minutes)
    out: list[dict] = []
    w = window_start(start)
    while w < end:
        for a in plan_window(ctx, personas, w):
            at = datetime.fromisoformat(a["at"])
            if start <= at < end:
                out.append(a)
        w += timedelta(minutes=WINDOW_MINUTES)
    return out
