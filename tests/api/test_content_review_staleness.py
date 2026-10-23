"""The content clock's rules (ADR 0014 decisions 23 and 24, issue #286).

``app/content_review/staleness.py`` is pure: no database, no clock of its own. These tests
pin down due dates, the clock badge, propagation down the dependency edges (golden template
-> range template -> scenario -> module -> course), what Mark reviewed clears, and that a
cycle in the edges neither loops nor double-counts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.content_review import staleness as st

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

# golden -> template -> scenario -> module -> course, plus a second template on the golden
GOLDEN, TPL, TPL2, SCN, MOD, COURSE, OTHER = "g", "t", "t2", "s", "m", "c", "x"
NAMES = {
    GOLDEN: ("golden template", "Ubuntu 24.04"),
    TPL: ("range template", "Medium Enterprise"),
    TPL2: ("range template", "Small Enterprise"),
    SCN: ("scenario", "SOC Analyst"),
    MOD: ("module", "Phishing triage"),
    COURSE: ("course", "C201 Network Defence"),
    OTHER: ("course", "Unrelated"),
}
# item -> what it depends on
DEPENDS_ON = {TPL: {GOLDEN}, TPL2: {GOLDEN}, SCN: {TPL}, MOD: {SCN}, COURSE: {MOD}}


def graph(reasons=None, depends_on=None) -> st.Graph:
    return st.Graph(reasons or {}, st.used_by(depends_on if depends_on is not None else DEPENDS_ON), NAMES)


# ── due dates and the clock badge ─────────────────────────────────────────────
class TestDueDates:
    def test_due_is_last_review_plus_the_cycle(self):
        last = datetime(2026, 8, 1, tzinfo=UTC)
        assert st.due_at(baseline=datetime(2026, 1, 1, tzinfo=UTC), last_reviewed=last, cycle_days=90) == \
            last + timedelta(days=90)

    def test_never_reviewed_counts_from_the_baseline(self):
        base = datetime(2026, 9, 1, tzinfo=UTC)
        assert st.due_at(baseline=base, last_reviewed=None, cycle_days=120) == base + timedelta(days=120)

    def test_naive_datetimes_are_read_as_utc(self):
        naive = datetime(2026, 8, 1)
        assert st.due_at(baseline=naive, last_reviewed=None, cycle_days=90).tzinfo is not None

    @pytest.mark.parametrize("days", [90, 100, 120])
    def test_cycles_from_90_to_120_are_allowed(self, days):
        assert st.check_cycle(days) == days

    @pytest.mark.parametrize("days", [0, 30, 89, 121, 365, -90])
    def test_cycles_outside_90_to_120_are_refused(self, days):
        with pytest.raises(ValueError, match="90 and 120"):
            st.check_cycle(days)

    def test_overdue_reason_only_once_the_due_date_has_passed(self):
        assert st.overdue_text(NOW + timedelta(seconds=1), NOW) is None
        assert st.overdue_text(NOW, NOW) is None
        assert st.overdue_text(NOW - timedelta(hours=1), NOW) == "Review overdue (due today)"
        assert st.overdue_text(NOW - timedelta(days=12, hours=1), NOW) == "Review overdue by 12 days"
        assert st.overdue_text(NOW - timedelta(days=1, hours=1), NOW) == "Review overdue by 1 day"

    def test_clock_badge_green_far_amber_within_14_days_red_when_stale(self):
        assert st.clock(NOW + timedelta(days=30), NOW, stale=False) == ("green", 30)
        assert st.clock(NOW + timedelta(days=15), NOW, stale=False) == ("green", 15)
        assert st.clock(NOW + timedelta(days=14), NOW, stale=False) == ("amber", 14)
        assert st.clock(NOW + timedelta(days=1), NOW, stale=False) == ("amber", 1)
        assert st.clock(NOW + timedelta(days=60), NOW, stale=True) == ("stale", 60)
        assert st.clock(NOW - timedelta(days=3), NOW, stale=False)[0] == "stale"


# ── propagation ───────────────────────────────────────────────────────────────
class TestFlagStale:
    def test_flagging_a_golden_template_reaches_everything_built_on_it(self):
        g = graph()
        changed = g.flag_stale(GOLDEN, "New Ubuntu 24.04 point release", "feed:release:ubuntu-24.04.4", at=NOW)
        assert changed == {GOLDEN, TPL, TPL2, SCN, MOD, COURSE}
        assert OTHER not in g.reasons or not g.reasons[OTHER]
        own = g.reasons[GOLDEN]
        assert own == [{"origin": GOLDEN, "kind": "release", "text": "New Ubuntu 24.04 point release",
                        "source": "feed:release:ubuntu-24.04.4", "at": NOW.isoformat()}]

    def test_each_downstream_item_says_why_in_words(self):
        g = graph()
        g.flag_stale(GOLDEN, "New point release", "feed:x", at=NOW)
        assert g.reasons[TPL][0]["text"] == "Its golden template Ubuntu 24.04 is stale"
        assert g.reasons[TPL][0]["kind"] == "upstream"
        assert g.reasons[TPL][0]["origin"] == GOLDEN
        # further down, it names the item it reached it through
        assert g.reasons[COURSE][0]["text"] == \
            "Its golden template Ubuntu 24.04 is stale (through module Phishing triage)"
        assert g.reasons[SCN][0]["text"] == \
            "Its golden template Ubuntu 24.04 is stale (through range template Medium Enterprise)"

    def test_flagging_a_template_does_not_reach_upstream_or_siblings(self):
        g = graph()
        changed = g.flag_stale(TPL, "Known-exploited vulnerability", "kev:CVE-2026-0001", kind="vulnerability",
                               at=NOW)
        assert changed == {TPL, SCN, MOD, COURSE}
        assert not g.reasons.get(GOLDEN) and not g.reasons.get(TPL2)

    def test_flagging_twice_with_the_same_source_is_one_reason(self):
        g = graph()
        g.flag_stale(GOLDEN, "release", "feed:x", at=NOW)
        assert g.flag_stale(GOLDEN, "release", "feed:x", at=NOW) == set()
        assert len(g.reasons[GOLDEN]) == 1 and len(g.reasons[COURSE]) == 1

    def test_two_sources_on_one_item_are_two_reasons_but_one_downstream_reason(self):
        g = graph()
        g.flag_stale(GOLDEN, "release", "feed:x", at=NOW)
        g.flag_stale(GOLDEN, "KEV", "kev:1", kind="vulnerability", at=NOW)
        assert len(g.reasons[GOLDEN]) == 2
        assert len(g.reasons[TPL]) == 1

    def test_two_stale_sources_upstream_are_two_downstream_reasons(self):
        g = graph()
        g.flag_stale(GOLDEN, "release", "feed:x", at=NOW)
        g.flag_stale(TPL, "KEV", "kev:1", kind="vulnerability", at=NOW)
        origins = sorted(r["origin"] for r in g.reasons[COURSE])
        assert origins == sorted([GOLDEN, TPL])

    def test_unknown_kind_is_refused(self):
        with pytest.raises(ValueError):
            graph().flag_stale(GOLDEN, "x", "y", kind="upstream", at=NOW)

    def test_an_item_with_no_edges_flags_only_itself(self):
        g = graph()
        assert g.flag_stale(OTHER, "x", "y", at=NOW) == {OTHER}

    def test_stale_means_any_reason(self):
        g = graph()
        assert not g.is_stale(COURSE)
        g.flag_stale(GOLDEN, "x", "y", at=NOW)
        assert g.is_stale(COURSE)


class TestMarkReviewed:
    def test_reviewing_the_source_clears_it_everywhere_downstream(self):
        g = graph()
        g.flag_stale(GOLDEN, "release", "feed:x", at=NOW)
        changed = g.mark_reviewed(GOLDEN)
        assert changed == {GOLDEN, TPL, TPL2, SCN, MOD, COURSE}
        assert not any(g.reasons.get(k) for k in NAMES)

    def test_reviewing_a_downstream_item_keeps_its_upstream_reason(self):
        g = graph()
        g.flag_stale(GOLDEN, "release", "feed:x", at=NOW)
        g.flag_stale(TPL, "KEV", "kev:1", kind="vulnerability", at=NOW)
        changed = g.mark_reviewed(TPL)
        assert TPL in changed
        # TPL's own reason is gone, and with it what it pushed downstream ...
        assert [r["origin"] for r in g.reasons[TPL]] == [GOLDEN]
        assert [r["origin"] for r in g.reasons[COURSE]] == [GOLDEN]
        # ... but the golden template is still stale, and so is everything on it
        assert g.is_stale(TPL) and g.is_stale(COURSE)

    def test_reviewing_an_unflagged_item_changes_nothing(self):
        assert graph().mark_reviewed(COURSE) == set()

    def test_reviewing_clears_the_overdue_reason_too(self):
        g = graph()
        g.set_overdue(COURSE, "Review overdue by 3 days", at=NOW)
        assert g.mark_reviewed(COURSE) == {COURSE}
        assert not g.is_stale(COURSE)


class TestOverdue:
    def test_overdue_marks_the_item_and_does_not_propagate(self):
        g = graph()
        assert g.set_overdue(GOLDEN, "Review overdue by 2 days", at=NOW) is True
        assert g.reasons[GOLDEN] == [{"origin": GOLDEN, "kind": "time", "text": "Review overdue by 2 days",
                                      "source": "clock", "at": NOW.isoformat()}]
        assert not g.reasons.get(TPL)

    def test_overdue_text_is_updated_in_place_and_cleared(self):
        g = graph()
        g.set_overdue(GOLDEN, "Review overdue by 2 days", at=NOW)
        assert g.set_overdue(GOLDEN, "Review overdue by 2 days", at=NOW + timedelta(minutes=5)) is False
        assert g.set_overdue(GOLDEN, "Review overdue by 3 days", at=NOW) is True
        assert len(g.reasons[GOLDEN]) == 1
        assert g.set_overdue(GOLDEN, None, at=NOW) is True
        assert not g.is_stale(GOLDEN)
        assert g.set_overdue(GOLDEN, None, at=NOW) is False

    def test_clearing_overdue_keeps_a_release_flag(self):
        g = graph()
        g.flag_stale(GOLDEN, "release", "feed:x", at=NOW)
        g.set_overdue(GOLDEN, "Review overdue by 2 days", at=NOW)
        g.set_overdue(GOLDEN, None, at=NOW)
        assert [r["kind"] for r in g.reasons[GOLDEN]] == ["release"]


# ── cycles in the edges ───────────────────────────────────────────────────────
class TestCycles:
    CYCLIC = {"a": {"c"}, "b": {"a"}, "c": {"b"}}  # a -> c -> b -> a

    def _g(self):
        names = {k: ("module", k.upper()) for k in "abc"}
        return st.Graph({}, st.used_by(self.CYCLIC), names)

    def test_flagging_inside_a_cycle_terminates_and_reaches_each_member_once(self):
        g = self._g()
        assert g.flag_stale("a", "x", "y", at=NOW) == {"a", "b", "c"}
        assert len(g.reasons["a"]) == 1  # its own reason, not a second one coming back round
        assert [r["kind"] for r in g.reasons["b"]] == ["upstream"]
        assert [r["kind"] for r in g.reasons["c"]] == ["upstream"]

    def test_reviewing_inside_a_cycle_clears_it(self):
        g = self._g()
        g.flag_stale("a", "x", "y", at=NOW)
        assert g.mark_reviewed("a") == {"a", "b", "c"}
        assert not any(g.is_stale(k) for k in "abc")

    def test_a_self_edge_is_harmless(self):
        g = st.Graph({}, st.used_by({"a": {"a"}}), {"a": ("course", "A")})
        assert g.flag_stale("a", "x", "y", at=NOW) == {"a"}
        assert len(g.reasons["a"]) == 1

    def test_dependents_lists_each_item_once_with_its_shortest_path(self):
        g = graph(depends_on={**DEPENDS_ON, COURSE: {MOD, TPL}})  # the course also books the template
        found = dict(g.dependents(GOLDEN))
        assert found[COURSE] == [GOLDEN, TPL, COURSE]
        assert set(found) == {TPL, TPL2, SCN, MOD, COURSE}


# ── re-propagation after the edges change (the periodic sync) ─────────────────
class TestRepropagate:
    def test_new_edges_carry_existing_flags_and_dropped_edges_lose_them(self):
        g = graph()
        g.flag_stale(GOLDEN, "release", "feed:x", at=NOW)
        assert g.is_stale(COURSE)
        # the course stops using the module, and a new course starts using the template
        new = {TPL: {GOLDEN}, TPL2: {GOLDEN}, SCN: {TPL}, MOD: {SCN}, OTHER: {TPL2}}
        g2 = st.Graph(g.reasons, st.used_by(new), NAMES)
        changed = g2.repropagate(at=NOW + timedelta(hours=1))
        assert not g2.is_stale(COURSE)
        assert g2.is_stale(OTHER)
        assert {COURSE, OTHER} <= changed
        assert MOD not in changed  # unchanged reasons keep their original time

    def test_repropagate_keeps_when_a_downstream_reason_was_first_seen(self):
        g = graph()
        g.flag_stale(GOLDEN, "release", "feed:x", at=NOW)
        g2 = st.Graph(g.reasons, st.used_by(DEPENDS_ON), NAMES)
        assert g2.repropagate(at=NOW + timedelta(days=1)) == set()
        assert g2.reasons[COURSE][0]["at"] == NOW.isoformat()

    def test_time_reasons_survive_and_do_not_spread(self):
        g = graph()
        g.set_overdue(GOLDEN, "Review overdue by 1 day", at=NOW)
        g2 = st.Graph(g.reasons, st.used_by(DEPENDS_ON), NAMES)
        g2.repropagate(at=NOW)
        assert g2.is_stale(GOLDEN) and not g2.is_stale(TPL)

    def test_garbage_reasons_are_dropped_not_crashed_on(self):
        g = st.Graph({COURSE: [{"nonsense": 1}, "text", None]}, st.used_by(DEPENDS_ON), NAMES)
        g.repropagate(at=NOW)
        assert not g.is_stale(COURSE)


# ── retired items ─────────────────────────────────────────────────────────────
class TestRetired:
    def test_a_retired_item_is_no_source_when_reasons_are_rebuilt(self):
        g = graph()
        g.flag_stale(TPL, "KEV", "kev:1", kind="vulnerability", at=NOW)
        assert g.is_stale(COURSE)
        g2 = st.Graph(g.reasons, st.used_by(DEPENDS_ON), NAMES, retired={TPL})
        g2.repropagate(at=NOW)
        assert not g2.is_stale(COURSE) and not g2.is_stale(SCN)

    def test_flagging_a_retired_item_does_not_spread(self):
        g = st.Graph({}, st.used_by(DEPENDS_ON), NAMES, retired={GOLDEN})
        assert g.flag_stale(GOLDEN, "release", "feed:x", at=NOW) == {GOLDEN}
        assert not g.is_stale(TPL)
