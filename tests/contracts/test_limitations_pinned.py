"""Tripwires for the limitation inventory (TN10X-F5, the TAZ #435 return).

Every "reproduced-current" row there describes behaviour that is true TODAY and is a
limitation, not a goal. Each test here asserts one such behaviour, so it passes while the
limitation holds and fails loudly, naming its row, the moment someone removes the
limitation — the signal to update the row's documentation (and the inventory), not to
"fix" the test. A tripwire is not an endorsement: the pinned state is the known gap.

Rows already pinned by an existing test get a comment only, no new test:

* synthetic injects (no host sensor): tests/scenario_engine/test_inject_evidence_labels.py,
  tests/worker/test_inject_dispatch.py;
* no team scoring/standings: tests/scoring/test_scoring.py, tests/api/test_class_setup_200.py;
* backup runs unquiesced: tests/contracts/test_backup_arc2_runs.py;
* vsphere simulator-only / abandon without hypervisor cleanup: tests/api/test_range_abandon.py,
  tests/worker/test_range_lease_heartbeat.py;
* ARC²/C105 test-content enforcement: tests/api/test_release_test_content.py,
  tests/contracts/test_installer_shipped_content.py;
* cmi5 activity bound to one release: tests/api/test_cmi5_lti.py (superseded release),
  tests/api/test_cmi5_lms.py (registration stays on its release);
* an LTI launch never enrols: tests/api/test_cmi5_lti.py::TestLaunch;
* PLATFORM_TENANT_ID warn-only: tests/api/test_platform_tenant_318.py;
* no content accreditation gate: tests/api/test_content_review_api.py.

Rows that are "unproven" in the inventory (alert delivery, real AD, pre-v1.0 index mapping,
installed-host Moodle, endurance, 70k vCPU) need live services and are deliberately absent:
their cheapest falsifier is not a test this suite can run.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from _shared import real_tenant
from app import auth
from app.lti_identity import session as lti_session
from app.models import ExternalPlatform, IntegrationAuthType, User, UserRole
from app.routers import integrations
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "control-plane/web"
CONTROL_PLANE = ROOT / "control-plane"

# ── Row "No global shortcuts/command palette" (no-global-shortcuts) ───────────────────


def test_no_global_shortcut_service_exists():
    """No global shortcut service or command palette exists in the SPA; the only keyboard
    handling is component-local (range-designer's own keydown and its shortcut hints). A
    global one would refute the row: update docs and the inventory, then pin the new
    behaviour instead."""
    pattern = re.compile(r"command[-_ ]?palette|shortcut[-_ ]?service|global[-_ ]?shortcut|hotkey|mousetrap", re.IGNORECASE)
    hits = [
        str(f)
        for f in sorted((WEB / "src").rglob("*"))
        if f.suffix in (".ts", ".html") and f.is_file() and pattern.search(f.read_text(errors="replace"))
    ]
    assert not hits, (
        "Limitation row 'No global shortcuts/command palette' (no-global-shortcuts) no longer "
        f"holds — a global shortcut service / command palette now exists: {hits}. Update the "
        "row's documentation and the limitation inventory; do not delete this test silently."
    )


# ── Row "English-only UI and unproven accessibility" (english-only-ui) ────────────────


def test_no_i18n_or_a11y_tooling_dependency():
    """The SPA ships English-only: no i18n runtime or extraction ($localize, @ngx-translate)
    and no accessibility audit tooling (axe) is depended on anywhere. Adding one refutes the
    row: update docs and the inventory, then pin the new behaviour instead."""
    package = json.loads((WEB / "package.json").read_text())
    wanted = re.compile(r"i18n|localize|ngx-translate|axe", re.IGNORECASE)
    installed = sorted(
        name
        for section in ("dependencies", "devDependencies")
        for name in package.get(section, {})
        if wanted.search(name)
    )
    assert not installed, (
        "Limitation row 'English-only UI and unproven accessibility' (english-only-ui) no "
        f"longer holds — i18n/axe tooling is now a dependency: {installed}. Update the row's "
        "documentation and the limitation inventory; do not delete this test silently."
    )
    angular = (WEB / "angular.json").read_text()
    assert not re.search(r'"i18n"|"localize"', angular, re.IGNORECASE), (
        "Limitation row 'English-only UI and unproven accessibility' (english-only-ui) no "
        "longer holds — angular.json now configures i18n/localize. Update the row's "
        "documentation and the limitation inventory."
    )


# ── Row "Partial encryption; no Vault/KMS/HSM/TDE/disk/SSE/internal TLS" (partial-encryption)
# ──────────────────────────────────────────────────────────────────────────────────────

_KMS_CLIENT_IMPORT = re.compile(
    r"^\s*(?:import|from)\s[^\n#]*\b(vault|kms|hsm|hvac|keyvault|tde)\b", re.IGNORECASE | re.MULTILINE
)
_KMS_CLIENT_CALL = re.compile(r"""\bclient\(\s*["'](kms|vault|hsm)["']""", re.IGNORECASE)


def test_no_vault_kms_hsm_integration_in_the_control_plane():
    """Encryption at rest is the app-level TN_SECRETS_KEY seal only (app/secretbox.py,
    MultiFernet); nothing in the control plane imports a Vault/KMS/HSM/TDE client (or
    reaches one by client("kms")-style construction without such an import), and there is
    no disk/SSE/internal-TLS layer. A real integration refutes the row: update
    docs/security.md and the inventory, then pin the new behaviour instead."""
    hits = []
    for f in sorted(CONTROL_PLANE.rglob("*.py")):
        text = f.read_text(errors="replace")
        if _KMS_CLIENT_IMPORT.search(text) or _KMS_CLIENT_CALL.search(text):
            hits.append(str(f.relative_to(ROOT)))
    assert not hits, (
        "Limitation row 'Partial encryption; no Vault/KMS/HSM/TDE/disk/SSE/internal TLS' "
        f"(partial-encryption) no longer holds — an external key/secret store is now "
        f"imported: {hits}. Update docs/security.md and the limitation inventory."
    )
    seal = (CONTROL_PLANE / "api/app/secretbox.py").read_text()
    assert "MultiFernet" in seal and 'KEY_ENV = "TN_SECRETS_KEY"' in seal, (
        "Limitation row 'Partial encryption' (partial-encryption): the app-level TN_SECRETS_KEY "
        "seal is what this row still names as the only at-rest encryption, and secretbox.py no "
        "longer looks like it. Re-read the row before updating docs/security.md."
    )


# ── Row "LTI requires HTTPS (only implicitly)" (lti-https-implicit) ───────────────────


def test_lti_https_is_only_implicit(monkeypatch):
    """HTTPS is required only implicitly: the launch's state cookie is Secure + SameSite=None
    (so plain-http browsers drop it and the launch is refused), the app's own tool base URL
    default is plain http, and no code path validates the scheme. The escape hatch for
    plain-http deployments is switching the cookie check off (LTI_REQUIRE_STATE_COOKIE=false).
    Making any of that an explicit https enforcement refutes the row: update docs and the
    inventory, then pin the new behaviour instead."""
    flat = re.sub(r"\s+", " ", (ROOT / "control-plane/api/app/routers/integrations.py").read_text())
    assert re.search(r'set_cookie\(\s*lti_state_cookie_name\([a-z_]+\).*?secure=True.*?samesite=["\']none["\']', flat), (
        "Limitation row 'LTI requires HTTPS' (lti-https-implicit): the state cookie is the "
        "implicit HTTPS requirement and is no longer set Secure/SameSite=None. Update the "
        "row's documentation and the limitation inventory."
    )
    declared = re.search(r'TOOL_BASE_URL = os\.getenv\("LTI_TOOL_BASE_URL", "([^"]*)"\)', (ROOT / "control-plane/api/app/lti13.py").read_text())
    assert declared and declared.group(1).startswith("http://"), (
        "Limitation row 'LTI requires HTTPS' (lti-https-implicit): the app's built-in "
        "LTI_TOOL_BASE_URL default is no longer plain http, so the app now ships an https "
        "posture. Update the row's documentation and the limitation inventory."
    )
    # The documented escape hatch: a plain-http tool deployment turns the check off.
    monkeypatch.delenv("LTI_REQUIRE_STATE_COOKIE", raising=False)
    assert integrations._state_cookie_required() is True  # default: the check is on
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("LTI_REQUIRE_STATE_COOKIE", off)
        assert integrations._state_cookie_required() is False, (
            "Limitation row 'LTI requires HTTPS' (lti-https-implicit): the state-cookie check "
            "can no longer be switched off for plain-http deployments "
            f"(LTI_REQUIRE_STATE_COOKIE={off!r}). Update the row's documentation."
        )


# ── Row "LTI hand-off WebSockets unsupported" (ws-rejects-lti) ────────────────────────


@pytest.fixture
def lti_student(db_session):
    """A tenant whose Student exists only because an LTI launch created the account — the
    kind of user a lti-session token is minted for (app/lti_identity/session.py)."""
    tenant = real_tenant(db_session, f"pin-ws-{uuid.uuid4().hex[:6]}")
    issuer = "https://moodle.example.test"
    platform = ExternalPlatform(
        id=uuid.uuid4(),
        name="Moodle",
        slug=f"moodle-{uuid.uuid4().hex[:6]}",
        platform_type="moodle",
        base_url=issuer,
        auth_type=IntegrationAuthType.lti13,
        tenant_id=tenant.id,
        lti_issuer=issuer,
        lti_client_id="client-1",
        lti_deployment_id="dep-1",
        lti_auth_login_url=f"{issuer}/mod/lti/auth.php",
        lti_jwks_url=f"{issuer}/mod/lti/certs.php",
    )
    learner = User(
        id=uuid.uuid4(),
        keycloak_id=f"lti:{platform.id}:lms-7",
        email="learner@lms.example",
        display_name="Learner",
        role=UserRole.student,
        tenant_id=tenant.id,
        is_active=True,
    )
    db_session.add_all([platform, learner])
    db_session.commit()
    return tenant, learner, platform


@pytest.fixture
def real_auth_with_refusing_idp(monkeypatch):
    """Tokens are really checked, and the identity provider accepts nothing: whatever the
    HTTP path still accepts below it accepts as an lti-session token, never as an IdP one."""
    from fastapi import HTTPException

    class RefusingIdP:
        async def validate_token(self, token):
            raise HTTPException(401, "not an IdP token")

    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "get_auth_backend", lambda: RefusingIdP())


def test_an_lti_session_token_signs_in_over_http_but_not_over_websockets(
    client, db_session, lti_student, real_auth_with_refusing_idp
):
    """Row 'LTI hand-off WebSockets unsupported' (ws-rejects-lti). A valid lti-session token
    signs its Student in over HTTP (auth.get_token_identity is the one place that accepts
    typ lti-session), but the /ws handshake only ever validates through the IdP backend
    (app/ws_auth.ws_user), which refuses it: the socket closes 1008. Accepting lti-session
    tokens on /ws refutes the row: update docs and the inventory, then pin the new
    behaviour instead."""
    tenant, learner, platform = lti_student
    token = lti_session.mint_session_token(db_session, learner, platform, 600)

    # Same token, HTTP: accepted, and by the lti-session branch — the IdP refuses everything.
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, f"the lti-session token must be valid over HTTP first: {me.status_code}"
    assert me.json()["user"]["role"] == "student"

    # Same token, WebSocket handshake: refused before the channel is even considered.
    try:
        with client.websocket_connect(f"/ws/tenant.{tenant.id}", subprotocols=["bearer", token]):
            closed = None  # the socket OPENED: the limitation no longer holds at all
    except WebSocketDisconnect as dropped:
        closed = dropped.code
    assert closed == 1008, (
        "Limitation row 'LTI hand-off WebSockets unsupported' (ws-rejects-lti) no longer "
        f"holds — the /ws handshake did not refuse the lti-session token with 1008 "
        f"(socket opened: {closed is None}; closed with: {closed}). Update the row's "
        "documentation and the limitation inventory; do not delete this test silently."
    )
