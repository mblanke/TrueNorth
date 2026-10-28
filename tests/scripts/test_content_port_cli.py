"""scripts/content_port_inprocess.py: export, verify offline, preview and import from the
command line, through the API in-process as a named platform administrator (the same rules
as the Content view's Port content panel)."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from app import auth
from app.content_port import signing
from app.models import Template, Tenant, User, UserRole

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))  # the content port's test kit
import _inprocess_api  # noqa: E402
import content_port_inprocess as cli  # noqa: E402
from _content_port_kit import DEV, proven_set, template  # noqa: E402


class _Shared:
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


@pytest.fixture
def admin(monkeypatch, client, db_session):
    import app.db

    monkeypatch.setattr(app.db, "SessionLocal", lambda: _Shared(db_session))
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))
    monkeypatch.setenv(signing.TRUSTED_ENV, signing.public_info()["public_key"])
    _inprocess_api._audited.clear()
    u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email="port-admin@example.test",
             display_name="Port", role=UserRole.admin, tenant_id=DEV)
    db_session.add(u)
    db_session.flush()
    yield u.email
    from app.main import app as fastapi_app

    fastapi_app.dependency_overrides.pop(auth.get_token_identity, None)


def _run(admin, *argv):
    return cli.main(["--admin", admin, *argv], connect=_inprocess_api.connect)


def test_export_verify_preview_import(admin, db_session, tmp_path, capsys):
    _t, _s, c = proven_set(db_session)
    site = Tenant(id=uuid.uuid4(), name="New", slug=f"n-{uuid.uuid4().hex[:6]}")
    db_session.add(site)
    db_session.flush()
    out = tmp_path / "bundle.json"
    assert _run(admin, "export", "--item", f"course:{c.id}", "--out", str(out)) == 0
    doc = json.loads(out.read_text())
    assert len(doc["items"]) == 3

    public = signing.public_info()["public_key"]
    assert cli.main(["verify", str(out), "--public-key", public]) == 0
    doc["items"][0]["content"]["name"] = "changed"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc))
    assert cli.main(["verify", str(bad), "--public-key", public]) == 1

    assert _run(admin, "preview", str(out), "--tenant", str(site.id)) == 0
    assert "create" in capsys.readouterr().out
    assert _run(admin, "import", str(out), "--tenant", str(site.id)) == 0  # no --yes: a preview
    assert db_session.query(Template).filter_by(tenant_id=site.id).count() == 0
    assert _run(admin, "import", str(out), "--tenant", str(site.id), "--yes") == 0
    assert db_session.query(Template).filter_by(tenant_id=site.id).count() == 1
    assert _run(admin, "import", str(out), "--tenant", str(site.id), "--yes") == 1  # a replay


def test_export_of_unproven_content_is_refused(admin, db_session, tmp_path, capsys):
    t = template(db_session)
    out = tmp_path / "bundle.json"
    assert _run(admin, "export", "--item", f"range_template:{t.id}", "--out", str(out)) == 3
    assert "No range built from it" in capsys.readouterr().err
    assert not out.exists()


def test_export_does_not_replace_a_file_unless_forced(admin, db_session, tmp_path):
    out = tmp_path / "bundle.json"
    out.write_text("keep")
    t = template(db_session)
    assert _run(admin, "export", "--item", f"range_template:{t.id}", "--out", str(out)) == 2
    assert out.read_text() == "keep"


@pytest.mark.parametrize("raw", [b'{"a": 1, "a": 2}', b'{"a": 1.5}', b'{"a": NaN}'])
def test_verify_reads_as_strictly_as_the_api(tmp_path, raw):
    f = tmp_path / "b.json"
    f.write_bytes(raw)
    assert cli.main(["verify", str(f), "--public-key", signing.public_info()["public_key"]]) == 1


@pytest.mark.parametrize("bad", ["course:../../etc", "folder:" + str(uuid.uuid4()), "course"])
def test_an_item_is_type_and_uuid(bad):
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["export", "--item", bad, "--out", "x"])


def test_an_exercise_template_is_exported_with_what_it_needs_and_imported_not_proven(admin, db_session, tmp_path):
    from _content_port_kit import exercise_template_set
    from app.exercise_templates.models import ExerciseTemplate

    _t, _s, et = exercise_template_set(db_session)
    site = Tenant(id=uuid.uuid4(), name="New", slug=f"n-{uuid.uuid4().hex[:6]}")
    db_session.add(site)
    db_session.flush()
    out = tmp_path / "bundle.json"
    assert _run(admin, "export", "--item", f"exercise_template:{et.id}", "--out", str(out)) == 0
    assert [i["item_type"] for i in json.loads(out.read_text())["items"]] == [
        "range_template", "scenario", "exercise_template"]
    assert cli.main(["verify", str(out), "--public-key", signing.public_info()["public_key"]]) == 0
    assert _run(admin, "import", str(out), "--tenant", str(site.id), "--yes") == 0
    landed = db_session.query(ExerciseTemplate).filter_by(tenant_id=site.id).one()
    assert landed.name == et.name and landed.id != et.id
