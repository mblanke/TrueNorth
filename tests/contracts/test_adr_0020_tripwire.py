"""ADR 0020 tripwires: instructors judge team exercises (docs/adr/0020).

Each test pins one decision of the ADR to the code that enforces it, so the day the code
drifts from the ADR a test goes red. Structural tests scan the API, worker and Angular
panel sources (AST or plain text — the same approach as test_audit_jobs_celery_free.py);
behavioural tests run the in-process units against the shared SQLite fixtures — no live
services, no network.

Decisions NOT covered here (no test possible without them being built or run):
- Decision 6 (Exercise Studio judging guide, `scoring.no_free_credit` /
  `scoring.validators_known`, dry run): the studio is not built (`tools/exstudio` is
  absent; ADR 0020 itself says "not built yet: #332, #334"), so there is nothing to pin.
- The duration clock (ADR 0020 open question) is deliberately unchanged until decided.
"""

from __future__ import annotations

import ast
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app import aar_html, exercise_judging
from app.auth import CurrentUser
from app.detections import names, redaction
from app.detections.evidence import INGESTED_AT, look_for, student_query, window
from app.detections.models import ACHIEVED, CITED, RECORDED, DetectionSubmission
from app.models import Exercise, ExerciseState, Objective, ObjectiveType, Range, Template, User, UserRole
from app.rbac import ROLE_PERMISSIONS, Permission
from app.routers import detections as detections_router
from app.search_backends.detection_query import parse_detection
from app.search_backends.query import QueryError
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "control-plane/api/app"
WORKER = ROOT / "control-plane/worker/worker"
WEB = ROOT / "control-plane/web/src/app"

NOW = datetime.now(UTC)
# The AUTH_DISABLED tenant/user the root conftest seeds (tests/conftest.py DEV_ID).
DEV_TENANT_ID = "00000000-0000-0000-0000-000000000001"


# ── helpers ────────────────────────────────────────────────────────────

def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text())


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _constants(node: ast.AST) -> list[str]:
    """Every string literal in the node, decorators and body alike."""
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _achieved_true_writes(path: Path) -> bool:
    """Does this module mark an objective achieved: ``x.achieved = True`` or
    ``f(..., achieved=True)``? (Clearing it, ``= False``, is a withdraw, not credit.)"""
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Assign):
            targets = node.targets
            values = [node.value]
        elif isinstance(node, ast.Call):
            targets = [kw.value for kw in node.keywords if kw.arg == "achieved"]
            values = targets
        else:
            continue
        for target, value in zip(targets, values, strict=False):
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "achieved"
                and isinstance(value, ast.Constant)
                and value.value is True
            ):
                return True
    return False


def _instructor(name: str = "Sgt Rivera") -> CurrentUser:
    return CurrentUser(
        id=str(uuid.uuid4()),
        email="instructor@example.test",
        display_name=name,
        role=UserRole.instructor,
        tenant_id=DEV_TENANT_ID,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )


def _exercise(db, *, state: ExerciseState = ExerciseState.running, max_score: int = 0) -> Exercise:
    """A team exercise on a real range (FKs are enforced in this suite) with two detection
    objectives worth 60 and 40, started an hour ago."""
    template = Template(name="tpl", yaml="kind: template\n", tenant_id=DEV_TENANT_ID)
    db.add(template)
    db.flush()
    range_ = Range(name="range", template_id=template.id, tenant_id=DEV_TENANT_ID)
    db.add(range_)
    db.flush()
    ex = Exercise(
        name="team ex",
        kind="collective",
        range_id=range_.id,
        tenant_id=DEV_TENANT_ID,
        state=state,
        max_score=max_score,
        started_at=NOW - timedelta(hours=1),
    )
    db.add(ex)
    db.flush()
    for ref, points in (("obj-a", 60), ("obj-b", 40)):
        db.add(
            Objective(
                exercise_id=ex.id,
                ref_id=ref,
                objective_type=ObjectiveType.detection,
                validator=names.QUERY,
                points=points,
            )
        )
    db.flush()
    return ex


