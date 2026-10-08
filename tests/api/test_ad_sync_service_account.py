"""/ad-sync calls Keycloak's Admin API as the API's own service account.

Client credentials against the application realm (truenorth-api-admin, realm-management
view-realm + manage-users only); never the master realm's admin user and password.
"""

from __future__ import annotations

from app.routers import ad_sync


class _Resp:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"access_token": "tok"}


def test_token_is_client_credentials_in_the_application_realm(monkeypatch):
    seen: dict = {}

    def fake_post(url, data=None, timeout=None):
        seen["url"], seen["data"] = url, data
        return _Resp()

    monkeypatch.setattr(ad_sync.httpx, "post", fake_post)
    monkeypatch.setenv("KEYCLOAK_URL", "http://keycloak:8080/auth")
    monkeypatch.setenv("KEYCLOAK_REALM", "truenorth")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "truenorth-api-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", "must-not-be-used")

    assert ad_sync._admin_token(ad_sync._keycloak_config()) == "tok"
    assert seen["url"] == "http://keycloak:8080/auth/realms/truenorth/protocol/openid-connect/token"
    assert seen["data"] == {
        "grant_type": "client_credentials",
        "client_id": "truenorth-api-admin",
        "client_secret": "s3cret",
    }
    assert "must-not-be-used" not in str(seen)
