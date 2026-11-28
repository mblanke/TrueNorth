# Author: Marc-Andre Blanke
# Created: 2026-11-27T00:00:00Z
"""The demo cohort's people and plan: 150 fictional Students, their classes and outcomes.

Pure and deterministic: the same seed gives the same roster, so a re-run of
``seed_demo_cohort.py`` finds every Student it made before. No I/O here; the seeding
script turns this plan into API calls.

- 150 Students: Cyber Command x120, Fleet Cyber East x10, Fleet Cyber West x10,
  Cyber Mission Assurance x10. The formation is ``users.unit``, which the Unit readiness
  page filters on.
- One proficiency band per Student (novice .. expert). Cyber Command is mostly junior
  NCMs; the fleet and mission-assurance Students skew more experienced. The band drives
  their competency assertions, attendance and class outcome, so each Student reads the
  same everywhere in the app.
- Six classes of 25, each mixing formations: three closed, two in progress, one
  scheduled. Dates are relative to the day the plan is made, inside the readiness page's
  default window (last 365 days to 90 days ahead).

All names are invented. Emails use the reserved ``truenorth.test`` domain. Nobody here
gets a login or a password.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

DEFAULT_SEED = 150
EMAIL_DOMAIN = "truenorth.test"

UNITS: tuple[tuple[str, int], ...] = (
    ("Cyber Command", 120),
    ("Fleet Cyber East", 10),
    ("Fleet Cyber West", 10),
    ("Cyber Mission Assurance", 10),
)
COHORT_SIZE = sum(n for _, n in UNITS)

LEVELS: tuple[str, ...] = ("novice", "beginner", "intermediate", "advanced", "expert")
# Share of each band, in LEVELS order. Cyber Command is the intake; the rest are specialists.
LEVEL_WEIGHTS: dict[str, tuple[int, ...]] = {
    "Cyber Command": (22, 27, 30, 15, 6),
    "Fleet Cyber East": (5, 20, 35, 28, 12),
    "Fleet Cyber West": (5, 20, 35, 28, 12),
    "Cyber Mission Assurance": (5, 15, 35, 30, 15),
}

# CAF ranks, NCM-heavy; sailors get navy ranks. Experience nudges rank up, never decides it.
RANKS = {
    "army_air": {
        "junior": ("Pte", "Pte", "Cpl", "Cpl", "Cpl", "MCpl"),
        "senior": ("MCpl", "Sgt", "Sgt", "WO", "Lt", "Capt"),
    },
    "navy": {"junior": ("S2", "S1", "S1", "MS", "MS", "LS"), "senior": ("MS", "PO2", "PO2", "PO1", "SLt", "Lt(N)")},
}

BRANCH_BY_UNIT: dict[str, tuple[str, ...]] = {
    "Cyber Command": ("Canadian Army", "Canadian Army", "Royal Canadian Air Force", "Royal Canadian Navy"),
    "Fleet Cyber East": ("Royal Canadian Navy",),
    "Fleet Cyber West": ("Royal Canadian Navy",),
    "Cyber Mission Assurance": ("Canadian Army", "Royal Canadian Air Force", "Royal Canadian Navy"),
}

FIRST_NAMES = (
    "Alex",
    "Amelia",
    "Andre",
    "Anika",
    "Ben",
    "Brooke",
    "Caleb",
    "Camille",
    "Chloe",
    "Colin",
    "Dana",
    "Daniel",
    "Elise",
    "Emma",
    "Ethan",
    "Felix",
    "Gabriel",
    "Grace",
    "Hannah",
    "Isaac",
    "Jade",
    "Jasmine",
    "Jordan",
    "Julien",
    "Kai",
    "Kara",
    "Liam",
    "Lucas",
    "Maya",
    "Megan",
    "Mia",
    "Nadia",
    "Nathan",
    "Noah",
    "Olivia",
    "Omar",
    "Owen",
    "Priya",
    "Quinn",
    "Rachel",
    "Riley",
    "Sam",
    "Sara",
    "Simon",
    "Sophie",
    "Tariq",
    "Theo",
    "Victoria",
    "William",
    "Zoe",
)
LAST_NAMES = (
    "Arsenault",
    "Bergeron",
    "Bouchard",
    "Campbell",
    "Chen",
    "Cote",
    "Dubois",
    "Fraser",
    "Gagnon",
    "Gill",
    "Hamilton",
    "Heppner",
    "Ibrahim",
    "Kowalski",
    "Lambert",
    "Lavoie",
    "Leblanc",
    "MacDonald",
    "MacKenzie",
    "Martin",
    "Morin",
    "Nguyen",
    "Ouellet",
    "Paquette",
    "Patel",
    "Pelletier",
    "Roy",
    "Singh",
    "Stewart",
    "Tanaka",
    "Tremblay",
    "Wilson",
)
CALLSIGN_WORDS = (
    "Aurora",
    "Badger",
    "Borealis",
    "Cedar",
    "Cobalt",
    "Falcon",
    "Granite",
    "Harbour",
    "Kestrel",
    "Lynx",
    "Maple",
    "Narwhal",
    "Osprey",
    "Polar",
    "Raven",
    "Sentinel",
    "Tundra",
    "Vector",
    "Wolverine",
    "Zephyr",
)

# Six classes of 25. ``phase`` decides what the seed records for it:
#   closed:      sessions held, attendance, results, close-out;
#   in_progress: some sessions held and attended, no results yet;
#   scheduled:   roll call only, its sessions still to come.
CLASS_SIZE = 25


@dataclass(frozen=True)
class ClassSpec:
    name: str
    phase: str
    start_offset_days: int  # from the plan's "today"
    length_days: int
    sessions: int  # one per working day booked, at most length_days


CLASSES: tuple[ClassSpec, ...] = (
    ClassSpec("DEMO-CYB-26-01", "closed", -210, 12, 4),
    ClassSpec("DEMO-CYB-26-02", "closed", -140, 12, 4),
    ClassSpec("DEMO-CYB-26-03", "closed", -70, 12, 4),
    ClassSpec("DEMO-CYB-26-04", "in_progress", -6, 14, 4),
    ClassSpec("DEMO-CYB-26-05", "in_progress", -3, 14, 4),
    ClassSpec("DEMO-CYB-26-06", "scheduled", 30, 12, 4),
)


@dataclass(frozen=True)
class Student:
    email: str
    first_name: str
    last_name: str
    rank: str
    service_branch: str
    unit: str
    callsign: str
    level: str

    @property
    def display_name(self) -> str:
        return f"{self.rank} {self.first_name} {self.last_name}"


@dataclass
class ClassPlan:
    spec: ClassSpec
    start: date
    end: date
    members: list[Student] = field(default_factory=list)
    # email -> number of held sessions attended (closed / in-progress classes only)
    attended: dict[str, int] = field(default_factory=dict)
    # email -> "pass" | "fail" | "incomplete" (closed classes only)
    outcomes: dict[str, str] = field(default_factory=dict)


def _rank(rng: random.Random, level: str, branch: str) -> str:
    senior = LEVELS.index(level) >= 3 and rng.random() < 0.6
    ranks = RANKS["navy" if branch == "Royal Canadian Navy" else "army_air"]
    return rng.choice(ranks["senior" if senior else "junior"])


def build_roster(seed: int = DEFAULT_SEED) -> list[Student]:
    """The 150 Students, in a stable order. Same seed, same roster."""
    rng = random.Random(seed)
    used_emails: set[str] = set()
    used_callsigns: set[str] = set()
    roster: list[Student] = []
    for unit, count in UNITS:
        for _ in range(count):
            level = rng.choices(LEVELS, weights=LEVEL_WEIGHTS[unit])[0]
            first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            base = f"{first}.{last}".lower()
            email, n = f"{base}.demo@{EMAIL_DOMAIN}", 2
            while email in used_emails:
                email, n = f"{base}{n}.demo@{EMAIL_DOMAIN}", n + 1
            used_emails.add(email)
            branch = rng.choice(BRANCH_BY_UNIT[unit])
            callsign = f"{rng.choice(CALLSIGN_WORDS)}-{rng.randint(10, 99)}"
            while callsign in used_callsigns:
                callsign = f"{rng.choice(CALLSIGN_WORDS)}-{rng.randint(10, 99)}"
            used_callsigns.add(callsign)
            roster.append(
                Student(
                    email=email,
                    first_name=first,
                    last_name=last,
                    rank=_rank(rng, level, branch),
                    service_branch=branch,
                    unit=unit,
                    callsign=callsign,
                    level=level,
                )
            )
    return roster


def assertion_levels(student: Student, competencies: list[str], seed: int = DEFAULT_SEED) -> dict[str, str]:
    """competency code -> proficiency, for 6-10 of ``competencies``: the Student's band,
    give or take one level. Deterministic per Student."""
    if not competencies:
        return {}
    rng = random.Random(f"{seed}:{student.email}:competency")
    picks = rng.sample(sorted(competencies), k=min(len(competencies), rng.randint(6, 10)))
    centre = LEVELS.index(student.level)
    out = {}
    for code in picks:
        idx = min(len(LEVELS) - 1, max(0, centre + rng.choice((-1, 0, 0, 0, 1))))
        out[code] = LEVELS[idx]
    return out


# Chance of attending a given session, and of passing a closed class, by band.
ATTEND_P = {"novice": 0.72, "beginner": 0.82, "intermediate": 0.9, "advanced": 0.95, "expert": 0.98}
PASS_P = {"novice": 0.45, "beginner": 0.7, "intermediate": 0.88, "advanced": 0.96, "expert": 0.99}


def plan_classes(roster: list[Student], today: date, seed: int = DEFAULT_SEED) -> list[ClassPlan]:
    """Deal the roster into the six classes, every class a mix of formations, and decide
    attendance and outcomes from each Student's band."""
    rng = random.Random(f"{seed}:classes")
    shuffled = list(roster)
    rng.shuffle(shuffled)
    plans = []
    for i, spec in enumerate(CLASSES):
        start = today + timedelta(days=spec.start_offset_days)
        plan = ClassPlan(spec=spec, start=start, end=start + timedelta(days=spec.length_days - 1))
        plan.members = sorted(shuffled[i * CLASS_SIZE : (i + 1) * CLASS_SIZE], key=lambda s: s.email)
        plans.append(plan)

    for plan in plans:
        held = held_sessions(plan, today)
        for s in plan.members:
            if held:
                plan.attended[s.email] = sum(rng.random() < ATTEND_P[s.level] for _ in range(held))
            if plan.spec.phase == "closed":
                share = plan.attended[s.email] / held if held else 0
                if share < 0.5:
                    plan.outcomes[s.email] = "incomplete"
                else:
                    plan.outcomes[s.email] = "pass" if rng.random() < PASS_P[s.level] else "fail"
    return plans


def session_days(plan: ClassPlan) -> list[date]:
    """The days the class's sessions fall on: spread across its dates, weekdays only."""
    days = [plan.start + timedelta(days=d) for d in range(plan.spec.length_days)]
    weekdays = [d for d in days if d.weekday() < 5] or days
    n = min(plan.spec.sessions, len(weekdays))
    step = len(weekdays) / n
    return [weekdays[int(i * step)] for i in range(n)]


def held_sessions(plan: ClassPlan, today: date) -> int:
    """How many of the class's sessions are over by ``today``."""
    if plan.spec.phase == "scheduled":
        return 0
    return sum(1 for d in session_days(plan) if d < today)


def summary(roster: list[Student]) -> dict[str, Counter]:
    """unit -> Counter of levels: the table the seed prints."""
    out: dict[str, Counter] = {unit: Counter() for unit, _ in UNITS}
    for s in roster:
        out[s.unit][s.level] += 1
    return out
