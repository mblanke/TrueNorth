"""ARC² Course Studio: a run belongs to the tenant that created it.

Course authors in one tenant must not see, read, download, retry or steer another
tenant's runs. Cross-tenant access is a 404 (as in app/tenancy.py), never a 403, and a
refused write queues nothing for the runner. A run with no recorded owner (started
from Claude Code, or created before ownership was recorded) is visible to nobody until
tools/arc2/assign_owner.py assigns it.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import UserRole

from tests.api.test_arc2_studio import REQUEST, runner_finishes, write_run

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@pytest.fixture
def runs(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "arc2"
    root.mkdir()
    monkeypatch.setenv("ARC2_RUNS_DIR", str(root))
    monkeypatch.setenv("ARC2_STUDIO_ENABLED", "true")
    return root


@contextmanager
def acting_as(role: UserRole, tenant: str):
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email=f"{role.value}@{tenant[-2:]}.example.test",
        display_name=role.value,
        role=role,
        tenant_id=tenant,
        keycloak_id="kc-test",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def queue_files(runs: Path) -> list[str]:
    return sorted(p.name for p in (runs / "_queue").glob("*.json"))


@pytest.fixture
def foreign_run(client, runs) -> str:
    """A packaged run created through the API by an instructor in OTHER_TENANT, whose last job failed."""
    with acting_as(UserRole.instructor, OTHER_TENANT):
        slug = client.post("/arc2/runs", json={"name": "Their course", "request": REQUEST}).json()["slug"]
    runner_finishes(runs, "Failed to authenticate", state="failed")
    write_run(runs, slug, outline="pending", done=1, package=True)
    return slug


def test_the_owner_records_its_tenant_on_the_run_and_the_job(client, runs):
    with acting_as(UserRole.instructor, OTHER_TENANT):
        slug = client.post("/arc2/runs", json={"name": "Mine", "request": REQUEST}).json()["slug"]
    meta = json.loads((runs / "_studio" / f"{slug}.json").read_text())
    [job] = [json.loads(p.read_text()) for p in (runs / "_queue").glob("*.json")]
    assert meta["tenant_id"] == OTHER_TENANT
    assert job["tenant_id"] == OTHER_TENANT


@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.admin])
def test_another_tenant_does_not_see_the_run_in_the_list(client, runs, foreign_run, role):
    with acting_as(role, DEV_TENANT):
        assert client.get("/arc2/runs").json()["runs"] == []


@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.admin])
@pytest.mark.parametrize("path", ["", "/file?path=04-artifacts/instructor/answer_key.md", "/package.zip"])
def test_another_tenant_cannot_read_the_run(client, runs, foreign_run, role, path):
    with acting_as(role, DEV_TENANT):
        r = client.get(f"/arc2/runs/{foreign_run}{path}")
    assert r.status_code == 404, r.text


@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.admin])
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/retry", {}),
        ("/reply", {"action": "accept"}),
        ("/reply", {"action": "feedback", "text": "Replace module 2"}),
    ],
)
def test_another_tenant_cannot_steer_the_run_and_nothing_is_queued(client, runs, foreign_run, role, path, body):
    before = queue_files(runs)
    chat = (runs / "_studio" / f"{foreign_run}.chat.jsonl").read_text()
    with acting_as(role, DEV_TENANT):
        r = client.post(f"/arc2/runs/{foreign_run}{path}", json=body)
    assert r.status_code == 404, r.text
    assert queue_files(runs) == before
    assert (runs / "_studio" / f"{foreign_run}.chat.jsonl").read_text() == chat


def test_the_owning_tenant_still_can(client, runs, foreign_run):
    with acting_as(UserRole.instructor, OTHER_TENANT):
        assert [r["slug"] for r in client.get("/arc2/runs").json()["runs"]] == [foreign_run]
        assert client.get(f"/arc2/runs/{foreign_run}").status_code == 200
        assert (
            client.get(
                f"/arc2/runs/{foreign_run}/file", params={"path": "04-artifacts/instructor/answer_key.md"}
            ).status_code
            == 200
        )
        assert client.get(f"/arc2/runs/{foreign_run}/package.zip").status_code == 200
        assert client.post(f"/arc2/runs/{foreign_run}/retry").status_code == 200


@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.admin])
@pytest.mark.parametrize("meta", [None, {"name": "Old", "created_by": "someone@example.test"}])
def test_a_run_without_an_owner_is_visible_to_nobody(client, runs, role, meta):
    write_run(runs, "arc2-legacy", outline="pending", done=1, package=True, owner=None)
    if meta is not None:
        (runs / "_studio").mkdir(exist_ok=True)
        (runs / "_studio" / "arc2-legacy.json").write_text(json.dumps(meta))
    with acting_as(role, DEV_TENANT):
        assert client.get("/arc2/runs").json()["runs"] == []
        assert client.get("/arc2/runs/arc2-legacy").status_code == 404
        assert client.get("/arc2/runs/arc2-legacy/package.zip").status_code == 404
        assert client.post("/arc2/runs/arc2-legacy/reply", json={"action": "accept"}).status_code == 404
    assert queue_files(runs) == []


def test_a_new_run_never_takes_over_another_tenants_slug(client, runs, foreign_run):
    with acting_as(UserRole.instructor, DEV_TENANT):
        slug = client.post("/arc2/runs", json={"name": "Their course", "request": REQUEST}).json()["slug"]
    assert slug != foreign_run
    assert json.loads((runs / "_studio" / f"{foreign_run}.json").read_text())["tenant_id"] == OTHER_TENANT


def test_a_send_that_loses_the_race_for_a_slug_picks_another(client, runs, monkeypatch):
    """slug_for is check-then-write; the exclusive create must not overwrite a run claimed in between."""
    from app.routers import arc2_studio

    picks = iter(["arc2-race", "arc2-race-2"])
    monkeypatch.setattr(arc2_studio, "slug_for", lambda name: next(picks))
    (runs / "_studio").mkdir()
    (runs / "_studio" / "arc2-race.json").write_text(json.dumps({"name": "Race", "tenant_id": OTHER_TENANT}))
    with acting_as(UserRole.instructor, DEV_TENANT):
        slug = client.post("/arc2/runs", json={"name": "Race", "request": REQUEST}).json()["slug"]
    assert slug == "arc2-race-2"
    assert json.loads((runs / "_studio" / "arc2-race.json").read_text())["tenant_id"] == OTHER_TENANT


def test_a_package_symlinked_to_another_tenants_run_is_not_served(client, runs, foreign_run):
    with acting_as(UserRole.instructor, DEV_TENANT):
        mine = client.post("/arc2/runs", json={"name": "Mine", "request": REQUEST}).json()["slug"]
        (runs / mine / "07-bundle").mkdir(parents=True)
        (runs / mine / "07-bundle" / "cmi5").symlink_to(runs / foreign_run / "07-bundle" / "cmi5")
        assert client.get(f"/arc2/runs/{mine}/package.zip").status_code == 404


@pytest.mark.parametrize("text", ["fix wording --resume arc2-victim accept", "x --SLUG arc2-victim", "--resume=arc2-v"])
def test_text_cannot_name_another_run_on_the_engines_command_line(client, runs, text):
    with acting_as(UserRole.instructor, DEV_TENANT):
        assert client.post("/arc2/runs", json={"name": "Mine", "request": text + " please"}).status_code == 422
        slug = client.post("/arc2/runs", json={"name": "Mine", "request": REQUEST}).json()["slug"]
        runner_finishes(runs, "Outline ready.")
        write_run(runs, slug, outline="pending", done=1)
        before = queue_files(runs)
        assert client.post(f"/arc2/runs/{slug}/reply", json={"action": "feedback", "text": text}).status_code == 422
        assert queue_files(runs) == before


@pytest.mark.parametrize("link", ["manifest.json", "01-blueprint/outline.yaml", "request.txt"])
def test_run_files_symlinked_to_another_tenants_run_are_not_read(client, runs, foreign_run, link):
    (runs / foreign_run / "request.txt").write_text("their request")
    with acting_as(UserRole.instructor, DEV_TENANT):
        mine = client.post("/arc2/runs", json={"name": "Mine", "request": REQUEST}).json()["slug"]
        meta = runs / "_studio" / f"{mine}.json"
        meta.write_text(json.dumps({k: v for k, v in json.loads(meta.read_text()).items() if k != "request"}))
        target = runs / mine / link
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(runs / foreign_run / link)
        body = client.get(f"/arc2/runs/{mine}").text
    assert "Capture basics" not in body and "Packet Sniffing with Wireshark" not in body
    assert "their request" not in body


def test_a_run_directory_that_is_a_symlink_to_another_run_is_not_read(client, runs, foreign_run):
    with acting_as(UserRole.instructor, DEV_TENANT):
        mine = client.post("/arc2/runs", json={"name": "Mine", "request": REQUEST}).json()["slug"]
        (runs / mine).symlink_to(runs / foreign_run, target_is_directory=True)
        body = client.get(f"/arc2/runs/{mine}").text
        assert client.get(f"/arc2/runs/{mine}/package.zip").status_code == 404
        assert client.get(f"/arc2/runs/{mine}/file", params={"path": "01-blueprint/outline.yaml"}).status_code == 404
    assert "Capture basics" not in body


def test_a_folder_in_the_run_linked_to_another_run_is_not_read_or_served(client, runs, foreign_run):
    with acting_as(UserRole.instructor, DEV_TENANT):
        mine = client.post("/arc2/runs", json={"name": "Mine", "request": REQUEST}).json()["slug"]
        (runs / mine).mkdir()
        (runs / mine / "01-blueprint").symlink_to(runs / foreign_run / "01-blueprint", target_is_directory=True)
        (runs / mine / "07-bundle").symlink_to(runs / foreign_run / "07-bundle", target_is_directory=True)
        assert "Capture basics" not in client.get(f"/arc2/runs/{mine}").text
        assert client.get(f"/arc2/runs/{mine}/file", params={"path": "01-blueprint/outline.yaml"}).status_code == 404
        assert client.get(f"/arc2/runs/{mine}/package.zip").status_code == 404