def _as_current_user(row: User) -> CurrentUser:
    """The CurrentUser a real request would carry for a User row (routers take one)."""
    return CurrentUser(
        id=str(row.id),
        email=row.email,
        display_name=row.display_name,
        role=row.role,
        tenant_id=DEV_TENANT_ID,
        keycloak_id=row.keycloak_id,
    )


def _student(db) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.test",
        display_name="Student",
        role=UserRole.student,
        tenant_id=DEV_TENANT_ID,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    db.add(user)
    db.flush()
    return user


def _submission(db, ex: Exercise, ref: str, user: User, *, verdict: str = RECORDED) -> DetectionSubmission:
    row = DetectionSubmission(
        tenant_id=ex.tenant_id,
        exercise_id=ex.id,
        objective_ref=ref,
        user_id=user.id,
        query="process_name:rundll32.exe",
        verdict=verdict,
        events_matched=3,
        window_start=NOW,
    )
    db.add(row)
    db.flush()
    return row


# ── Decision 1: no automatic credit; the ack is the only writer ───────


def test_only_the_acknowledgement_marks_an_objective_achieved():
    """ADR 0020 §1: "Nothing else marks it achieved: not a Student detection, not a
    validator, not a mock run." Exactly one module in the whole control plane may write
    ``achieved = True``: the instructor's ack (app/exercise_judging.py)."""
    writers = [
        path.relative_to(ROOT).as_posix()
        for base in (API, WORKER)
        for path in sorted(base.rglob("*.py"))
        if _achieved_true_writes(path)
    ]
    assert writers == ["control-plane/api/app/exercise_judging.py"], writers


def test_the_ack_and_withdraw_routes_need_objective_ack_and_are_audited():
    """ADR 0020 §1: "achieved only when an instructor acknowledges it
    (`POST /exercises/{id}/objectives/{ref}/ack`, `objective:ack`)" and §2: "Both
    re-total the score and are audited." Every ``.../ack`` route (the POST and its DELETE)
    must demand ``objective:ack`` and write its audit event."""
    tree = _tree(API / "routers/exercises.py")
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        routes = [
            d
            for d in node.decorator_list
            if isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr in ("post", "delete")
            and d.args
            and isinstance(d.args[0], ast.Constant)
            and str(d.args[0].value).endswith("/ack")
        ]
        if not routes:
            continue
        checked += 1
        # The permission sits in the signature: `user = Depends(require_permission(Permission.OBJECTIVE_ACK))`.
        permitted = any(
            isinstance(n, ast.Attribute) and n.attr == "OBJECTIVE_ACK" for n in ast.walk(node)
        )
        assert permitted, f"{node.name} does not require objective:ack"
        expected = "ack_objective" if routes[0].func.attr == "post" else "withdraw_objective_ack"
        assert expected in _constants(node), f"{node.name} writes no {expected} audit event"
    assert checked == 2, f"expected the ack POST and DELETE routes, found {checked}"


def test_students_may_submit_detections_but_never_ack():
    """ADR 0020 §1/§3: the instructor judges; a Student's submission is evidence. The
    student role carries ``detection:submit`` and not ``objective:ack``."""
    student = ROLE_PERMISSIONS[UserRole.student]
    assert Permission.DETECTION_SUBMIT in student
    assert Permission.OBJECTIVE_ACK not in student
    assert Permission.OBJECTIVE_ACK in ROLE_PERMISSIONS[UserRole.instructor]


# ── Decision 2: instructors judge with a record; withdraw; re-total ───


