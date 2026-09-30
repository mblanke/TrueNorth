"""ARC² Course Studio API: runs on disk, jobs queued for the host runner.

Nothing here runs the engine. The runs directory is a temp dir; "the runner" is
simulated by moving queue files into _jobs/ with a result, which is what
tools/arc2/runner.py does.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import UserRole

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
REQUEST = "60 min beginner course on the basics of packet sniffing and using Wireshark"


@pytest.fixture
def runs(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "arc2"
    root.mkdir()
    monkeypatch.setenv("ARC2_RUNS_DIR", str(root))
    monkeypatch.setenv("ARC2_STUDIO_ENABLED", "true")
    return root


@contextmanager
def acting_as(role: UserRole):
    who = CurrentUser(id=str(uuid.uuid4()), email=f"{role.value}@example.test", display_name=role.value,
                      role=role, tenant_id=DEV_TENANT, keycloak_id="kc-test")
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def queued(runs: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((runs / "_queue").glob("*.json"))]


def runner_finishes(runs: Path, result: str, state: str = "done") -> None:
    """What tools/arc2/runner.py does with each queued job."""
    (runs / "_jobs").mkdir(exist_ok=True)
    for path in sorted((runs / "_queue").glob("*.json")):
        job = json.loads(path.read_text())
        job.update(state=state, result=result if state == "done" else None, error=None if state == "done" else result,
                   started_at=job["created_at"], finished_at=job["created_at"])
        (runs / "_jobs" / f"{job['id']}.json").write_text(json.dumps(job))
        path.unlink()


def write_run(runs: Path, slug: str, outline: str = "pending", preview: str = "n/a", done: int = 1, package: bool = False) -> Path:
    run = runs / slug
    (run / "01-blueprint").mkdir(parents=True)
    keys = ["content-architect", "code-generator", "range-engineer", "artifact-creator", "sensor-gateway", "qa-tester", "package-builder"]
    manifest = {
        "slug": slug, "course": {"code": "ARC2-WSB", "title": "Packet Sniffing with Wireshark"},
        "stages": {k: {"state": "done" if i < done else "pending", "attempts": 1 if i < done else 0} for i, k in enumerate(keys)},
        "gates": {"outline": {"state": outline}, "preview": {"state": preview}},
        "qa": {"result": "not_run", "cycle": 0, "findings": []},
        "files": [], "human_actions": [{"id": "a1", "stage": "orchestrator", "category": "package", "text": "x",
                                         "blocks_promotion": True, "status": "open"}],
    }
    (run / "manifest.json").write_text(json.dumps(manifest))
    (run / "01-blueprint" / "outline.yaml").write_text("course: ARC2-WSB\nmodules:\n  - id: mod_001\n    title: Capture basics\n")
    (run / "04-artifacts" / "instructor").mkdir(parents=True)
    (run / "04-artifacts" / "instructor" / "answer_key.md").write_text("answers")
    if package:
        (run / "07-bundle" / "cmi5").mkdir(parents=True)
        (run / "07-bundle" / "cmi5" / "cmi5.xml").write_text("<courseStructure/>")
    return run


def test_off_unless_enabled(client, runs, monkeypatch):
    monkeypatch.delenv("ARC2_STUDIO_ENABLED")
    assert client.get("/arc2/runs").status_code == 404


@pytest.mark.parametrize("role", [UserRole.student, UserRole.observer, UserRole.range_ops])
def test_only_course_authors_may_use_it(client, runs, role):
    with acting_as(role):
        assert client.get("/arc2/runs").status_code == 403
        assert client.post("/arc2/runs", json={"name": "x", "request": REQUEST}).status_code == 403


def test_instructors_may(client, runs):
    with acting_as(UserRole.instructor):
        assert client.get("/arc2/runs").status_code == 200


def test_send_creates_the_project_and_queues_stage_1_only(client, runs):
    r = client.post("/arc2/runs", json={"name": "Packet sniffing basics", "request": REQUEST})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "arc2-packet-sniffing-basics"
    assert body["phase"] == "queued"
    assert body["messages"][0] == {"role": "user", "text": REQUEST, "ts": body["messages"][0]["ts"]}
    [job] = queued(runs)
    assert (job["action"], job["slug"], job["text"]) == ("start", "arc2-packet-sniffing-basics", REQUEST)
    assert [r["slug"] for r in client.get("/arc2/runs").json()["runs"]] == ["arc2-packet-sniffing-basics"]


def test_the_same_name_twice_gets_its_own_run(client, runs):
    a = client.post("/arc2/runs", json={"name": "Wireshark", "request": REQUEST}).json()["slug"]
    b = client.post("/arc2/runs", json={"name": "Wireshark", "request": REQUEST}).json()["slug"]
    assert (a, b) == ("arc2-wireshark", "arc2-wireshark-2")


def test_nothing_can_be_sent_while_arc2_is_working(client, runs):
    slug = client.post("/arc2/runs", json={"name": "Wireshark", "request": REQUEST}).json()["slug"]
    r = client.post(f"/arc2/runs/{slug}/reply", json={"action": "accept"})
    assert r.status_code == 409
    assert "still working" in r.json()["detail"]


def test_the_runners_report_becomes_a_pipeline_message_and_the_outline_is_shown(client, runs):
    slug = client.post("/arc2/runs", json={"name": "Wireshark", "request": REQUEST}).json()["slug"]
    runner_finishes(runs, "Outline ready: 2 modules, 4 objectives. Accept or send feedback.")
    write_run(runs, slug, outline="pending", done=1)
    d = client.get(f"/arc2/runs/{slug}").json()
    assert d["phase"] == "outline"
    assert [m["role"] for m in d["messages"]] == ["user", "pipeline"]
    assert d["outline"]["modules"][0]["title"] == "Capture basics"
    assert d["stages"][0]["state"] == "done" and d["stages"][1]["state"] == "pending"


def test_accept_and_feedback_resume_the_run(client, runs):
    slug = client.post("/arc2/runs", json={"name": "Wireshark", "request": REQUEST}).json()["slug"]
    runner_finishes(runs, "Outline ready.")
    write_run(runs, slug, outline="pending", done=1)

    assert client.post(f"/arc2/runs/{slug}/reply", json={"action": "feedback", "text": "  "}).status_code == 422
    r = client.post(f"/arc2/runs/{slug}/reply", json={"action": "feedback", "text": "Add a module on\ncapture filters"})
    assert r.status_code == 200
    assert queued(runs)[-1]["text"] == "Add a module on capture filters"
    runner_finishes(runs, "Outline revised.")

    r = client.post(f"/arc2/runs/{slug}/reply", json={"action": "accept"})
    assert r.status_code == 200
    job = queued(runs)[-1]
    assert (job["action"], job["text"]) == ("resume", "accept")
    assert r.json()["messages"][-1]["text"] == "Outline accepted."


def test_there_is_nothing_to_accept_on_a_packaged_run(client, runs):
    write_run(runs, "arc2-done-course", outline="accepted", preview="accepted", done=7, package=True)
    assert client.post("/arc2/runs/arc2-done-course/reply", json={"action": "accept"}).status_code == 409


def test_a_run_started_from_claude_code_shows_up_too(client, runs):
    write_run(runs, "arc2-from-cli", outline="accepted", preview="pending", done=6)
    [run] = client.get("/arc2/runs").json()["runs"]
    assert (run["slug"], run["phase"], run["actions_open"], run["actions_blocking"]) == ("arc2-from-cli", "preview", 1, 1)


def test_files_stay_inside_the_run_and_instructor_material_is_marked(client, runs):
    write_run(runs, "arc2-files")
    ok = client.get("/arc2/runs/arc2-files/file", params={"path": "04-artifacts/instructor/answer_key.md"}).json()
    assert ok["instructor_only"] is True
    for bad in ["../../etc/passwd", "/etc/passwd", "manifest.json/../../x", "01-blueprint/nothing.md", "run.sh"]:
        assert client.get("/arc2/runs/arc2-files/file", params={"path": bad}).status_code == 404
    assert client.get("/arc2/runs/../file", params={"path": "x"}).status_code == 404


def test_bad_slugs_are_not_found(client, runs):
    assert client.get("/arc2/runs/not-arc2").status_code == 404
    assert client.post("/arc2/runs/NOPE/reply", json={"action": "accept"}).status_code == 404


def test_the_package_downloads_as_a_zip_with_cmi5_xml_at_the_root(client, runs):
    write_run(runs, "arc2-pkg")
    assert client.get("/arc2/runs/arc2-pkg/package.zip").status_code == 404
    write_run(runs, "arc2-pkg2", outline="accepted", preview="accepted", done=7, package=True)
    r = client.get("/arc2/runs/arc2-pkg2/package.zip")
    assert r.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(r.content)).namelist() == ["cmi5.xml"]


def test_a_failed_start_says_so(client, runs):
    slug = client.post("/arc2/runs", json={"name": "Wireshark", "request": REQUEST}).json()["slug"]
    runner_finishes(runs, "could not start claude: not found", state="failed")
    d = client.get(f"/arc2/runs/{slug}").json()
    assert d["phase"] == "failed"
    assert d["messages"][-1]["error"] is True
