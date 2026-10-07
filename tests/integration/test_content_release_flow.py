"""The accepted C105 release, imported through the live API, is what the course delivers.

Happy path against the real stack (scripts/itest.sh: Postgres, alembic schema, the API
with AUTH_DISABLED, so the caller is the dev admin, who holds course:author):

  1. the spine and the programme catalogue are imported (idempotent);
  2. content/releases/c105-foundations-d0e83b9f6c1f.tar.gz is uploaded to
     POST /course-releases and verified by the API (digests recomputed server side);
  3. accepting it is refused until its six open human actions are acknowledged, then
     succeeds;
  4. C105 appears in GET /courses with the released module titles, and its release
     status names the accepted version.

Steps 2-3 tolerate a stack that already holds this release (a kept local stack): the same
bytes return the existing release with 200, and an accepted release is not accepted twice.
The same flow runs in process in tests/api/test_c105_release.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "content/releases/c105-foundations-d0e83b9f6c1f.tar.gz"
RELEASE_DIGEST = "d0e83b9f6c1f084d81ec2eff6c3222441e68ef8ce92fbd98d2e7ab7a1f265c71"
CATALOGUE = ROOT / "content/catalogue/cyber_operator_programme.csv"
CROSSWALK = ROOT / "truenorth-content-pack/truenorth-content/crosswalk.csv"
MODULE_TITLES = [
    "Security Principles and the CIA Triad",
    "Threat Actors and Attack Surfaces",
    "Access Control and Identity",
    "Applied Cryptography Concepts",
    "Security Governance and Frameworks",
    "Security Operations and the Analyst Role",
]


def _post_file(client, path: str, src: Path, content_type: str):
    with src.open("rb") as fh:
        return client.post(path, files={"file": (src.name, fh, content_type)})


@pytest.fixture(scope="module")
def catalogue(api_client):
    resp = _post_file(api_client, "/qsp/import-crosswalk", CROSSWALK, "text/csv")
    assert resp.status_code == 200, f"import-crosswalk => {resp.status_code} {resp.text}"
    resp = _post_file(api_client, "/courses/import-programme", CATALOGUE, "text/csv")
    assert resp.status_code == 200, f"import-programme => {resp.status_code} {resp.text}"
    return api_client


def test_the_c105_release_imports_and_the_course_lists_it(catalogue):
    client = catalogue
    resp = _post_file(client, "/course-releases", BUNDLE, "application/gzip")
    assert resp.status_code in (200, 201), f"POST /course-releases => {resp.status_code} {resp.text}"
    release = resp.json()
    assert release["release_digest"] == RELEASE_DIGEST
    assert release["catalogue_code"] == "C105"
    assert set(release["activities"].values()) == {"theory"}
    actions = [a["id"] for a in release["open_actions"]]
    assert len(actions) == 6

    if release["state"] == "candidate":
        refused = client.post(f"/course-releases/{release['id']}/accept", json={})
        assert refused.status_code == 409, refused.text
        resp = client.post(
            f"/course-releases/{release['id']}/accept",
            json={"acknowledge_actions": actions, "notes": "integration: tests/integration/test_content_release_flow.py"},
        )
        assert resp.status_code == 200, f"accept => {resp.status_code} {resp.text}"
        release = resp.json()
    assert release["state"] == "accepted"
    assert sorted(release["acknowledged_actions"]) == sorted(actions)

    listed = client.get("/courses", params={"limit": 200})
    assert listed.status_code == 200, listed.text
    assert release["course_id"] in {c["id"] for c in listed.json()["items"]}

    outline = client.get(f"/courses/{release['course_id']}/outline")
    assert outline.status_code == 200, outline.text
    assert [m["title"] for m in outline.json()["modules"]] == MODULE_TITLES

    status = client.get(f"/course-releases/courses/{release['course_id']}")
    assert status.status_code == 200, status.text
    assert status.json()["legacy"] is False
    assert status.json()["active_release_id"] == release["id"]
