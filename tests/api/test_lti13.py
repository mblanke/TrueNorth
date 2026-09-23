"""LTI 1.3 launch validation and just-in-time user provisioning.

Until now nothing tested ``app/lti13.py``. These cover the two identity decisions a
launch makes:

1. Is this id_token really from the platform we registered? The audience and the
   deployment must match OUR registration. Before, a platform with no client id
   configured compared the token's ``aud`` against itself, and the deployment claim was
   never checked, so any install of the same client on that issuer could launch.
2. Which TrueNorth user is it? The email claim is asserted by the platform, and
   ``users.email`` is globally unique, so matching it across tenants handed a
   registered platform any user in any tenant — an admin included.

Launches are signed with the tool's own keypair (``lti13.get_tool_key``) standing in for
the platform's, and its JWKS is served where the platform's would be.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import respx
from app import lti13
from app.models import ExternalPlatform, IntegrationAuthType, LTINonce, User, UserRole
from app.routers.integrations import _jit_user
from fastapi import HTTPException
from httpx import Response
from jose import jwt

TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
ISSUER = "http://moodle.test"
JWKS_URL = "http://moodle.test/mod/lti/certs.php"


def _platform(db, *, client_id="client-1", deployment_id="dep-1", tenant=TENANT_A) -> ExternalPlatform:
    p = ExternalPlatform(
        id=uuid.uuid4(),
        name="Moodle",
        slug=f"moodle-{uuid.uuid4().hex[:6]}",
        platform_type="moodle",
        base_url=ISSUER,
        auth_type=IntegrationAuthType.lti13,
        tenant_id=tenant,
        lti_issuer=ISSUER,
        lti_client_id=client_id,
        lti_deployment_id=deployment_id,
        lti_jwks_url=JWKS_URL,
    )
    db.add(p)
    db.flush()
    return p


def _launch(db, platform: ExternalPlatform, *, aud="client-1", deployment="dep-1") -> tuple[str, str]:
    """A signed id_token plus the state it was issued under."""
    nonce, state = uuid.uuid4().hex, uuid.uuid4().hex
    db.add(
        LTINonce(
            nonce=nonce,
            state=state,
            platform_id=platform.id,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    db.flush()
    key = lti13.get_tool_key(db)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": aud,
            "sub": "moodle-user-7",
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            lti13.CLAIM_DEPLOYMENT: deployment,
            lti13.CLAIM_MESSAGE_TYPE: "LtiResourceLinkRequest",
        },
        key.private_key_pem,
        algorithm="RS256",
        headers={"kid": key.kid},
    )
    return token, state


@pytest.fixture
def platform_keys(db_session):
    """Serve the signing key's JWKS at the platform's JWKS URL."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JWKS_URL).mock(return_value=Response(200, json=lti13.jwks(db_session)))
        yield mock


@pytest.mark.asyncio
class TestValidateLaunch:
    async def test_a_matching_launch_is_accepted(self, db_session, platform_keys):
        platform = _platform(db_session)
        token, state = _launch(db_session, platform)
        found, claims = await lti13.validate_launch(db_session, token, state)
        assert found.id == platform.id
        assert claims["sub"] == "moodle-user-7"

    async def test_a_different_deployment_is_refused(self, db_session, platform_keys):
        platform = _platform(db_session)
        token, state = _launch(db_session, platform, deployment="some-other-install")
        with pytest.raises(ValueError, match="deployment"):
            await lti13.validate_launch(db_session, token, state)

    async def test_a_platform_without_a_registered_deployment_cannot_launch(self, db_session, platform_keys):
        platform = _platform(db_session, deployment_id=None)
        token, state = _launch(db_session, platform)
        with pytest.raises(ValueError, match="deployment id"):
            await lti13.validate_launch(db_session, token, state)

    async def test_a_platform_without_a_registered_client_cannot_launch(self, db_session, platform_keys):
        """Without our own client id, the audience would be checked against itself."""
        platform = _platform(db_session, client_id=None)
        token, state = _launch(db_session, platform)
        with pytest.raises(ValueError, match="client id"):
            await lti13.validate_launch(db_session, token, state)

    async def test_a_token_for_another_client_is_refused(self, db_session, platform_keys):
        platform = _platform(db_session)
        token, state = _launch(db_session, platform, aud="client-2")
        with pytest.raises(Exception):  # noqa: B017 — jose raises JWTClaimsError
            await lti13.validate_launch(db_session, token, state)

    async def test_a_replayed_state_is_refused(self, db_session, platform_keys):
        platform = _platform(db_session)
        token, state = _launch(db_session, platform)
        await lti13.validate_launch(db_session, token, state)
        with pytest.raises(ValueError, match="replayed"):
            await lti13.validate_launch(db_session, token, state)


def _user(db, *, email: str, tenant: uuid.UUID, role=UserRole.student) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=email,
        display_name="someone",
        role=role,
        tenant_id=tenant,
    )
    db.add(u)
    db.flush()
    return u


class TestJitUser:
    def test_an_email_belonging_to_another_tenant_is_refused(self, db_session):
        """The platform names an admin's address in tenant B; it must not become them."""
        _user(db_session, email="boss@example.test", tenant=TENANT_B, role=UserRole.admin)
        platform = _platform(db_session, tenant=TENANT_A)
        with pytest.raises(HTTPException) as exc:
            _jit_user(db_session, platform, {"sub": "7", "email": "boss@example.test"})
        assert exc.value.status_code == 403

    def test_an_existing_user_in_the_platforms_tenant_is_matched(self, db_session):
        mine = _user(db_session, email="trainee@example.test", tenant=TENANT_A)
        platform = _platform(db_session, tenant=TENANT_A)
        assert _jit_user(db_session, platform, {"sub": "7", "email": "trainee@example.test"}).id == mine.id

    def test_a_new_learner_is_created_in_the_platforms_tenant_as_a_student(self, db_session):
        platform = _platform(db_session, tenant=TENANT_A)
        user = _jit_user(db_session, platform, {"sub": "new-1", "email": "new@example.test"})
        assert str(user.tenant_id) == str(TENANT_A)
        assert user.role == UserRole.student
        assert user.source == "lti"