def test_acknowledgement_records_who_note_cites_and_cites_become_cited(db_session):
    """ADR 0020 §2: "The acknowledgement records: who judged; the instructor's note;
    optionally, the Student detections the instructor relied on." A cited submission
    becomes ``cited`` and the score re-totals."""
    ex = _exercise(db_session)
    student = _student(db_session)
    sub = _submission(db_session, ex, "obj-a", student)
    obj = exercise_judging.acknowledge(
        db_session, ex.id, "obj-a", exercise_judging.JudgementIn(evidence="Beacon in Zeek", cites=[sub.id]), _instructor()
    )
    judged = json.loads(obj.evidence)
    assert judged["source"] == "instructor"
    assert judged["by"] == "Sgt Rivera"
    assert judged["note"] == "Beacon in Zeek"
    assert judged["cites"] == [str(sub.id)]
    assert sub.verdict == CITED
    assert obj.achieved is True
    assert obj.achieved_at is not None
    assert ex.total_score == 60  # obj-a's points, re-totalled in the same transaction


def test_a_cite_must_be_a_recorded_detection_on_that_objective_in_this_run(db_session):
    """ADR 0020 §2: cites are "ids of submissions on that objective in this run". Anything
    else — an unknown id, another objective's submission, a pre-0020 achieved row — is a
    422, not a silent cite."""
    ex = _exercise(db_session)
    student = _student(db_session)
    other_objective = _submission(db_session, ex, "obj-b", student)
    historical = _submission(db_session, ex, "obj-a", student, verdict=ACHIEVED)
    body = exercise_judging.JudgementIn
    for cites in ([uuid.uuid4()], [other_objective.id], [historical.id]):
        with pytest.raises(HTTPException) as err:
            exercise_judging.acknowledge(db_session, ex.id, "obj-a", body(evidence=None, cites=cites), _instructor())
        assert err.value.status_code == 422
        assert "recorded detection on this objective in this run" in err.value.detail


def test_withdraw_takes_the_judgement_back_and_retotals(db_session):
    """ADR 0020 §2: "An instructor can withdraw a judgement while the exercise is open ...
    Both re-total the score and are audited." The cited submissions go back to
    ``recorded``, the objective is no longer achieved, the score drops."""
    ex = _exercise(db_session)
    sub = _submission(db_session, ex, "obj-a", _student(db_session))
    exercise_judging.acknowledge(db_session, ex.id, "obj-a", exercise_judging.JudgementIn(evidence="note"), _instructor())
    with pytest.raises(HTTPException) as twice:
        exercise_judging.acknowledge(db_session, ex.id, "obj-a", exercise_judging.JudgementIn(), _instructor())
    assert twice.value.status_code == 409  # already judged

    obj = exercise_judging.withdraw(db_session, ex.id, "obj-a")
    assert obj.achieved is False
    assert obj.achieved_at is None
    assert obj.evidence is None
    assert sub.verdict == RECORDED
    assert ex.total_score == 0

    with pytest.raises(HTTPException) as third:
        exercise_judging.withdraw(db_session, ex.id, "obj-a")
    assert third.value.status_code == 409  # nothing to withdraw


def test_withdraw_also_uncredits_pre_0020_automatic_credit(db_session):
    """ADR 0020 adversarial review: "Withdraw also uncredits pre-ADR 0020 automatic
    credit." Old evidence carried ``submission_id`` and the row's verdict was ``achieved``;
    a withdraw uncites it, so class closeout stops counting it."""
    ex = _exercise(db_session)
    sub = _submission(db_session, ex, "obj-a", _student(db_session), verdict=ACHIEVED)
    obj = db_session.query(Objective).filter_by(exercise_id=ex.id, ref_id="obj-a").one()
    obj.achieved = True
    obj.evidence = json.dumps({"source": "automatic", "submission_id": str(sub.id), "on_target": 3})

    exercise_judging.withdraw(db_session, ex.id, "obj-a")

    db_session.refresh(sub)
    assert sub.verdict == RECORDED


# ── Decision 3: detections are evidence, not grades ───────────────────


def test_detections_record_evidence_and_nothing_more():
    """ADR 0020 §3: "It records the query and how many in-window events it matched, and
    nothing more: no answer key, no precision, no verdict, no credit. Submissions are
    capped at 20." No code may write the answer-key columns, and a recorded row's verdict
    is ``recorded``, nothing else."""
    assert detections_router.MAX_SUBMISSIONS == 20
    router = _tree(API / "routers/detections.py")
    for node in ast.walk(router):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not (
                    isinstance(target, ast.Attribute) and target.attr in ("on_target", "threshold", "min_precision")
                ), f"routers/detections.py writes the answer-key column {target.attr}"
                if isinstance(target, ast.Attribute) and target.attr == "verdict":
                    assert isinstance(node.value, ast.Name) and node.value.id == RECORDED, (
                        "routers/detections.py records a verdict other than 'recorded'"
                    )


