"""Which objective validators can be judged, which are refused, and a scenario check.

The scoring engine (scoring/validators.py) and the API's ``POST /scenarios/validate``
(control-plane/api/app/engine_bridge.py) both read this module, so a validator cannot be
accepted by one and refused by the other. It has no dependencies beyond the standard
library and ``variables``: the API imports it without the engine's runtime deps.

A validator is either judged from evidence the platform actually has (telemetry in the
range's event index, a submitted deliverable, a breadcrumb answer), or an explicit
instructor action (``manual_ack``: the instructor acknowledges the objective through
``POST /exercises/{id}/objectives/{ref}/ack``). Nothing passes on content alone.

Validators that used to be stubs are **refused** with the reason below. A scenario that
names one fails validation; at scoring time it is unscored, never passed or failed.

Evidence classes. Inject telemetry carries ``tn_ground_truth.evidence_class``:
``synthetic`` (a simulated inject built the record; nothing ran on a host) or ``live``.
Telemetry without that label is ``observed``: a range host or sensor produced it. A
validator that asserts something really happened counts only observed evidence:
``user_disabled`` always; ``opensearch_query`` when ``require_observed: true``. Detection
objectives default to ``require_observed: false``, because detecting a simulated inject's
records is what they score.
"""

from __future__ import annotations

import re
from typing import Any

from .variables import render, unresolved

# Content spelling -> what the validator judges.
SCENARIO_VALIDATORS: dict[str, str] = {
    "opensearch_query": "enough events match a query in the range's event index",
    "deliverable_check": "a deliverable was submitted",
    "manual_ack": "an instructor acknowledged the objective (explicit instructor action)",
    "user_disabled": "Windows Security event 4725 (account disabled) observed for the named account",
}

_USE_MANUAL = "Use validator: manual_ack; an instructor verifies it and acknowledges the objective."

# Never judged automatically: the platform has no evidence that would prove them.
REFUSED_VALIDATORS: dict[str, str] = {
    "firewall_rule": (
        "no telemetry records a firewall rule change: range hosts do not audit rule changes "
        "(events 4946-4948) and the range router ships no configuration-change log. " + _USE_MANUAL
    ),
    "process_killed": (
        "Sysmon event 5 says a process ended, not that a defender ended it, and the shipped "
        "Sysmon configuration logs terminations only for some paths. " + _USE_MANUAL
    ),
    "network_isolated": (
        "isolation is the absence of traffic, and no event proves a host is cut off. " + _USE_MANUAL
    ),
}

# Account names a user_disabled objective may name: no query syntax can ride in.
ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._$-]{0,63}$")

# A synthetic record says so under the inject label (scenario_engine.injectors).
GROUND_TRUTH_FIELD = "tn_ground_truth"


def canonical(name: Any) -> str:
    """``validate.opensearch.query`` -> ``opensearch_query``; empty stays empty."""
    raw = str(name or "").strip().lower()
    for prefix in ("validate.", "validate_"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.replace(".", "_")


def evidence_class(source: dict[str, Any]) -> str:
    """``synthetic``, ``live`` (a live inject's record) or ``observed`` for one event."""
    labels = source.get(GROUND_TRUTH_FIELD)
    if isinstance(labels, dict):
        declared = labels.get("evidence_class")
        if declared in ("synthetic", "live"):
            return declared
        return "synthetic" if labels.get("simulated", True) is not False else "live"
    # Inject telemetry written before 2026-10-08 carried flat labels.
    if source.get("truenorth_simulated") is True or source.get("event.module") == "truenorth.inject":
        return "synthetic"
    module = source.get("event")
    if isinstance(module, dict) and module.get("module") == "truenorth.inject":
        return "synthetic"
    return "observed"


def _err(path: str, message: str) -> dict[str, str]:
    return {"path": path, "message": message}


def objective_errors(scenario: dict[str, Any]) -> list[dict[str, str]]:
    """Errors (``{path, message}``) for objectives that could never be judged honestly.

    Covers what the JSON Schema cannot say: why a validator is refused, an objective that
    pre-approves itself, a ``user_disabled`` with no usable account name, and phase-level
    ``validators:`` blocks, which nothing evaluates.
    """
    errors: list[dict[str, str]] = []
    variables = scenario.get("variables") if isinstance(scenario.get("variables"), dict) else {}

    for i, obj in enumerate(scenario.get("objectives") or []):
        if not isinstance(obj, dict):
            continue
        path = f"objectives.{i}"
        name = canonical(obj.get("validator"))
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        if name in REFUSED_VALIDATORS:
            errors.append(_err(f"{path}.validator", f"validator '{name}' is refused: {REFUSED_VALIDATORS[name]}"))
        elif name not in SCENARIO_VALIDATORS:
            errors.append(
                _err(f"{path}.validator", f"unknown validator '{name}'; use one of {', '.join(SCENARIO_VALIDATORS)}")
            )
        if name == "manual_ack" and "approved" in params:
            errors.append(
                _err(
                    f"{path}.params.approved",
                    "a manual_ack objective cannot approve itself; an instructor acknowledges it during the exercise",
                )
            )
        if name == "user_disabled":
            user = render(str(params.get("username") or ""), variables)
            if not user or unresolved(user) or not ACCOUNT_NAME.match(user):
                errors.append(
                    _err(f"{path}.params.username", "user_disabled needs params.username: a plain account name")
                )
        if "require_observed" in params and not isinstance(params["require_observed"], bool):
            errors.append(_err(f"{path}.params.require_observed", "require_observed must be true or false"))

    for i, phase in enumerate(scenario.get("timeline") or []):
        if not isinstance(phase, dict) or "validators" not in phase:
            continue
        entries = phase["validators"] if isinstance(phase["validators"], list) else [phase["validators"]]
        for j, entry in enumerate(entries):
            name = canonical(entry.get("type") if isinstance(entry, dict) else entry)
            why = REFUSED_VALIDATORS.get(name) or (
                "" if name in SCENARIO_VALIDATORS else f"'{name}' is not a validator. "
            )
            errors.append(
                _err(
                    f"timeline.{i}.validators.{j}",
                    f"phase-level validators are never evaluated; {why}Score it as an entry under objectives:.",
                )
            )
    return errors
