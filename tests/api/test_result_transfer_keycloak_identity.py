from __future__ import annotations

import json
import uuid

import httpx
import pytest
from app.result_transfer.identity_backends import PseudonymousIdentityError
from app.result_transfer.identity_backends.keycloak import (
    ACCOUNT_MARKER,
    KeycloakPseudonymousConfig,
    KeycloakPseudonymousIdentityBackend,
)

SUBJECT = str(uuid.UUID("12345678-1234-5678-1234-567812345678"))


def _backend(requests: list[httpx.Request], *, marked: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "admin-token"})
        if request.method == "POST" and request.url.path.endswith("/users"):
            return httpx.Response(201, headers={"Location": f"https://kc.test/admin/realms/tn/users/{SUBJECT}"})
        if request.method == "GET":
            marker = [ACCOUNT_MARKER] if marked else ["ordinary-user"]
            return httpx.Response(
                200,
                json={
                    "id": SUBJECT,
                    "username": "tn-learner-test",
                    "attributes": {"tn_account_kind": marker},
                },
            )
        return httpx.Response(204)

    return KeycloakPseudonymousIdentityBackend(
        KeycloakPseudonymousConfig(
            base_url="https://kc.test/auth",
            realm="tn",
            client_id="tn-api-admin",
            client_secret="not-logged",
        ),
        transport=httpx.MockTransport(handler),
    )


def _admin_requests(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [request for request in requests if "/admin/realms/" in request.url.path]


def test_create_is_disabled_marker_tagged_and_contains_no_identity() -> None:
    requests: list[httpx.Request] = []
    backend = _backend(requests)

    identity = backend.create_disabled_account()

    create = _admin_requests(requests)[0]
    body = json.loads(create.content)
    assert identity.subject == SUBJECT and identity.username.startswith("tn-learner-")
    assert body == {
        "username": identity.username,
        "enabled": False,
        "emailVerified": False,
        "attributes": {"tn_account_kind": [ACCOUNT_MARKER]},
    }
    serialized = json.dumps(body)
    assert "tenant" not in serialized and "subject_token" not in serialized
    assert "firstName" not in body and "lastName" not in body and "email" not in body
    assert "not-logged" not in repr(backend.config)


def test_temporary_login_stays_disabled_until_separately_enabled() -> None:
    requests: list[httpx.Request] = []
    backend = _backend(requests)

    login = backend.prepare_one_time_login(SUBJECT)
    admin = _admin_requests(requests)
    writes = [(request.method, request.url.path, json.loads(request.content or b"{}")) for request in admin]

    assert login.username == "tn-learner-test" and login.password
    assert "password=" not in repr(login) and login.password not in repr(login)
    assert writes[1][2] == {"enabled": False}
    assert writes[2][1].endswith("/reset-password")
    assert writes[2][2]["temporary"] is True
    assert not any(body.get("enabled") is True for _, _, body in writes)

    backend.enable_account(SUBJECT)
    assert json.loads(_admin_requests(requests)[-1].content) == {"enabled": True}


def test_lifecycle_refuses_an_unmarked_keycloak_account() -> None:
    requests: list[httpx.Request] = []
    backend = _backend(requests, marked=False)

    with pytest.raises(PseudonymousIdentityError, match="not a TrueNorth pseudonymous"):
        backend.disable_account(SUBJECT)

    assert [request.method for request in _admin_requests(requests)] == ["GET"]