def test_the_grammar_refuses_labels_and_free_text_and_the_window_is_ingest_time():
    """ADR 0020 §3: the ADR 0005 machinery stays — "the closed query grammar, with
    ground-truth labels refused; the exercise window on server ingest time." A Student
    cannot query a platform label, cannot send free text, and the window runs on
    ``truenorth.ingested_at``, not on anything a sender controls."""
    assert "process_name" in student_query("process_name:rundll32.exe")
    for query in ("tn_ground_truth:*", "inject_action:*", "event.module:truenorth.inject", "rundll32 beacon"):
        with pytest.raises(QueryError):
            student_query(query)
    with pytest.raises(QueryError, match="platform label"):
        parse_detection("tn_ground_truth:*")
    w = window(NOW, NOW + timedelta(hours=1))
    assert list(w["range"]) == [INGESTED_AT] == ["truenorth.ingested_at"]


def test_submissions_reach_the_endpoint_participants_only():
    """ADR 0020 §3: "participants only". The submission endpoint still asks the
    participants gate before anything runs."""
    source = (API / "routers/detections.py").read_text()
    assert "may_submit" in source, "submit_detection no longer checks participants"


def test_a_student_sees_their_own_submissions_and_staff_see_everyones(db_session):
    """ADR 0020 §3: "The Student sees their own submissions; instructors see everyone's on
    the objective they are judging." ``list_detections`` filters on the caller's user_id
    for a non-staff caller and hands staff every submission on the exercise."""
    ex = _exercise(db_session)
    student, other = _student(db_session), _student(db_session)
    mine = _submission(db_session, ex, "obj-a", student)
    theirs = _submission(db_session, ex, "obj-b", other)

    seen = detections_router.list_detections(exercise_id=ex.id, db=db_session, user=_as_current_user(student))
    assert [r.id for r in seen] == [mine.id], "a Student saw another Student's submission"

    staff = detections_router.list_detections(exercise_id=ex.id, db=db_session, user=_instructor())
    assert {r.id for r in staff} == {mine.id, theirs.id}, "staff do not see every submission"


# ── Decision 4: the scenario `query` is guidance, never an answer key ─


def test_the_look_for_is_staff_guidance_and_never_compared():
    """ADR 0020 §4: "The scenario's objective `query` is instructor guidance on what to
    look for. It is never compared with anything. It stays staff-only ... The
    `opensearch_query` validator name stays for now."""
    scenario = "variables:\n  user: jdoe\nobjectives:\n  - id: obj-a\n    params:\n      query: 'process_name:{{ user }}'\n"
    assert look_for(names.QUERY, None, "obj-a", scenario) == "process_name:jdoe"
    assert look_for("manual_ack", None, "obj-a", scenario) is None  # only detection objectives have one
    assert names.QUERY == "opensearch_query"
    # staff-only: the redacted objective a Student sees carries neither the query nor its params
    assert "query" not in redaction.OBJECTIVE_KEYS
    assert "params" not in redaction.OBJECTIVE_KEYS
    # never compared: the submissions endpoint never reads the look-for
    assert "look_for" not in (API / "routers/detections.py").read_text()


# ── Decision 5: mock runs are judged too ──────────────────────────────


