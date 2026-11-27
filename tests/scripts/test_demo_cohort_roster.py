# Author: Marc-Andre Blanke
# Created: 2026-11-27T00:00:00Z
"""The demo cohort plan (scripts/demo_cohort_roster.py): 150 Students, 120/10/10/10 by
formation, a spread of proficiency, stable by seed, and classes that mix formations."""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import demo_cohort_roster as dc  # noqa: E402

TODAY = date(2026, 11, 27)


def test_roster_is_150_split_120_10_10_10():
    roster = dc.build_roster()
    assert len(roster) == 150 == dc.COHORT_SIZE
    assert Counter(s.unit for s in roster) == {
        "Cyber Command": 120,
        "Fleet Cyber East": 10,
        "Fleet Cyber West": 10,
        "Cyber Mission Assurance": 10,
    }


def test_emails_unique_on_the_test_domain():
    roster = dc.build_roster()
    emails = [s.email for s in roster]
    assert len(set(emails)) == len(emails)
    assert all(e.endswith(".demo@truenorth.test") for e in emails)
    assert len({s.callsign for s in roster}) == len(roster)


def test_same_seed_same_roster_other_seed_differs():
    assert dc.build_roster(7) == dc.build_roster(7)
    assert dc.build_roster(7) != dc.build_roster(8)


def test_every_level_present_and_specialists_skew_higher():
    roster = dc.build_roster()
    assert set(s.level for s in roster) == set(dc.LEVELS)

    def mean(unit_filter):
        levels = [dc.LEVELS.index(s.level) for s in roster if unit_filter(s.unit)]
        return sum(levels) / len(levels)

    assert mean(lambda u: u != "Cyber Command") > mean(lambda u: u == "Cyber Command")


def test_fleet_cyber_are_sailors_with_navy_ranks():
    navy = set(dc.RANKS["navy"]["junior"] + dc.RANKS["navy"]["senior"])
    for s in dc.build_roster():
        if s.unit.startswith("Fleet Cyber"):
            assert s.service_branch == "Royal Canadian Navy"
            assert s.rank in navy


def test_assertions_stay_within_one_level_of_the_band():
    codes = [f"C-{i:02d}" for i in range(20)]
    for s in dc.build_roster()[:40]:
        got = dc.assertion_levels(s, codes)
        assert 6 <= len(got) <= 10
        assert set(got) <= set(codes)
        centre = dc.LEVELS.index(s.level)
        assert all(abs(dc.LEVELS.index(v) - centre) <= 1 for v in got.values())
        assert got == dc.assertion_levels(s, codes)  # deterministic
    assert dc.assertion_levels(dc.build_roster()[0], []) == {}


def test_classes_cover_everyone_once_and_mix_formations():
    roster = dc.build_roster()
    plans = dc.plan_classes(roster, TODAY)
    assert [p.spec.phase for p in plans] == ["closed"] * 3 + ["in_progress"] * 2 + ["scheduled"]
    seen = [s.email for p in plans for s in p.members]
    assert sorted(seen) == sorted(s.email for s in roster)
    assert all(len(p.members) == dc.CLASS_SIZE for p in plans)
    assert sum(len({s.unit for s in p.members}) > 1 for p in plans) >= 5


def test_dates_inside_the_readiness_default_window():
    for p in dc.plan_classes(dc.build_roster(), TODAY):
        assert p.start >= TODAY - timedelta(days=365)
        assert p.end <= TODAY + timedelta(days=90)
        if p.spec.phase == "closed":
            assert p.end < TODAY
        elif p.spec.phase == "in_progress":
            assert p.start < TODAY <= p.end
        else:
            assert p.start > TODAY


def test_outcomes_only_on_closed_classes_with_a_spread():
    plans = dc.plan_classes(dc.build_roster(), TODAY)
    outcomes = Counter()
    for p in plans:
        held = dc.held_sessions(p, TODAY)
        if p.spec.phase == "closed":
            assert set(p.outcomes) == {s.email for s in p.members}
            assert held == p.spec.sessions
            outcomes.update(p.outcomes.values())
        else:
            assert p.outcomes == {}
        if p.spec.phase == "scheduled":
            assert held == 0 and p.attended == {}
        assert all(0 <= n <= held for n in p.attended.values())
    assert outcomes["pass"] > outcomes["fail"] > 0


def test_in_progress_classes_have_some_sessions_held_and_some_to_come():
    for p in dc.plan_classes(dc.build_roster(), TODAY):
        if p.spec.phase == "in_progress":
            assert 0 < dc.held_sessions(p, TODAY) < len(dc.session_days(p))


def test_session_days_are_weekdays_within_the_class():
    for p in dc.plan_classes(dc.build_roster(), TODAY):
        days = dc.session_days(p)
        assert len(days) == p.spec.sessions
        assert all(p.start <= d <= p.end and d.weekday() < 5 for d in days)
