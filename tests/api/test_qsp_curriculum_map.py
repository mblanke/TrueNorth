"""Tests for the QSP career map: /qsp/curriculum-map.

Covers the shape the UI draws from (stages, lanes, derived edges), the learner
overlay resolved through the LMS enrolment chain, and — importantly — the query
count, which is what the endpoint exists to fix.
"""

import uuid

import pytest
from sqlalchemy import event

# The dev-mode identity `get_current_user` returns when AUTH_DISABLED is set.
DEV_USER_ID = "00000000-0000-0000-0000-000000000001"


# ── Seeding ───────────────────────────────────────────────────────────────


def _qual(db, qsp_code, nqual, dp_order, track, rank_level, title=""):
    from app.models import Qualification

    q = Qualification(
        id=uuid.uuid4(), qsp_code=qsp_code, nqual=nqual, dp_order=dp_order,
        track=track, rank_level=rank_level, title=title or f"{nqual} qualification",
    )
    db.add(q)
    db.flush()
    return q


def _po(db, qual, po_code, tier="core", duration_min=120, title=""):
    from app.models import PerformanceObjective, POTier

    po = PerformanceObjective(
        id=uuid.uuid4(), qualification_id=qual.id, po_code=po_code,
        title=title or f"Objective {po_code}", tier=POTier(tier),
        duration_min=duration_min, critical_events="[]",
    )
    db.add(po)
    db.flush()
    return po


def _course_for_po(db, po, pass_threshold=70):
    """Create the PO's course + module, mirroring what generate_learning_paths builds."""
    from app.models import Course, CourseModule, ModuleContentType

    course = Course(id=uuid.uuid4(), name=f"Course for {po.po_code}")
    db.add(course)
    db.flush()
    module = CourseModule(
        id=uuid.uuid4(), course_id=course.id, ordinal=0, title=po.title,
        content_type=ModuleContentType.scenario, po_id=po.id,
        pass_threshold=pass_threshold,
    )
    db.add(module)
    db.flush()
    return course, module


def _enrol(db, course, status="in_progress", user_id=DEV_USER_ID):
    from app.models import Enrollment, EnrollmentStatus

    e = Enrollment(
        id=uuid.uuid4(), user_id=user_id, course_id=course.id,
        status=EnrollmentStatus(status),
    )
    db.add(e)
    db.flush()
    return e


def _progress(db, enrollment, module, status="completed", score=90, max_score=100, attempts=1):
    from app.models import ModuleProgress, ModuleProgressStatus

    p = ModuleProgress(
        id=uuid.uuid4(), enrollment_id=enrollment.id, module_id=module.id,
        status=ModuleProgressStatus(status), score=score, max_score=max_score,
        attempts=attempts,
    )
    db.add(p)
    db.flush()
    return p


@pytest.fixture
def spine(db_session):
    """The real shape of the seeded spine: a two-rung ladder plus two specialty forks."""
    aljq = _qual(db_session, "ALJQ", "ALJQ", 1, "progression", "Pte", "Cyber Defence Analyst")
    temp67 = _qual(db_session, "TEMP67", "ACPZ", 2, "progression", "Cpl", "Senior Cyber Defence Analyst")
    temp64 = _qual(db_session, "TEMP64", "ALRA-RED", 2, "specialty", "Cpl", "Red — Adversary Emulation")
    alra = _qual(db_session, "ALRA", "ALRA", 2, "specialty", "Cpl", "Malware Reverse Engineer")

    pos = {
        "ALJQ": [
            _po(db_session, aljq, "PO_002", tier="core", duration_min=180),
            _po(db_session, aljq, "PO_001", tier="gate", duration_min=480),
        ],
        "TEMP67": [_po(db_session, temp67, "PO_008", duration_min=960)],
        "TEMP64": [_po(db_session, temp64, "PO_001")],
        "ALRA": [_po(db_session, alra, "PO_001", tier="gate")],
    }
    db_session.commit()
    return {"quals": {q.qsp_code: q for q in (aljq, temp67, temp64, alra)}, "pos": pos}


def _node(payload, qsp_code):
    return next(n for n in payload["nodes"] if n["qsp_code"] == qsp_code)


def _edge_set(payload):
    return {(e["from"], e["to"], e["kind"]) for e in payload["edges"]}


