# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""GET /injectors (app/routers/injectors.py).

Read-only introspection of the scenario-engine injector registry: every registered
injector comes back with its authoring metadata, and the entries agree with the registry
itself (the UIs render from this, so a missing field is a blank form).
"""

from __future__ import annotations


def test_catalogue_lists_the_registered_injectors(client):
    from scenario_engine import injectors

    body = client.get("/injectors")
    assert body.status_code == 200, body.text
    catalogue = body.json()
    assert catalogue, "the engine ships injectors; an empty catalogue means discovery broke"

    registered = set(injectors.list_injectors())
    assert {entry["name"] for entry in catalogue} == registered

    for entry in catalogue:
        assert set(entry) == {
            "name",
            "description",
            "required_params",
            "mitre_techniques",
            "touches_range_hosts",
            "execution_mode",
        }
        inst = injectors.get_injector(entry["name"])
        assert entry["required_params"] == list(getattr(inst, "required_params", []) or [])
        assert entry["touches_range_hosts"] == bool(getattr(inst, "touches_range_hosts", True))


def test_contract_flags_of_a_host_touching_injector(client):
    """An injector that runs actions on range hosts must say so (the mock backend records
    those as skipped, docs/scenario-inject-execution.md), and default flags are honest."""
    from scenario_engine import injectors

    catalogue = client.get("/injectors").json()
    flagged = {e["name"]: e for e in catalogue}
    host_touching = [
        name
        for name in injectors.list_injectors()
        if getattr(injectors.get_injector(name), "touches_range_hosts", True)
    ]
    assert host_touching, "expected the registry to contain host-touching injectors"
    for name in host_touching:
        assert flagged[name]["touches_range_hosts"] is True


def test_mitre_techniques_are_sorted_and_unique(client):
    catalogue = client.get("/injectors").json()
    for entry in catalogue:
        techniques = entry["mitre_techniques"]
        assert techniques == sorted(set(techniques))
