"""Which objective validators can be judged, by whom, which are refused, and a scenario check.

The scoring engine (scoring/), the API's scenario validation and create/update
(control-plane/api/app/engine_bridge.py, routers/scenarios.py) and the Scenario Studio's
validator list all agree with this module (tests/scenario_engine/
test_scenario_objective_validation.py). It needs only the standard library and
``variables``, so the API imports it without the engine's runtime deps.

Who judges what. Every objective has exactly one judge, and nothing passes on content:

- ``opensearch_query`` (default): a **Student's submitted detection** (ADR 0005,
  app/detections/credit.py). The scoring engine never credits it: an inject's own
  synthetic records match the answer key, so the engine would award full points with no
  Student action. In the engine it is unscored.
- ``opensearch_query`` with ``require_observed: true``: the **scoring engine**, from
  observed telemetry only (below). Achieved when enough observed events match; otherwise
  unscored, never failed. Student submissions are not credited against it.
- ``user_disabled``: the scoring engine, from observed Windows Security 4725 for the
  account inside the exercise window, with no later 4722 re-enable. Unscored when there is
  no exercise window or no observed Windows Security telemetry in it (the evidence source
  is missing; a real disable must never score as a fail).
- ``deliverable_check``: a Student's submission record, handed to the engine by the
  platform (``submission``). Content cannot name a file. The platform has no deliverable
  submission yet, so in practice it is unscored until an instructor acknowledges it.
- ``manual_ack``: an explicit instructor action (``POST /exercises/{id}/objectives/{ref}/
  ack``). Content cannot pre-approve it. Unscored until acknowledged.
- ``greyspace_breadcrumb``: the token the Student submitted (ADR 0007).

Validators that used to be stubs are **refused** with the reason below: a scenario naming
one fails validation and cannot be stored; at scoring time it is unscored.

Evidence classes. Inject telemetry carries ``tn_ground_truth.evidence_class``:
``synthetic`` (a simulated inject built the record; nothing ran on a host) or ``live``.
Telemetry without that label is ``observed``: a range host or sensor produced it. The label
is stored but deliberately not indexed (telemetry/pipelines/bootstrap.py): indexing it would
let anyone with telemetry search pick the inject's events out by field or free text. So
observed-only scoring classifies each matching event after the query, over every match
(event_stores ``search(..., offset=)`` pages), and is unscored past ``MAX_SCAN`` matches.
"""

from __future__ import annotations

import re
from typing import Any

from .variables import render, unresolved

# Canonical content spelling -> who judges it and on what.
SCENARIO_VALIDATORS: dict[str, str] = {
    "opensearch_query": "a Student's submitted detection; with require_observed, observed telemetry",
    "manual_ack": "an instructor's acknowledgement (explicit instructor action)",
    "deliverable_check": "a Student's deliverable submission record",
    "user_disabled": "observed Windows Security 4725 for the account in the exercise window",
    "greyspace_breadcrumb": "the Greyspace breadcrumb token the Student submitted",
}

# What an objective with no validator means: the instructor decides (never an auto-pass).
DEFAULT_VALIDATOR = "manual_ack"

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

# Params only the platform may supply; content naming them is refused (and the runner drops them).
PLATFORM_PARAMS: dict[str, str] = {
    "approved": "a manual objective is approved by an instructor during the exercise, not by content",
    "submission": "a deliverable is the Student's submission record, supplied by the platform",
    "storage_path": "a deliverable is the Student's submission record, not a file content names",
    "window_start": "the exercise window comes from the exercise, not from content",
    "window_end": "the exercise window comes from the exercise, not from content",
}

# Account names a user_disabled objective may name: no query syntax can ride in.
ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._$-]{0,63}$")

# Most matches classified for observed-only scoring; past it, unscored.
MAX_SCAN = 10_000

# A synthetic record says so under the inject label (scenario_engine.injectors).
GROUND_TRUTH_FIELD = "tn_ground_truth"


def canonical(name: Any) -> str:
    """The one spelling: ``validate.opensearch.query`` -> ``opensearch_query``.

    An empty name is ``DEFAULT_VALIDATOR``. The API's ``canonical_validator``
    (app/detections/names.py) must agree on every input (drift test).
    """
    raw = str(name or "").strip().lower()
    if not raw:
        return DEFAULT_VALIDATOR
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

    Covers what the JSON Schema cannot say: why a validator is refused, params only the
    platform may set (a self-approving manual objective, a deliverable file named by
    content), a ``user_disabled`` with no usable account name, a ``greyspace_breadcrumb``
    with no crumb, and phase-level ``validators:`` blocks, which nothing evaluates.
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
        for key, why in PLATFORM_PARAMS.items():
            if key in params:
                errors.append(_err(f"{path}.params.{key}", f"content cannot set {key}: {why}"))
        if name == "user_disabled":
            user = render(str(params.get("username") or ""), variables)
            if not user or unresolved(user) or not ACCOUNT_NAME.match(user):
                errors.append(
                    _err(f"{path}.params.username", "user_disabled needs params.username: a plain account name")
                )
        if name == "greyspace_breadcrumb" and not str(params.get("crumb_id") or ""):
            errors.append(_err(f"{path}.params.crumb_id", "greyspace_breadcrumb needs params.crumb_id"))
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