# ── Structure ─────────────────────────────────────────────────────────────


class TestStages:
    def test_empty_spine_still_returns_the_full_ladder(self, client):
        payload = client.get("/qsp/curriculum-map").json()
        assert [s["dp_order"] for s in payload["stages"]] == [1, 2, 3, 4, 5]
        assert all(s["planned"] for s in payload["stages"])
        assert payload["nodes"] == []
        assert payload["edges"] == []

    def test_ingested_periods_are_not_planned(self, client, spine):
        stages = {s["dp_order"]: s for s in client.get("/qsp/curriculum-map").json()["stages"]}
        assert stages[1]["planned"] is False
        assert stages[2]["planned"] is False
        assert [dp for dp, s in stages.items() if s["planned"]] == [3, 4, 5]

    def test_ingested_ranks_override_the_ladder_defaults(self, client, db_session):
        # DP3 ships with no configured rank; an ingested qualification supplies it.
        _qual(db_session, "DP3QSP", "DP3", 3, "progression", "MCpl")
        db_session.commit()
        stages = {s["dp_order"]: s for s in client.get("/qsp/curriculum-map").json()["stages"]}
        assert stages[3]["rank_level"] == "MCpl"
        assert stages[3]["planned"] is False

    def test_un_ingested_periods_carry_no_invented_rank(self, client, spine):
        """DP3-5 have no source QSP on-box, so the map must not assert a rank for them.

        Guessed CAF rank progression shown to CAF users would be worse than a blank —
        it reads as authoritative. Blank is honest; ingest fills it in.
        """
        stages = {s["dp_order"]: s for s in client.get("/qsp/curriculum-map").json()["stages"]}
        for dp in (3, 4, 5):
            assert stages[dp]["planned"] is True
            assert stages[dp]["rank_level"] == ""


class TestTracks:
    def test_one_shared_ladder_lane_plus_one_lane_per_specialty(self, client, spine):
        tracks = client.get("/qsp/curriculum-map").json()["tracks"]
        assert tracks[0] == {
            "key": "progression", "label": "Core progression", "kind": "progression",
        }
        # Specialty lanes read in label order: "Malware…" before "Red — …".
        assert [t["key"] for t in tracks[1:]] == ["ALRA", "ALRA-RED"]
        assert all(t["kind"] == "specialty" for t in tracks[1:])

    def test_nodes_carry_the_lane_they_belong_to(self, client, spine):
        payload = client.get("/qsp/curriculum-map").json()
        assert _node(payload, "ALJQ")["track_key"] == "progression"
        assert _node(payload, "TEMP67")["track_key"] == "progression"
        assert _node(payload, "TEMP64")["track_key"] == "ALRA-RED"


class TestEdges:
    def test_ladder_branch_and_planned_edges_are_all_derived(self, client, spine):
        assert _edge_set(client.get("/qsp/curriculum-map").json()) == {
            ("ALJQ", "TEMP67", "progression"),
            ("ALJQ", "TEMP64", "branch"),
            ("ALJQ", "ALRA", "branch"),
            ("TEMP67", "planned:3", "planned"),
        }

    def test_no_planned_edge_when_the_ladder_is_complete(self, client, db_session):
        for dp in (1, 2, 3, 4, 5):
            _qual(db_session, f"Q{dp}", f"N{dp}", dp, "progression", "R")
        db_session.commit()
        assert not [e for e in client.get("/qsp/curriculum-map").json()["edges"]
                    if e["kind"] == "planned"]

    def test_specialty_branches_from_the_ladder_rung_below_it(self, client, db_session):
        _qual(db_session, "A", "A", 1, "progression", "Pte")
        _qual(db_session, "B", "B", 2, "progression", "Cpl")
        _qual(db_session, "S", "S", 3, "specialty", "Sgt")
        db_session.commit()
        # The DP3 specialty forks off DP2, not DP1.
        assert ("B", "S", "branch") in _edge_set(client.get("/qsp/curriculum-map").json())


class TestNodes:
    def test_objectives_are_inline_and_in_path_order(self, client, spine):
        """Gate before core — the order the generated learning paths use, not po_code order."""
        aljq = _node(client.get("/qsp/curriculum-map").json(), "ALJQ")
        assert [o["po_code"] for o in aljq["objectives"]] == ["PO_001", "PO_002"]
        assert aljq["po_count"] == 2
        assert aljq["gate_count"] == 1
        assert aljq["total_minutes"] == 660


