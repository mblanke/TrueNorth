from __future__ import annotations

import uuid
from datetime import UTC, datetime

from _shared import DEV_TENANT, OTHER_TENANT, acting_as
from app.models import AuditLog, UserRole
from app.result_transfer.contract import generate_signing_keypair


def _body(public_key: str) -> dict:
    return {
        "purpose": "subject-signing",
        "remote_enclave": "secret",
        "public_key": public_key,
        "not_before": datetime(2026, 10, 24, 12, tzinfo=UTC).isoformat(),
    }


def test_platform_admin_enrolls_lists_and_retires_public_trust_key(client, db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    monkeypatch.setenv("PLATFORM_TENANT_ID", DEV_TENANT)
    _, public = generate_signing_keypair()

    created = client.post("/result-transfer/trust-keys", json=_body(public))
    assert created.status_code == 201, created.text
    value = created.json()
    assert value["public_key"] == public and value["active"] is True
    assert client.post("/result-transfer/trust-keys", json=_body(public)).json()["id"] == value["id"]
    assert [row["id"] for row in client.get("/result-transfer/trust-keys").json()] == [value["id"]]

    retired = client.post(f"/result-transfer/trust-keys/{value['id']}/retire")
    assert retired.status_code == 200 and retired.json()["active"] is False
    audits = db_session.query(AuditLog).filter(AuditLog.resource_id == value["id"]).all()
    assert [row.action for row in audits] == ["result_transfer_trust_enroll", "result_transfer_trust_retire"]
    assert all(public not in (row.detail or "") for row in audits)


def test_trust_key_management_is_platform_admin_only(client, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    monkeypatch.setenv("PLATFORM_TENANT_ID", DEV_TENANT)
    _, public = generate_signing_keypair()

    for role in (UserRole.student, UserRole.instructor, UserRole.observer, UserRole.range_ops):
        with acting_as(role):
            assert client.get("/result-transfer/trust-keys").status_code == 403
            assert client.post("/result-transfer/trust-keys", json=_body(public)).status_code == 403
    with acting_as(UserRole.admin, OTHER_TENANT):
        assert client.get("/result-transfer/trust-keys").status_code == 403
        assert client.post("/result-transfer/trust-keys", json=_body(public)).status_code == 403
    missing = client.post(f"/result-transfer/trust-keys/{uuid.uuid4()}/retire")
    assert missing.status_code == 404


def test_api_rejects_wrong_direction_for_local_classification(client, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    monkeypatch.setenv("PLATFORM_TENANT_ID", DEV_TENANT)
    _, public = generate_signing_keypair()
    body = _body(public) | {"purpose": "result-signing"}

    response = client.post("/result-transfer/trust-keys", json=body)

    assert response.status_code == 422
    assert "cannot be enrolled" in response.json()["detail"]
