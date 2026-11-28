# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/load_content_inprocess.py: the catalogue code parser and the publish sweep.
publish_all() runs against a fake in-process API collaborator: courses in KEEP_DRAFT stay
drafts together with their module's quizzes, already-published rows are skipped, drafts are
PATCHed with {"is_published": True} and any publish failure fails the whole run. main() is
not covered here: it needs the /srcapp checkout and a connected API."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "cov_c_load_content_inprocess", ROOT / "scripts" / "load_content_inprocess.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class FakeApi:
    """The slice of lc.Api publish_all() uses: get() for listings and course detail,
    _send() for the PATCHes."""

    def __init__(self, listing: dict):
        self._listing = listing
        self.gets: list[str] = []
        self.sends: list[tuple[str, str, dict, str | None]] = []
        self.send_status = 200

    def get(self, path: str):
        self.gets.append(path)
        return self._listing[path]

    def _send(self, method, path, body, ctype):
        self.sends.append((method, path, json.loads(body), ctype))
        return self.send_status, {"ok": True}


def listing():
    return {
        # 503 on the paged listing exercises the plain-path fallback.
        "/courses?limit=1000": (503, None),
        "/courses": (
            200,
            [
                {"id": "c1", "name": "C101 — Phishing Basics", "is_published": False},
                {"id": "c2", "name": "C105 — Release Fixture", "is_published": False},
                {"id": "c3", "name": "C102 — Already Out", "is_published": True},
            ],
        ),
        "/courses/c2": (200, {"id": "c2", "modules": [{"id": "m9"}]}),
        "/quizzes?limit=1000": (
            200,
            [
                {"id": "q1", "module_id": "m9", "is_published": False},
                {"id": "q2", "module_id": "m7", "is_published": False},
            ],
        ),
        # A dict body must unwrap to its results.
        "/learning-paths?limit=1000": (200, {"results": [{"id": "lp1", "title": "Intro", "is_published": False}]}),
    }


def test_code_takes_the_catalogue_code_from_the_course_name():
    assert mod._code({"name": "C204 — Security Monitoring"}) == "C204"
    assert mod._code({"name": "  C205 — With Leading Spaces"}) == "C205"
    assert mod._code({"name": "C999 No Separator"}) == "C999 No Separator"
    assert mod._code({}) == ""
    assert mod._code({"name": None}) == ""


def test_publish_all_patches_drafts_and_holds_keep_draft_courses_with_their_quizzes(capsys):
    api = FakeApi(listing())
    assert mod.publish_all(api) is True

    patched = {path: body for _, path, body, _ in api.sends}
    assert api.sends[0][:2] == ("PATCH", "/courses/c1")
    assert patched["/courses/c1"] == {"is_published": True}
    assert patched["/quizzes/q2"] == {"is_published": True}
    assert patched["/learning-paths/lp1"] == {"is_published": True}
    # C105 course and the quiz of its module m9 are held, the published row is untouched.
    assert "/courses/c2" not in patched and "/quizzes/q1" not in patched and "/courses/c3" not in patched
    assert api.sends[0][3] == "application/json"
    # The paged listing failed (503), so the fallback fetched the plain path once.
    assert api.gets[:2] == ["/courses?limit=1000", "/courses"]
    out = capsys.readouterr().out
    assert "courses: 3 listed, 1 published now, 0 failed, 1 kept as drafts (C105)" in out
    assert "quizzes: 2 listed, 1 published now, 0 failed" in out


def test_publish_all_accepts_a_custom_keep_draft_set():
    api = FakeApi(listing())
    assert mod.publish_all(api, keep_draft=frozenset()) is True
    patched = {path for _, path, _, _ in api.sends}
    assert "/courses/c2" in patched  # no longer held
    assert "/quizzes/q1" in patched  # with no held course, its quiz is patched too


def test_publish_all_fails_when_a_patch_is_rejected(capsys):
    api = FakeApi(listing())
    api.send_status = 422
    assert mod.publish_all(api) is False
    out = capsys.readouterr().out
    assert "FAIL courses C101 — Phishing Basics: 422" in out
    assert "courses: 3 listed, 0 published now, 1 failed" in out