# ── Learner overlay ───────────────────────────────────────────────────────


class TestProgress:
    def test_unenrolled_learner_sees_an_untouched_map(self, client, spine):
        payload = client.get("/qsp/curriculum-map").json()
        aljq = _node(payload, "ALJQ")
        assert aljq["progress"] == {"completed": 0, "in_progress": 0, "total": 2, "pct": 0}
        assert all(o["progress_state"] == "not_started" for o in aljq["objectives"])

    @pytest.mark.parametrize(
        ("module_status", "score", "expected"),
        [
            ("completed", 90, "completed"),
            ("completed", 40, "failed"),  # attempted, but under the module's pass mark
            ("in_progress", 0, "in_progress"),
            ("skipped", 0, "not_started"),
            ("not_started", 0, "not_started"),
        ],
    )
    def test_module_progress_maps_to_po_state(
        self, client, db_session, spine, module_status, score, expected
    ):
        po = spine["pos"]["ALJQ"][0]
        course, module = _course_for_po(db_session, po)
        enrollment = _enrol(db_session, course)
        _progress(db_session, enrollment, module, status=module_status, score=score, attempts=0)
        db_session.commit()

        objectives = _node(client.get("/qsp/curriculum-map").json(), "ALJQ")["objectives"]
        assert next(o for o in objectives if o["po_code"] == po.po_code)["progress_state"] == expected

    def test_an_attempt_counts_as_started_even_when_the_row_says_not_started(
        self, client, db_session, spine
    ):
        po = spine["pos"]["ALJQ"][0]
        course, module = _course_for_po(db_session, po)
        _progress(
            db_session, _enrol(db_session, course), module,
            status="not_started", score=0, attempts=2,
        )
        db_session.commit()

        objectives = _node(client.get("/qsp/curriculum-map").json(), "ALJQ")["objectives"]
        assert next(o for o in objectives if o["po_code"] == po.po_code)["progress_state"] == "in_progress"

    def test_enrolment_status_is_the_fallback_when_no_module_row_exists(
        self, client, db_session, spine
    ):
        po = spine["pos"]["ALJQ"][0]
        course, _module = _course_for_po(db_session, po)
        _enrol(db_session, course, status="completed")
        db_session.commit()

        objectives = _node(client.get("/qsp/curriculum-map").json(), "ALJQ")["objectives"]
        assert next(o for o in objectives if o["po_code"] == po.po_code)["progress_state"] == "completed"

    def test_another_learners_progress_is_not_shown(self, client, db_session, spine):
        po = spine["pos"]["ALJQ"][0]
        course, module = _course_for_po(db_session, po)
        other = _enrol(db_session, course, user_id=str(uuid.uuid4()))
        _progress(db_session, other, module, status="completed", score=100)
        db_session.commit()

        assert _node(client.get("/qsp/curriculum-map").json(), "ALJQ")["progress"]["completed"] == 0


def _complete_qualification(db_session, spine, qsp_code):
    for po in spine["pos"][qsp_code]:
        course, module = _course_for_po(db_session, po)
        _progress(db_session, _enrol(db_session, course), module, status="completed", score=100)
    db_session.commit()


class TestNodeState:
    def test_entry_node_is_available_and_everything_downstream_is_locked(self, client, spine):
        payload = client.get("/qsp/curriculum-map").json()
        assert _node(payload, "ALJQ")["state"] == "available"
        assert _node(payload, "TEMP67")["state"] == "locked"
        assert _node(payload, "TEMP64")["state"] == "locked"

    def test_partial_progress_makes_the_node_in_progress_without_unlocking_the_next(
        self, client, db_session, spine
    ):
        po = spine["pos"]["ALJQ"][0]
        course, module = _course_for_po(db_session, po)
        _progress(db_session, _enrol(db_session, course), module, status="completed", score=100)
        db_session.commit()

        payload = client.get("/qsp/curriculum-map").json()
        assert _node(payload, "ALJQ")["state"] == "in_progress"
        assert _node(payload, "ALJQ")["progress"] == {
            "completed": 1, "in_progress": 0, "total": 2, "pct": 50,
        }
        assert _node(payload, "TEMP67")["state"] == "locked"

    def test_completing_a_prerequisite_unlocks_the_ladder_and_every_fork(
        self, client, db_session, spine
    ):
        _complete_qualification(db_session, spine, "ALJQ")

        payload = client.get("/qsp/curriculum-map").json()
        assert _node(payload, "ALJQ")["state"] == "complete"
        assert _node(payload, "ALJQ")["progress"]["pct"] == 100
        assert _node(payload, "TEMP67")["state"] == "available"
        assert _node(payload, "TEMP64")["state"] == "available"
        assert _node(payload, "ALRA")["state"] == "available"


