"""Integration test: QSP ingest -> developmental path -> Student progress -> progress endpoints.

Against the live itest stack (``make itest``; AUTH_DISABLED, so every call is the seeded
dev admin, ``00000000-0000-0000-0000-000000000001``). The fixtures are the ones the API
unit tests already ingest (``tests/api/_release_kit.py``): the 17-row QSP crosswalk, the
programme catalogue, and one authored course, C103, whose last module delivers
ALJQ/PO_006 and carries a quiz.

    1. POST /qsp/import-crosswalk           the Qualification/PO/EO spine
    2. POST /courses/import-programme        the catalogue (C103 must exist before its content)
    3. POST /courses/import-course-content   C103's modules, its PO_006 binding and quizzes
    4. POST /qsp/generate-learning-paths     the developmental path; C103 is on it
    5. a Student account (POST /users), enrolled by the admin, the course completed for
       them: visible to the admin through GET /courses/{id}/progress/{user},
       /courses/{id}/enrollments and /users/{user}/transcript
    6. the dev admin, as the learner the stack can sign in as, passes the PO_006 quiz:
       module progress is recorded and GET /qsp/curriculum-map shows PO_006 completed
       on the ALJQ node

Only the dev admin can call the API here, so step 6 is how the stack exercises the path a
Student's own quiz submission takes (``quizzes.submit_attempt`` -> module progress ->
``qsp_progress``); step 5 is how a real Student-role record is recorded and read. Every
step is idempotent, so the test can run again on the same stack.
"""

from __future__ import annotations

import json
import pathlib
import uuid

import pytest

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).resolve().parents[2]
CROSSWALK = ROOT / "truenorth-content-pack/truenorth-content/crosswalk.csv"
CATALOGUE = ROOT / "content/catalogue/cyber_operator_programme.csv"
COURSE_YAML = ROOT / "content/courses/c103-intro-to-networking.yaml"
DEV_ADMIN = "00000000-0000-0000-0000-000000000001"
QSP, PO = "ALJQ", "PO_006"


def _ok(resp, *codes: int) -> dict | list:
    codes = codes or (200,)
    assert resp.status_code in codes, f"{resp.request.method} {resp.request.url} => {resp.status_code} {resp.text}"
    return resp.json() if resp.content else {}


def _upload(client, path: str, file: pathlib.Path, content_type: str) -> dict:
    with file.open("rb") as fh:
        return _ok(client.post(path, files={"file": (file.name, fh, content_type)}))


def _find_course(client, code: str) -> dict:
    page = _ok(client.get("/courses", params={"limit": 200}))
    matches = [c for c in page["items"] if c["name"].startswith(f"{code} — ")]
    assert len(matches) == 1, f"expected one catalogue course {code}, got {[c['name'] for c in matches]}"
    return matches[0]


def _node(curriculum_map: dict, qsp_code: str) -> dict:
    return next(n for n in curriculum_map["nodes"] if n["qsp_code"] == qsp_code)


@pytest.fixture(scope="module")
def ingested(api_client):
    """Steps 1-4: spine, catalogue, C103 content, generated paths. Returns C103's outline."""
    spine = _upload(api_client, "/qsp/import-crosswalk", CROSSWALK, "text/csv")
    assert spine["imported"] is True

    catalogue = _upload(api_client, "/courses/import-programme", CATALOGUE, "text/csv")
    assert catalogue["imported"] is True

    content = _upload(api_client, "/courses/import-course-content", COURSE_YAML, "application/x-yaml")
    assert content["imported"] is True and content["course_code"] == "C103"

    paths = _ok(api_client.post("/qsp/generate-learning-paths"))
    assert paths["generated"] is True
    assert paths["qualification_paths"] >= 1 and paths["progression_paths"] >= 1

    course = _find_course(api_client, "C103")
    outline = _ok(api_client.get(f"/courses/{course['id']}/outline"))
    return outline


def _po_module(outline: dict) -> dict:
    modules = [m for m in outline["modules"] if (m.get("delivers") or {}).get("po_code") == PO]
    assert len(modules) == 1, f"C103 should have exactly one module delivering {PO}"
    assert modules[0]["quiz"], f"the {PO} module should carry a quiz"
    return modules[0]


