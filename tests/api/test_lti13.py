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

import jwt
import pytest
import respx
from app import lti13
from app.models import ExternalPlatform, IntegrationAuthType, LTINonce, User, UserRole
from app.routers.integrations import _jit_user
from fastapi import HTTPException
from httpx import Response

TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
ISSUER = "http://moodle.test"
JWKS_URL = "http://moodle.test/mod/lti/certs.php"


def _platform(
    db, *, client_id="client-1", deployment_id="dep-1", tenant=TENANT_A, base_url=ISSUER
) -> ExternalPlatform:
    p = ExternalPlatform(
        id=uuid.uuid4(),
        name="Moodle",
        slug=f"moodle-{uuid.uuid4().hex[:6]}",
        platform_type="moodle",
        base_url=base_url,
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


def _launch(db, platform: ExternalPlatform, *, aud="client-1", deployment="dep-1", extra=None) -> tuple[str, str]:
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
            **(extra or {}),
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
        """An unknown client id no longer falls back to another registration."""
        platform = _platform(db_session)
        token, state = _launch(db_session, platform, aud="client-2")
        with pytest.raises(ValueError, match="No registered LTI platform"):
            await lti13.validate_launch(db_session, token, state)

    async def test_the_audience_is_verified_against_our_registration(self, db_session, platform_keys):
        """azp selects our registration, but the signed aud is someone else's."""
        platform = _platform(db_session)
        token, state = _launch(db_session, platform, aud="client-2", extra={"azp": "client-1"})
        with pytest.raises(jwt.InvalidAudienceError):
            await lti13.validate_launch(db_session, token, state)

    async def test_a_token_naming_another_tenants_client_gets_that_registration_only(
        self, db_session, platform_keys
    ):
        """Two tenants register one Moodle; each launch resolves to its own client's row."""
        _platform(db_session, client_id="client-1", tenant=TENANT_A)
        theirs = _platform(db_session, client_id="client-2", tenant=TENANT_B)
        token, state = _launch(db_session, theirs, aud="client-2")
        found, _ = await lti13.validate_launch(db_session, token, state)
        assert found.id == theirs.id

    async def test_a_replayed_state_is_refused(self, db_session, platform_keys):
        platform = _platform(db_session)
        token, state = _launch(db_session, platform)
        await lti13.validate_launch(db_session, token, state)
        with pytest.raises(ValueError, match="replayed"):
            await lti13.validate_launch(db_session, token, state)

    async def test_keys_are_fetched_at_the_internal_address_with_the_public_host(self, db_session):
        """Moodle redirects any request whose Host is not its wwwroot (found on 5.2.3)."""
        platform = _platform(db_session, base_url="http://moodle:8080")
        token, state = _launch(db_session, platform)
        with respx.mock(assert_all_called=True) as mock:
            route = mock.get("http://moodle:8080/mod/lti/certs.php").mock(
                return_value=Response(200, json=lti13.jwks(db_session))
            )
            await lti13.validate_launch(db_session, token, state)
        assert route.calls.last.request.headers["host"] == "moodle.test"


class TestPlatformRoute:
    """Farm nodes are registered by public URL but reached at their internal address."""

    def _p(self, base_url, issuer="https://moodle.example"):
        return ExternalPlatform(base_url=base_url, lti_issuer=issuer)

    def test_a_public_url_is_sent_to_the_internal_origin_keeping_the_public_host(self):
        url, headers = lti13.platform_route(
            self._p("http://moodle:8080"),
            "https://moodle.example/mod/lti/services.php/2/lineitems/2/lineitem?type_id=1",
        )
        assert url == "http://moodle:8080/mod/lti/services.php/2/lineitems/2/lineitem?type_id=1"
        assert headers == {"Host": "moodle.example"}

    def test_same_origin_platforms_are_untouched(self):
        url = "https://moodle.example/mod/lti/token.php"
        assert lti13.platform_route(self._p("https://moodle.example"), url) == (url, {})

    def test_urls_off_the_issuer_origin_are_never_rerouted(self):
        """A lineitem URL comes from the launch; it must not redirect our traffic elsewhere."""
        url = "https://attacker.example/steal"
        assert lti13.platform_route(self._p("http://moodle:8080"), url) == (url, {})

    def test_a_different_scheme_on_the_issuer_host_is_not_rerouted(self):
        url = "http://moodle.example/mod/lti/token.php"
        assert lti13.platform_route(self._p("http://moodle:8080"), url) == (url, {})

    def test_no_issuer_means_no_rerouting(self):
        url = "https://moodle.example/mod/lti/token.php"
        assert lti13.platform_route(self._p("http://moodle:8080", issuer=None), url) == (url, {})


def test_farm_nodes_fetch_the_public_half_of_the_tool_key(client, db_session):
    """Farm nodes fetch this at start (04-truenorth-bootstrap.sh); never the private key."""
    resp = client.get("/lti/public-key.pem")
    assert resp.status_code == 200
    assert resp.text.strip() == lti13.get_tool_key(db_session).public_key_pem.strip()
    assert "BEGIN PUBLIC KEY" in resp.text and "PRIVATE" not in resp.text


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


class TestLoginInitiation:
    """Several tenants may register the same Moodle (one issuer) as different clients.

    An unknown client id used to fall back to the issuer's first registration, so login
    initiation minted state for, and redirected under, another tenant's client."""

    def _registered(self, db, **kw) -> ExternalPlatform:
        p = _platform(db, **kw)
        p.lti_auth_login_url = f"{ISSUER}/mod/lti/auth.php"
        db.flush()
        return p

    def _login(self, db, client_id):
        return lti13.build_login_redirect(
            db,
            iss=ISSUER,
            login_hint="u",
            target_link_uri="http://tool/launch",
            client_id=client_id,
            lti_message_hint=None,
        )

    def test_a_known_client_gets_its_own_registration(self, db_session):
        self._registered(db_session, client_id="client-1")
        theirs = self._registered(db_session, client_id="client-2", tenant=TENANT_B)
        assert "client_id=client-2" in self._login(db_session, "client-2")
        minted = db_session.query(LTINonce).order_by(LTINonce.expires_at.desc()).first()
        assert minted.platform_id == theirs.id

    def test_an_unknown_client_does_not_borrow_another_registration(self, db_session):
        self._registered(db_session, client_id="client-1")
        with pytest.raises(ValueError, match="Unknown LTI platform"):
            self._login(db_session, "client-3")

    def test_no_client_id_is_ambiguous_when_the_issuer_has_several(self, db_session):
        self._registered(db_session, client_id="client-1")
        self._registered(db_session, client_id="client-2", tenant=TENANT_B)
        with pytest.raises(ValueError, match="Unknown LTI platform"):
            self._login(db_session, None)

    def test_no_client_id_resolves_a_sole_registration(self, db_session):
        self._registered(db_session, client_id="client-1")
        assert "client_id=client-1" in self._login(db_session, None)
