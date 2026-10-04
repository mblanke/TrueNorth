"""Static checks on infra/keycloak/realm-truenorth.json for things Keycloak 24 rejects.

The authoritative check is scripts/check_keycloak_realm.sh (CI job keycloak-realm),
which imports the file into the pinned Keycloak image. These catch the shapes that
broke it before, without Docker, so DoD fails first.
"""

import json
from pathlib import Path

REALM = json.loads((Path(__file__).resolve().parents[2] / "infra/keycloak/realm-truenorth.json").read_text())

# AuthenticationExecutionModel.Requirement in Keycloak >= 8; OPTIONAL was removed.
REQUIREMENTS = {"REQUIRED", "CONDITIONAL", "ALTERNATIVE", "DISABLED"}


def test_flow_executions_are_flat_and_use_current_requirements():
    """A sub-flow is its own entry referenced by flowAlias, never nested inline."""
    for flow in REALM.get("authenticationFlows", []):
        for execution in flow.get("authenticationExecutions", []):
            assert "authenticationExecutions" not in execution, (
                f"flow {flow['alias']!r}: nested executions; define the sub-flow separately"
            )
            assert execution.get("requirement") in REQUIREMENTS, (
                f"flow {flow['alias']!r}: requirement {execution.get('requirement')!r} is not one of {sorted(REQUIREMENTS)}"
            )


def test_no_legacy_user_federation_block():
    """LDAP federation is a component, created by install/roles/tn_keycloak, not this file."""
    assert "userFederationProviders" not in REALM


def test_client_attributes_are_strings():
    for client in REALM.get("clients", []):
        for key, value in (client.get("attributes") or {}).items():
            assert isinstance(value, str), f"client {client['clientId']!r} attribute {key!r} must be a string"