def test_the_spine_and_the_developmental_path_are_built(api_client, ingested):
    quals = {q["qsp_code"]: q for q in _ok(api_client.get("/qsp/qualifications"))}
    assert QSP in quals and quals[QSP]["po_count"] > 0

    objectives = {o["po_code"] for o in _ok(api_client.get(f"/qsp/qualifications/{QSP}/objectives"))}
    assert PO in objectives

    # C103 delivers PO_006, so the generated qualification path runs through it rather
    # than through a generated stub course.
    paths = _ok(api_client.get("/learning-paths"))
    on_a_path = [p for p in paths if ingested["id"] in p["course_ids"]]
    assert on_a_path, "C103 is on no generated learning path"

    progression = _ok(api_client.get("/qsp/developmental-progression"))
    assert QSP in {q["qsp_code"] for q in progression["progression"] + progression["specialty_streams"]}

    curriculum_map = _ok(api_client.get("/qsp/curriculum-map"))
    node = _node(curriculum_map, QSP)
    po6 = next(o for o in node["objectives"] if o["po_code"] == PO)
    assert po6["progress_state"] in ("not_started", "in_progress", "completed", "failed")


def test_a_students_progress_is_recorded_and_readable(api_client, ingested):
    course_id = ingested["id"]
    email = f"itest-student-{uuid.uuid4().hex[:10]}@example.test"
    student = _ok(
        api_client.post(
            "/users",
            json={"email": email, "display_name": "Itest Student", "role": "student", "first_name": "Itest"},
        ),
        201,
    )
    assert student["role"] == "student"
    sid = student["id"]

    enrollment = _ok(api_client.post(f"/courses/{course_id}/enroll", json={"user_id": sid, "course_id": course_id}), 201)
    assert enrollment["user_id"] == sid and enrollment["status"] == "enrolled"

    # One progress row per module, created with the enrolment (app.enrollment).
    progress = _ok(api_client.get(f"/courses/{course_id}/progress/{sid}"))
    assert len(progress) == len(ingested["modules"])
    assert {p["status"] for p in progress} == {"not_started"}

    completed = _ok(api_client.post(f"/courses/{course_id}/complete/{sid}"))
    assert completed["status"] == "completed" and completed["final_grade"]

    roster = _ok(api_client.get(f"/courses/{course_id}/enrollments"))
    mine = [e for e in roster if e["user_id"] == sid]
    assert len(mine) == 1 and mine[0]["status"] == "completed"

    transcript = _ok(api_client.get(f"/users/{sid}/transcript"))
    assert any(e["activity_type"] == "course" and e["completed_at"] for e in transcript["entries"]), transcript


def test_passing_the_po_quiz_moves_the_learner_along_the_path(api_client, ingested):
    course_id = ingested["id"]
    module = _po_module(ingested)
    quiz_id = module["quiz"]["id"]

    enrol = api_client.post(f"/courses/{course_id}/enroll", json={"user_id": DEV_ADMIN, "course_id": course_id})
    assert enrol.status_code in (201, 409), enrol.text  # 409: enrolled by an earlier run

    # Imported quizzes are drafts; the admin publishes this one, then sits it.
    _ok(api_client.patch(f"/quizzes/{quiz_id}", json={"is_published": True}))
    key = {q["id"]: q["correct"] for q in _ok(api_client.get(f"/quizzes/{quiz_id}/questions"))}
    attempt = _ok(api_client.post(f"/quizzes/{quiz_id}/attempts"), 201)
    assert {q["id"] for q in attempt["questions"]} == set(key)
    assert all("correct" not in q for q in attempt["questions"])

    result = _ok(api_client.post(f"/quizzes/attempts/{attempt['attempt_id']}/submit", json={"answers": key}))
    assert result["passed"] is True and result["pct"] == 100.0

    progress = {p["module_id"]: p for p in _ok(api_client.get(f"/courses/{course_id}/progress/{DEV_ADMIN}"))}
    row = progress[module["id"]]
    assert row["status"] == "completed" and row["score"] == 100

    curriculum_map = _ok(api_client.get("/qsp/curriculum-map"))
    node = _node(curriculum_map, QSP)
    po6 = next(o for o in node["objectives"] if o["po_code"] == PO)
    assert po6["progress_state"] == "completed", json.dumps(po6)[:500]
    assert node["progress"]["completed"] >= 1
