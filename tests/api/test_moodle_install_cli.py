"""The installer's Moodle hooks (app.moodle_backends.install_cli, install/roles/tn_moodle).

The installer registers the Moodle it runs as platform ``moodle-<node>`` in the tenant,
idempotently (a second run reports "unchanged", which is how the role reports no change),
never takes an issuer another tenant registered, and proves the wiring with a signed
describe_course that must come back "does not exist".
"""

from __future__ import annotations

import uuid

import pytest
from app.models import ExternalPlatform, IntegrationAuthType, Tenant
from app.moodle_backends import MoodleError, install_cli
from app.moodle_backends.fake import FakeMoodle, calls, reset

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")  # "default", seeded by conftest
SITE = "https://tn.example:8443"
REG = {
    "lti_type_id": 1,
    "lti_issuer": SITE,
    "lti_client_id": "abc123",
    "lti_deployment_id": "1",
    "lti_auth_login_url": f"{SITE}/mod/lti/auth.php",
    "lti_token_url": f"{SITE}/mod/lti/token.php",
    "lti_jwks_url": f"{SITE}/mod/lti/certs.php",
}


def test_tenant_id_by_slug(db_session):
    assert install_cli.tenant_id(db_session, "default") == TENANT
    with pytest.raises(install_cli.InstallError) as e:
        install_cli.tenant_id(db_session, "nope")
    assert e.value.code == 3


def test_register_creates_then_converges(db_session):
    first = install_cli.register(db_session, "default", "default", "http://moodle-default:8080", REG)
    assert first["action"] == "created" and first["slug"] == "moodle-default"
    p = db_session.get(ExternalPlatform, uuid.UUID(first["platform_id"]))
    assert p.tenant_id == TENANT and p.platform_type == "moodle" and p.is_active
    assert p.auth_type == IntegrationAuthType.lti13
    assert p.base_url == "http://moodle-default:8080" and p.lti_issuer == SITE
    assert p.lti_auth_login_url == f"{SITE}/mod/lti/auth.php"

    again = install_cli.register(db_session, "default", "default", "http://moodle-default:8080", REG)
    assert again == {**first, "action": "unchanged"}

    # A re-created Moodle gets a new client id; an operator deactivated it meanwhile.
    p.is_active = False
    db_session.flush()
    moved = install_cli.register(
        db_session, "default", "default", "http://moodle-default:8080", {**REG, "lti_client_id": "new"}
    )
    assert moved["action"] == "updated" and moved["platform_id"] == first["platform_id"]
    assert p.lti_client_id == "new" and p.is_active


def test_register_refuses_an_issuer_another_tenant_holds(db_session):
    other = Tenant(id=uuid.uuid4(), name="Unit B", slug=f"unit-b-{uuid.uuid4().hex[:6]}")
    db_session.add(other)
    db_session.flush()
    db_session.add(
        ExternalPlatform(
            name="Theirs",
            slug="moodle-b",
            platform_type="moodle",
            base_url="http://moodle-b:8080",
            auth_type=IntegrationAuthType.lti13,
            tenant_id=other.id,
            lti_issuer=SITE + "/",
        )
    )
    db_session.flush()
    with pytest.raises(install_cli.InstallError) as e:
        install_cli.register(db_session, "default", "default", "http://moodle-default:8080", REG)
    assert e.value.code == 4


@pytest.mark.parametrize("bad", [{}, {**REG, "lti_client_id": ""}, {k: v for k, v in REG.items() if k != "lti_jwks_url"}])
def test_register_refuses_an_incomplete_registration(db_session, bad):
    with pytest.raises(install_cli.InstallError) as e:
        install_cli.register(db_session, "default", "default", "http://moodle-default:8080", bad)
    assert e.value.code == 2


def test_check_describes_a_course_that_never_exists(db_session):
    reset()
    install_cli.register(db_session, "default", "default", "http://moodle-default:8080", REG)
    out = install_cli.check(db_session, "default", "default", backend=FakeMoodle())
    assert out["ok"] and out["issuer"] == SITE
    assert calls() == [("describe_course", install_cli.PROBE_IDNUMBER)]


def test_check_reports_a_refusal_as_exit_5(db_session):
    class Refusing(FakeMoodle):
        def describe_course(self, platform, idnumber):
            raise MoodleError("Moodle refused describe_course: tenant")

    install_cli.register(db_session, "default", "default", "http://moodle-default:8080", REG)
    with pytest.raises(install_cli.InstallError) as e:
        install_cli.check(db_session, "default", "default", backend=Refusing())
    assert e.value.code == 5 and "tenant" in str(e.value)


def test_check_without_a_platform_is_exit_3(db_session):
    with pytest.raises(install_cli.InstallError) as e:
        install_cli.check(db_session, "default", "other-node", backend=FakeMoodle())
    assert e.value.code == 3


def test_the_probe_is_an_idnumber_the_plugin_accepts():
    """course_sync.php only touches tn-stage:<uuid> or <uuid> idnumbers."""
    import re

    assert re.fullmatch(
        r"(tn-stage:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", install_cli.PROBE_IDNUMBER
    )