def test_a_mock_run_ends_running_and_the_worker_never_judges():
    """ADR 0020 §5: "A mock run no longer achieves its objectives. Like a real run, it
    stays running after its timeline until an instructor completes it." The v2 run task
    returns ``status: running`` after the timeline, and the worker imports nothing that
    knows how to judge or complete an exercise."""
    fn = _function(_tree(WORKER / "exercise_run.py"), "run_scenario_v2")
    statuses = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            statuses |= {
                v.value
                for k, v in zip(node.value.keys, node.value.values, strict=True)
                if isinstance(k, ast.Constant) and k.value == "status" and isinstance(v, ast.Constant)
            }
    assert "running" in statuses, f"run_scenario_v2 ends with {statuses}"
    assert "completed" not in statuses and "achieved" not in statuses

    for path in sorted(WORKER.rglob("*.py")):  # subpackages too (worker/worker/provisioners/)
        imported = set()
        for node in (n for n in ast.walk(_tree(path)) if isinstance(n, (ast.Import, ast.ImportFrom))):
            # Every name a judging module could be reached by, in any import form:
            # `from . import x`, `from .x import y`, `from app.exercise_judging import y`,
            # `import app.exercise_judging`.
            dotted = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                dotted.append(node.module)
            imported.update(part for name in dotted for part in name.split("."))
        judged_by = imported & {"exercise_judging", "exercise_completion", "detections"}
        assert not judged_by, f"worker/{path.name} imports {sorted(judged_by)}"


# ── Consequences: the Student panel copy ──────────────────────────────


def test_the_student_panel_says_recorded_with_no_attempts_countdown():
    """ADR 0020 Consequences: "The Student panel says 'Recorded: your query matched N
    events; your instructor judges the objective'. There is no achieved/missed, and no
    attempts countdown, only the 20-submission bound." The detection panel's message text
    and its verdict words carry exactly that."""
    source = (WEB / "features/exercise-detail/detection-panel.component.ts").read_text()
    assert "Recorded: your query matched" in source, "the panel no longer reports 'Recorded'"
    assert "Your instructor judges the objective." in source, "the panel no longer defers to the instructor"
    assert "submissions left" in source, "the 20-submission bound is no longer shown"
    assert "attempts left" not in source, "an attempts countdown returned"


# ── Adversarial-review items (ADR 0020, "After the adversarial review") ──


def test_the_exercise_lock_re_reads_the_row():
    """ADR 0020 review: "The exercise row lock re-reads the row (`populate_existing`), so
    an ack, a withdraw or a second /complete that overlaps a close is refused, not applied
    after it."""
    attrs = {n.attr for n in ast.walk(_function(_tree(API / "exercise_completion.py"), "lock")) if isinstance(n, ast.Attribute)}
    assert {"populate_existing", "with_for_update"} <= attrs


def test_retotal_fills_a_missing_max_score_and_never_rewrites_a_set_one(db_session):
    """ADR 0020 review: "An exercise made without a maximum (`max_score` 0) takes its
    objectives' total when judged or closed ... A maximum someone set is never rewritten."""
    unset = _exercise(db_session, max_score=0)
    exercise_judging.acknowledge(db_session, unset.id, "obj-a", exercise_judging.JudgementIn(evidence="note"), _instructor())
    assert unset.total_score == 60
    assert unset.max_score == 100  # filled from the objectives' total

    chosen = _exercise(db_session, max_score=150)
    exercise_judging.acknowledge(db_session, chosen.id, "obj-a", exercise_judging.JudgementIn(evidence="note"), _instructor())
    assert chosen.total_score == 60
    assert chosen.max_score == 150  # someone set it; retotal leaves it alone


def test_the_aar_renders_a_judgement_for_the_reader():
    """ADR 0020 review: "The AAR renders a judgement as 'Judged by X: note (cites N
    detections)'."""
    judged = json.dumps({"source": "instructor", "by": "Sgt Rivera", "note": "Beacon in Zeek", "cites": ["a", "b"]})
    assert aar_html.evidence_text(judged) == "Judged by Sgt Rivera: Beacon in Zeek (cites 2 detections)"
    bare = json.dumps({"source": "instructor", "by": "Sgt Rivera", "cites": ["a"]})
    assert aar_html.evidence_text(bare) == "Judged by Sgt Rivera (cites 1 detection)"
    assert aar_html.evidence_text(json.dumps({"source": "instructor", "by": None, "note": ""})) == "Judged by an instructor"
