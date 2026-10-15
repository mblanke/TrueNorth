"""The installer can load the shipped content/ into an installed host, off by default.

A production install starts with an empty catalogue by design; staging and demo hosts
opt in. The load goes through the API in-process as the bootstrap administrator
(scripts/load_content_inprocess.py), so no token or password is involved.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"


def _task(name: str) -> dict:
    tasks = yaml.safe_load((INSTALL / "roles/tn_seed/tasks/main.yml").read_text())
    return next(t for t in tasks if t.get("name") == name)


def test_off_by_default_and_on_for_staging() -> None:
    defaults = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    for var in ("tn_load_shipped_content", "tn_publish_shipped_content", "tn_load_shipped_demo"):
        assert defaults[var] is False, var
    host = yaml.safe_load((INSTALL / "inventory/staging.yml").read_text())["all"]["children"]["platform"]["hosts"][
        "tn-staging"
    ]
    assert host["tn_load_shipped_content"] is True
    assert host["tn_publish_shipped_content"] is True


def test_the_load_runs_in_process_as_the_bootstrap_admin_without_credentials() -> None:
    task = _task("Seed — load the shipped content")
    assert task["when"] == "tn_load_shipped_content | bool"
    cmd = task["ansible.builtin.command"]["cmd"]
    assert "scripts/load_content_inprocess.py" in cmd and "--admin {{ tn_bootstrap_admin_upn | quote }}" in cmd
    assert "--no-deps" in cmd and ":/srcapp:ro" in cmd
    assert "environment" not in task  # nothing secret is passed to it


def test_the_loader_replaces_only_the_token_check() -> None:
    # The harness is shared with scripts/arc2_batch_inprocess.py (scripts/_inprocess_api.py).
    src = (ROOT / "scripts/load_content_inprocess.py").read_text() + (ROOT / "scripts/_inprocess_api.py").read_text()
    assert "from _inprocess_api import connect" in src
    assert "dependency_overrides[auth.get_token_identity]" in src
    assert "x-csrf-token" in src  # CSRF stays on: the loader echoes the cookie like a browser
    # No credential of any kind: no bearer header, no Keycloak secret, no env lookups. The one
    # environment write allowed turns the rate limiter off (it breaks across TestClient loops).
    src = src.replace('os.environ.setdefault("RATE_LIMIT_ENABLED", "false")', "")
    assert "Authorization" not in src and "KEYCLOAK" not in src and "os.environ" not in src


# ── C105 stays a draft ─────────────────────────────────────────────────────
# Its committed release (content/releases/c105-*.tar.gz) is a CI / CATAPULT cmi5 fixture;
# the course is unpublished pending Standards review. Loading the shipped content with
# tn_publish_shipped_content (staging) must neither upload that release nor publish C105.


def _loader():
    import importlib.util

    spec = importlib.util.spec_from_file_location("load_content_inprocess", ROOT / "scripts/load_content_inprocess.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeApi:
    def __init__(self) -> None:
        self.patched: list[str] = []
        self.rows = {
            "/courses": [
                {"id": "c105", "name": "C105 — Foundations of Cybersecurity", "is_published": False},
                {"id": "c101", "name": "C101 — Computer Architecture & Systems", "is_published": False},
                {"id": "c102", "name": "C102 — Operating-System Fundamentals", "is_published": True},
            ],
            "/quizzes": [
                {"id": "q-c105", "title": "C105 module 1 quiz", "module_id": "m-c105-1", "is_published": False},
                {"id": "q-c101", "title": "C101 module 1 quiz", "module_id": "m-c101-1", "is_published": False},
            ],
            "/learning-paths": [{"id": "lp1", "name": "Programme — DP1", "is_published": False}],
        }

    def get(self, path: str):
        if path == "/courses/c105":
            return 200, {"id": "c105", "modules": [{"id": "m-c105-1"}, {"id": "m-c105-2"}]}
        return 200, self.rows[path.split("?", 1)[0]]

    def _send(self, method, path, body, ctype):
        assert method == "PATCH" and b'"is_published": true' in body
        self.patched.append(path)
        return 200, {}


def test_publish_keeps_c105_and_its_quizzes_as_drafts(capsys) -> None:
    loader = _loader()
    assert sorted(loader.KEEP_DRAFT) == ["C105"]
    api = _FakeApi()
    assert loader.publish_all(api) is True
    assert api.patched == ["/courses/c101", "/quizzes/q-c101", "/learning-paths/lp1"]
    assert "1 kept as drafts (C105)" in capsys.readouterr().out


def test_the_loader_never_uploads_a_release() -> None:
    for script in ("scripts/load_content.py", "scripts/load_content_inprocess.py"):
        src = (ROOT / script).read_text()
        assert '"/course-releases' not in src, script
    assert "content/releases" not in (INSTALL / "roles/tn_seed/tasks/main.yml").read_text()


def test_c105_course_file_is_a_draft() -> None:
    doc = yaml.safe_load((ROOT / "content/courses/c105-foundations-of-cybersecurity.yaml").read_text())
    assert doc["status"] == "draft" and doc["is_published"] is False