class TestLearnerPosition:
    def test_new_learner_starts_at_the_first_objective_of_the_entry_node(self, client, spine):
        assert client.get("/qsp/curriculum-map").json()["learner"] == {
            "current_qsp_code": "ALJQ",
            "current_po_code": "PO_001",  # the gate, i.e. first in path order
        }

    def test_marker_moves_to_the_first_incomplete_objective(self, client, db_session, spine):
        gate = next(p for p in spine["pos"]["ALJQ"] if p.po_code == "PO_001")
        course, module = _course_for_po(db_session, gate)
        _progress(db_session, _enrol(db_session, course), module, status="completed", score=100)
        db_session.commit()

        assert client.get("/qsp/curriculum-map").json()["learner"] == {
            "current_qsp_code": "ALJQ", "current_po_code": "PO_002",
        }

    def test_finishing_a_node_moves_the_marker_onto_the_ladder_not_a_specialty(
        self, client, db_session, spine
    ):
        _complete_qualification(db_session, spine, "ALJQ")
        # TEMP64/ALRA also unlock here; the core progression must win.
        assert client.get("/qsp/curriculum-map").json()["learner"]["current_qsp_code"] == "TEMP67"

    def test_only_ever_one_marker(self, client, db_session, spine):
        _complete_qualification(db_session, spine, "ALJQ")
        _complete_qualification(db_session, spine, "TEMP64")

        learner = client.get("/qsp/curriculum-map").json()["learner"]
        assert set(learner) == {"current_qsp_code", "current_po_code"}
        assert isinstance(learner["current_qsp_code"], str)


# ── The reason this endpoint exists ───────────────────────────────────────


class TestQueryCount:
    """The map replaced a 1+N fan-out over per-qualification endpoints, each of which
    re-queried per objective. Its cost must stay flat as the spine grows."""

    @staticmethod
    def _count_queries(engine, client, url):
        seen = []

        def _record(conn, cursor, statement, params, context, executemany):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", _record)
        try:
            assert client.get(url).status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", _record)
        return len(seen)

    def test_query_count_does_not_grow_with_the_spine(self, engine, client, db_session, spine):
        baseline = self._count_queries(engine, client, "/qsp/curriculum-map")

        # Triple the spine: 4 more qualifications, 12 more objectives.
        for dp in (3, 4, 5, 6):
            qual = _qual(db_session, f"EXTRA{dp}", f"EX{dp}", dp, "progression", "R")
            for n in range(3):
                _po(db_session, qual, f"PO_{dp}{n}")
        db_session.commit()

        grown = self._count_queries(engine, client, "/qsp/curriculum-map")
        assert grown == baseline, (
            f"query count grew from {baseline} to {grown} — the batched lookups in "
            "routers/qsp.py have regressed to per-qualification or per-PO queries"
        )

    def test_the_whole_map_is_a_handful_of_queries(self, engine, client, db_session, spine):
        """With every lookup path live — objectives, modules, enrolments, progress —
        the map is still a fixed handful of queries, not the ~80 the old per-PO
        fan-out cost."""
        for pos in spine["pos"].values():
            for po in pos:
                course, module = _course_for_po(db_session, po)
                _progress(db_session, _enrol(db_session, course), module, status="in_progress")
        db_session.commit()

        count = self._count_queries(engine, client, "/qsp/curriculum-map")
        assert count <= 10, f"{count} queries — expected a fixed handful"

    def test_per_qualification_endpoint_is_batched_too(self, engine, client, spine):
        """The older per-qualification route shares the same batched lookups."""
        count = self._count_queries(engine, client, "/qsp/qualifications/ALJQ/objectives")
        assert count <= 8, f"{count} queries for one qualification — expected a fixed handful"
