"""The noise dial: one number, 0-100, translated into behaviour.

Pure functions only — no DB, no clock — so the mapping is testable and the same level
always means the same thing in an AAR.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PRESETS: dict[str, int] = {"quiet": 10, "office": 40, "busy": 70, "chaos": 95}

# Everyday activity kinds and their base share of traffic. Personas' habits multiply in.
BASE_MIX: dict[str, float] = {
    "web_browse": 0.32,
    "dns_lookup": 0.18,
    "email_send": 0.10,
    "email_read": 0.12,
    "file_share": 0.14,
    "ad_logon": 0.06,
    "ssh_admin": 0.04,
    "ntp_sync": 0.04,
}

# Benign actions that look like an attack. Only personas flagged for them perform them,
# and only once the dial is high enough that a real range would have that kind of mess.
LOOKALIKES: dict[str, float] = {
    "admin_scan": 0.30,  # IT runs an inventory scan
    "admin_remote_exec": 0.25,  # IT pushes a fix with a remote-exec tool
    "bad_password": 0.30,  # somebody fat-fingers their password a few times
    "bulk_upload": 0.15,  # a large copy to a share at the end of the day
}

ACTIVITY_KINDS: frozenset[str] = frozenset(BASE_MIX) | frozenset(LOOKALIKES)

MAX_ACTIONS_PER_HOUR = 60.0  # per active persona, at level 100
MIN_ACTIONS_PER_HOUR = 2.0  # per active persona, at level 1
LOOKALIKE_FLOOR = 20  # no lookalikes at or below this level
MAX_LOOKALIKE_SHARE = 0.15


@dataclass(frozen=True)
class DialSettings:
    level: int
    active_fraction: float  # share of the roster that is "at work" at all
    actions_per_hour: float  # per active persona, before the diurnal curve
    diurnal_amplitude: float  # 1.0 = dead quiet after hours; 0 = flat around the clock
    lookalike_share: float  # probability an action by an eligible persona is a lookalike
    mix: dict[str, float] = field(default_factory=dict)


def clamp_level(level: int | float) -> int:
    return max(0, min(100, int(round(level))))


def settings_for(level: int | float) -> DialSettings:
    """The behaviour a dial position stands for. Monotonic in every knob."""
    lv = clamp_level(level)
    if lv == 0:
        return DialSettings(0, 0.0, 0.0, 1.0, 0.0, dict(BASE_MIX))
    x = lv / 100
    # Log scale: the bottom of the dial is where fine control matters.
    rate = MIN_ACTIONS_PER_HOUR * (MAX_ACTIONS_PER_HOUR / MIN_ACTIONS_PER_HOUR) ** x
    lookalike = 0.0 if lv <= LOOKALIKE_FLOOR else MAX_LOOKALIKE_SHARE * (lv - LOOKALIKE_FLOOR) / (100 - LOOKALIKE_FLOOR)
    return DialSettings(
        level=lv,
        active_fraction=round(0.15 + 0.85 * x, 4),
        actions_per_hour=round(rate, 3),
        diurnal_amplitude=round(1.0 - 0.6 * x, 4),
        lookalike_share=round(lookalike, 4),
        mix=dict(BASE_MIX),
    )


def resolve_level(
    level: int,
    overrides: dict | None,
    *,
    node: str = "",
    zone: str = "",
    paused: bool = False,
    enabled: bool = True,
) -> int:
    """The effective level for one node: node override beats zone override beats range."""
    if paused or not enabled:
        return 0
    ov = overrides or {}
    for key in (f"node:{node}", f"zone:{zone}"):
        if key.split(":", 1)[1] and key in ov:
            return clamp_level(ov[key])
    return clamp_level(level)


def diurnal(hour: float, work_start: int, work_end: int, amplitude: float) -> float:
    """Activity multiplier at a local hour for someone who works [work_start, work_end).

    1.0 during work hours, a lunch dip at noon, and ``1 - amplitude`` (floored) outside.
    """
    after_hours = max(0.03, 1.0 - amplitude)
    h = hour % 24
    in_hours = work_start <= h < work_end if work_start <= work_end else (h >= work_start or h < work_end)
    if not in_hours:
        return after_hours
    if 12 <= h < 13:
        return 0.5
    return 1.0
