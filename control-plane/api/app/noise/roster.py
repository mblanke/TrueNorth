"""The built-in roster: plausible personas with no LLM.

AI-generated org packs (with bios and portraits) replace this later; this is the
fallback that makes the feature work on a range with no model available.
"""

from __future__ import annotations

from .planner import _rng

FIRST = [
    "Alex",
    "Avery",
    "Blake",
    "Casey",
    "Dana",
    "Drew",
    "Eden",
    "Emery",
    "Finley",
    "Gray",
    "Harper",
    "Hayden",
    "Jamie",
    "Jordan",
    "Kai",
    "Kendall",
    "Lane",
    "Logan",
    "Morgan",
    "Noel",
    "Parker",
    "Quinn",
    "Reese",
    "Riley",
    "Rowan",
    "Sage",
    "Sam",
    "Skyler",
    "Taylor",
    "Tatum",
]
LAST = [
    "Abbott",
    "Bergeron",
    "Chen",
    "Desrosiers",
    "Ellis",
    "Fraser",
    "Gagnon",
    "Hughes",
    "Ibrahim",
    "Jensen",
    "Kowalski",
    "Leblanc",
    "MacDonald",
    "Nguyen",
    "Okafor",
    "Patel",
    "Roy",
    "Singh",
    "Tremblay",
    "Wong",
]

# (department, title, share of roster, habits, may perform lookalikes)
DEPARTMENTS: tuple[tuple[str, str, float, dict[str, float], bool], ...] = (
    ("Operations", "Operations Officer", 0.30, {"email_send": 1.4, "email_read": 1.4}, False),
    ("Logistics", "Supply Technician", 0.20, {"file_share": 1.6, "web_browse": 1.2}, False),
    ("Finance", "Finance Clerk", 0.15, {"file_share": 1.4, "email_read": 1.2}, False),
    ("HR", "HR Administrator", 0.10, {"email_send": 1.6, "web_browse": 1.2}, False),
    ("IT", "Systems Administrator", 0.15, {"ssh_admin": 6.0, "ad_logon": 2.0, "dns_lookup": 1.5}, True),
    ("Command", "Staff Officer", 0.10, {"email_read": 2.0, "email_send": 1.2, "file_share": 0.6}, False),
)


def builtin_roster(count: int, nodes: list[str], *, seed: int = 1, domain: str = "corp.local") -> list[dict]:
    """``count`` personas spread round-robin over ``nodes``. Deterministic per seed."""
    rng = _rng(seed, "roster")
    people: list[dict] = []
    used: set[str] = set()
    plan: list[tuple[str, str, dict[str, float], bool]] = []
    for dept, title, share, habits, look in DEPARTMENTS:
        plan += [(dept, title, habits, look)] * max(1, round(count * share))
    plan = plan[:count]
    while len(plan) < count:
        plan.append(plan[len(plan) % len(DEPARTMENTS)])
    for i, (dept, title, habits, look) in enumerate(plan):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        handle = f"{first[0]}{last}".lower()
        n = 2
        while handle in used:
            handle = f"{first[0]}{last}{n}".lower()
            n += 1
        used.add(handle)
        start = rng.choice((7, 8, 8, 9))
        people.append(
            {
                "handle": handle,
                "display_name": f"{first} {last}",
                "title": title,
                "department": dept,
                "node": nodes[i % len(nodes)] if nodes else "",
                "work_start": start,
                "work_end": start + 9,
                "habits": dict(habits),
                "lookalikes": look,
                "attrs": {"email": f"{handle}@{domain}"},
            }
        )
    # Everybody writes mostly to their own department, sometimes to command.
    for p in people:
        peers = [q["attrs"]["email"] for q in people if q["department"] == p["department"] and q is not p]
        cmd = [q["attrs"]["email"] for q in people if q["department"] == "Command" and q is not p]
        p["attrs"]["contacts"] = (peers[:6] + cmd[:2]) or [p["attrs"]["email"]]
    return people
