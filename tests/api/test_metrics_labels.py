"""/metrics labels requests by route template, never by raw path (security sweep H1).

The raw path of a calendar-feed fetch is a bearer credential (ADR 0004). It used to be
copied into ``http_requests_total{path=...}`` and served on an unauthenticated
``/metrics``, which nginx also proxied publicly as ``/api/metrics``.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import User, UserRole

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
REPO = Path(__file__).resolve().parents[2]


def _issue_feed(client, db_session) -> str:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name="instructor",
        role=UserRole.instructor,
        tenant_id=DEV_TENANT,
    )
    db_session.add(u)
    db_session.flush()
    fastapi_app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=str(u.id),
        email=u.email,
        display_name=u.display_name,
        role=u.role,
        tenant_id=str(u.tenant_id),
        keycloak_id=u.keycloak_id,
    )
    try:
        r = client.post("/schedule/feed-token")
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 200, r.text
    return r.json()["url"].split("/api/v1", 1)[1]


def test_a_feed_token_never_reaches_metrics(client, db_session):
    path = _issue_feed(client, db_session)
    token = path.rsplit("/", 1)[1].removesuffix(".ics")
    assert client.get(path).status_code == 200
    assert client.get(f"/api/v1{path}").status_code == 200  # the versioned alias too

    body = client.get("/metrics").text
    assert token not in body
    assert 'path="/schedule/feed/{token}.ics"' in body


def test_unrouted_paths_collapse_to_one_label(client):
    probe = f"/no-such-thing/{uuid.uuid4().hex}"
    assert client.get(probe).status_code == 404
    body = client.get("/metrics").text
    assert probe not in body
    assert 'path="unmatched"' in body


def test_a_scrape_token_when_configured_is_required(client, monkeypatch):
    monkeypatch.setenv("METRICS_SCRAPE_TOKEN", "s3cret-scrape")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret-scrape"})
    assert ok.status_code == 200
    assert "http_requests_total" in ok.text


def test_bundled_nginx_configs_do_not_proxy_metrics():
    for conf in (
        REPO / "control-plane/web/nginx.conf",
        REPO / "infra/platform/nginx/conf.d/truenorth.conf",
    ):
        text = conf.read_text()
        assert re.search(r"location ~ \^/api/\(v1/\)\?metrics/\?\$ \{\s*return 404;", text), conf
