"""Detection rules: create through the API.

Nothing tested the create route, which is how it shipped raising TypeError on every
call: `DetectionRule(**data, author=...)` with `author` also in the request body.
"""

import json
import uuid

import pytest
from _shared import OTHER_TENANT, acting_as
from app.models import DetectionRule, UserRole

SIGMA = """title: Suspicious PowerShell Download Cradle
logsource:
  category: process_creation
  product: windows
detection:
  selection:
    CommandLine|contains: DownloadString
  condition: selection
"""


def _rule(**extra):
    return {"title": "PowerShell download cradle", "detection_yaml": SIGMA, "level": "high", **extra}


def test_a_rule_can_be_created(client):
    resp = client.post("/detection-rules", json=_rule())
    assert resp.status_code == 201, resp.text
    assert resp.json()["title"] == "PowerShell download cradle"


def test_the_author_is_the_caller_not_the_body(client):
    resp = client.post("/detection-rules", json=_rule(author="someone else entirely"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["author"] != "someone else entirely"


def test_a_rule_without_a_condition_is_rejected(client):
    bad = SIGMA.replace("  condition: selection\n", "")
    resp = client.post("/detection-rules", json=_rule(detection_yaml=bad))
    assert resp.status_code == 422


# -- error paths (stage 4) ------------------------------------------------------------------
# Invalid Sigma, unknown MITRE ids, and other tenants' rules: every way a request should be
# refused, and that a refusal changes nothing.

INVALID_SIGMA = [
    ("title: [unclosed\n  detection: {", "Invalid YAML"),
    ("- just\n- a list\n", "Root must be a YAML mapping"),
    ("title: t\nlogsource: {product: windows}\n", "Missing required field: 'detection'"),
    ("logsource: {product: windows}\ndetection: {condition: x}\n", "Missing required field: 'title'"),
    ("title: t\ndetection: {condition: x}\n", "Missing required field: 'logsource'"),
    ("title: t\nlogsource: {product: windows}\ndetection: [a, b]\n", "'detection' must be a mapping"),
    ("title: t\nlogsource: windows\ndetection: {condition: x}\n", "'logsource' must be a mapping"),
]


def _create(client, **extra) -> dict:
    resp = client.post("/detection-rules", json=_rule(**extra))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _foreign_rule(db) -> DetectionRule:
    rule = DetectionRule(title="theirs", detection_yaml=SIGMA, tenant_id=uuid.UUID(OTHER_TENANT), level="low",
                         sigma_id=f"their-{uuid.uuid4().hex[:8]}")
    db.add(rule)
    db.commit()
    return rule


class TestValidate:
    def test_valid_sigma_validates_clean(self, client):
        body = client.post("/detection-rules/validate", json={"yaml": SIGMA}).json()
        assert body == {"valid": True, "errors": [], "warnings": []}

    @pytest.mark.parametrize("payload", [{}, {"yaml": ""}])
    def test_no_yaml_is_invalid_not_an_error(self, client, payload):
        body = client.post("/detection-rules/validate", json=payload).json()
        assert body["valid"] is False and body["errors"] == ["No YAML content provided"]

    @pytest.mark.parametrize(("raw", "error"), INVALID_SIGMA)
    def test_invalid_sigma_names_what_is_wrong(self, client, raw, error):
        body = client.post("/detection-rules/validate", json={"yaml": raw}).json()
        assert body["valid"] is False
        assert any(e.startswith(error) for e in body["errors"]), body

    def test_warnings_do_not_make_a_rule_invalid(self, client):
        raw = "title: t\nlogsource: {foo: bar}\nlevel: severe\ndetection: {sel: {a: b}, condition: sel}\n"
        body = client.post("/detection-rules/validate", json={"yaml": raw}).json()
        assert body["valid"] is True and len(body["warnings"]) == 2

    def test_a_non_object_body_is_422(self, client):
        assert client.post("/detection-rules/validate", json=["yaml"]).status_code == 422


class TestCreateErrors:
    @pytest.mark.parametrize(("raw", "error"), INVALID_SIGMA)
    def test_invalid_sigma_is_refused_and_nothing_is_stored(self, client, db_session, raw, error):
        before = db_session.query(DetectionRule).count()
        resp = client.post("/detection-rules", json=_rule(detection_yaml=raw.ljust(10)))
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["message"] == "Invalid Sigma rule" and any(e.startswith(error) for e in detail["errors"])
        assert db_session.query(DetectionRule).count() == before

    @pytest.mark.parametrize("bad", ["T59", "t1059", "attack.t1059", "T1059.1", "TA2", "lateral movement", ""])
    def test_an_unknown_mitre_id_is_refused(self, client, db_session, bad):
        before = db_session.query(DetectionRule).count()
        resp = client.post("/detection-rules", json=_rule(mitre_attack_ids=["T1059.001", bad]))
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["message"] == "Unknown MITRE ATT&CK id" and len(detail["errors"]) == 1
        assert db_session.query(DetectionRule).count() == before

    def test_known_mitre_ids_are_stored(self, client):
        rule = _create(client, mitre_attack_ids=["T1059", "T1059.001", "TA0002"])
        assert json.loads(rule["mitre_attack_ids"]) == ["T1059", "T1059.001", "TA0002"]

    @pytest.mark.parametrize(
        ("bad", "reason"),
        [
            ("T9999", "is not an ATT&CK Enterprise technique"),  # well-formed, never issued
            ("T1059.999", "is not an ATT&CK Enterprise technique"),
            ("TA9999", "is not an ATT&CK Enterprise tactic"),
            ("T1086", "was revoked by MITRE; use T1059.001"),  # PowerShell, now a sub-technique
        ],
    )
    def test_an_id_the_attack_catalogue_refuses_is_422_with_the_offending_ids(self, client, db_session, bad, reason):
        before = db_session.query(DetectionRule).count()
        resp = client.post("/detection-rules", json=_rule(mitre_attack_ids=["T1059.001", bad, "TA0002"]))
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["message"] == "Unknown MITRE ATT&CK id"
        assert detail["invalid_ids"] == [bad]
        assert detail["errors"] == [f"{bad!r} {reason}"]
        assert db_session.query(DetectionRule).count() == before

    def test_a_deprecated_but_unrevoked_id_is_accepted(self, client):
        rule = _create(client, mitre_attack_ids=["T1064"])  # Scripting: deprecated, never revoked
        assert json.loads(rule["mitre_attack_ids"]) == ["T1064"]

    def test_every_offending_id_is_reported(self, client):
        resp = client.post("/detection-rules", json=_rule(mitre_attack_ids=["T9998", "T1059", "T1086", "nope"]))
        assert resp.status_code == 422
        assert resp.json()["detail"]["invalid_ids"] == ["T9998", "T1086", "nope"]

    def test_no_catalogue_means_no_rule_with_mitre_ids_is_stored(self, client, db_session, monkeypatch):
        from app import attack_catalogue, mitre

        def unavailable():
            raise attack_catalogue.CatalogueUnavailableError("gone")

        monkeypatch.setattr(mitre, "load", unavailable)
        before = db_session.query(DetectionRule).count()
        resp = client.post("/detection-rules", json=_rule(mitre_attack_ids=["T1059"]))
        assert resp.status_code == 503 and "catalogue" in resp.json()["detail"]
        assert db_session.query(DetectionRule).count() == before
        assert client.post("/detection-rules", json=_rule()).status_code == 201  # no ids: nothing to check

    @pytest.mark.parametrize(
        "bad",
        [{"level": "severe"}, {"status": "live"}, {"title": ""}, {"detection_yaml": "short"}, {"title": None}],
    )
    def test_schema_violations_are_422(self, client, bad):
        assert client.post("/detection-rules", json=_rule(**bad)).status_code == 422

    def test_a_duplicate_sigma_id_is_409_not_500(self, client):
        _create(client, sigma_id="tn-dup-1")
        resp = client.post("/detection-rules", json=_rule(sigma_id="tn-dup-1"))
        assert resp.status_code == 409

    def test_a_sigma_id_held_by_another_tenant_is_409_without_saying_whose(self, client, db_session):
        theirs = _foreign_rule(db_session)
        resp = client.post("/detection-rules", json=_rule(sigma_id=theirs.sigma_id))
        assert resp.status_code == 409 and OTHER_TENANT not in resp.text and "theirs" not in resp.text

    def test_a_student_cannot_create_update_or_delete(self, client):
        rule = _create(client)
        with acting_as(UserRole.student):
            assert client.post("/detection-rules", json=_rule()).status_code == 403
            assert client.patch(f"/detection-rules/{rule['id']}", json={"title": "x"}).status_code == 403
            assert client.delete(f"/detection-rules/{rule['id']}").status_code == 403


class TestUpdateAndDeleteErrors:
    @pytest.mark.parametrize(("raw", "error"), INVALID_SIGMA)
    def test_invalid_sigma_on_update_leaves_the_rule_as_it_was(self, client, raw, error):
        rule = _create(client)
        resp = client.patch(f"/detection-rules/{rule['id']}", json={"detection_yaml": raw.ljust(10), "title": "new"})
        assert resp.status_code == 422
        stored = client.get(f"/detection-rules/{rule['id']}").json()
        assert (stored["detection_yaml"], stored["title"]) == (SIGMA, rule["title"])

    def test_an_unknown_mitre_id_on_update_leaves_the_rule_as_it_was(self, client):
        rule = _create(client, mitre_attack_ids=["T1003.001"])
        resp = client.patch(f"/detection-rules/{rule['id']}", json={"mitre_attack_ids": ["T1003.001", "mimikatz"]})
        assert resp.status_code == 422 and "mimikatz" in json.dumps(resp.json())
        assert json.loads(client.get(f"/detection-rules/{rule['id']}").json()["mitre_attack_ids"]) == ["T1003.001"]

    def test_clearing_mitre_ids_is_allowed(self, client):
        rule = _create(client, mitre_attack_ids=["T1003.001"])
        resp = client.patch(f"/detection-rules/{rule['id']}", json={"mitre_attack_ids": None})
        assert resp.status_code == 200 and resp.json()["mitre_attack_ids"] is None

    def test_a_missing_rule_is_404_for_every_verb(self, client):
        missing = uuid.uuid4()
        assert client.get(f"/detection-rules/{missing}").status_code == 404
        assert client.patch(f"/detection-rules/{missing}", json={"title": "x"}).status_code == 404
        assert client.delete(f"/detection-rules/{missing}").status_code == 404

    def test_a_deleted_rule_is_gone_and_cannot_be_deleted_twice(self, client):
        rule = _create(client)
        assert client.delete(f"/detection-rules/{rule['id']}").status_code == 204
        assert client.get(f"/detection-rules/{rule['id']}").status_code == 404
        assert client.patch(f"/detection-rules/{rule['id']}", json={"title": "x"}).status_code == 404
        assert client.delete(f"/detection-rules/{rule['id']}").status_code == 404
        assert rule["id"] not in {r["id"] for r in client.get("/detection-rules").json()}

    def test_a_malformed_id_is_422(self, client):
        assert client.get("/detection-rules/not-a-uuid").status_code == 422


class TestCrossTenant:
    def test_another_tenants_rule_is_invisible_and_untouchable(self, client, db_session):
        theirs = _foreign_rule(db_session)
        assert client.get(f"/detection-rules/{theirs.id}").status_code == 404
        assert client.patch(f"/detection-rules/{theirs.id}", json={"title": "pwned"}).status_code == 404
        assert client.delete(f"/detection-rules/{theirs.id}").status_code == 404
        assert str(theirs.id) not in {r["id"] for r in client.get("/detection-rules", params={"limit": 500}).json()}
        assert client.get("/detection-rules", params={"search": "theirs"}).json() == []
        db_session.expire_all()
        assert (theirs.title, theirs.deleted_at) == ("theirs", None)

    def test_each_tenant_lists_only_its_own(self, client, db_session):
        theirs = _foreign_rule(db_session)
        mine = _create(client)
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            listed = {r["id"] for r in client.get("/detection-rules").json()}
        assert str(theirs.id) in listed and mine["id"] not in listed
