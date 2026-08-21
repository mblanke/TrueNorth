"""Bridge to the scenario-engine package that ships alongside this API.

The scenario engine owns the canonical scenario/template JSON Schemas and the
inject registry, but it is a sibling directory, not an installed dependency of
the API. This module is the single place that knows how to find it, so the
schema and the injector catalogue have one source of truth instead of vendored
copies drifting apart.

Resolution order: the ``SCENARIO_ENGINE_DIR`` environment variable, then the
in-repo default (``<repo>/scenario-engine``). Containers must either COPY the
directory or set the variable; a missing engine surfaces as a clean 503 from
the endpoints that need it, never an ImportError at startup.
"""

from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException


def engine_dir() -> Path:
    """Where the scenario-engine lives, by env var or by searching upward.

    The upward walk is bounded by however many parents actually exist: in the
    container the app sits at /app/app, which has three ancestors, so indexing a
    fixed depth raised IndexError instead of the clean 503 this module promises.
    """
    env = os.getenv("SCENARIO_ENGINE_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "scenario-engine"
        if candidate.is_dir():
            return candidate
    # Nothing found — return the in-repo location so the error names a real path.
    return here.parents[min(3, len(here.parents) - 1)] / "scenario-engine"


def _unavailable(what: str) -> HTTPException:
    return HTTPException(
        503,
        f"{what} unavailable: scenario-engine not found at '{engine_dir()}'. "
        "Set SCENARIO_ENGINE_DIR or deploy the scenario-engine directory alongside the API.",
    )


@lru_cache(maxsize=4)
def load_schema(name: str) -> dict[str, Any]:
    """Load a JSON Schema shipped by the engine (e.g. 'scenario', 'template')."""
    path = engine_dir() / "schemas" / f"{name}.schema.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _unavailable(f"{name} schema") from exc


def validate_yaml(schema_name: str, yaml_text: str) -> dict[str, Any]:
    """Validate a YAML document against an engine schema.

    Always returns a result (never raises for bad input): ``normalized`` is the
    parsed mapping whenever the YAML parses — even when schema-invalid — so an
    editing UI can round-trip a document while showing its errors.
    """
    import yaml as pyyaml
    from jsonschema import Draft7Validator

    schema = load_schema(schema_name)
    try:
        parsed = pyyaml.safe_load(yaml_text)
    except pyyaml.YAMLError as exc:
        return {"valid": False, "errors": [{"path": "", "message": f"Invalid YAML: {exc}"}], "normalized": None}
    if not isinstance(parsed, dict):
        return {
            "valid": False,
            "errors": [{"path": "", "message": "Document must be a YAML mapping"}],
            "normalized": None,
        }

    errors = [
        {"path": ".".join(str(p) for p in err.absolute_path), "message": err.message}
        for err in Draft7Validator(schema).iter_errors(parsed)
    ]
    errors.sort(key=lambda e: e["path"])
    return {"valid": not errors, "errors": errors, "normalized": parsed}


@lru_cache(maxsize=1)
def injector_catalogue() -> list[dict[str, Any]]:
    """The registered injectors as plain metadata, for authoring UIs.

    Imported lazily: the registry auto-discovers its modules on import, and the
    API must not pay that (or fail on it) at startup.
    """
    root = str(engine_dir())
    if not (engine_dir() / "scenario_engine").is_dir():
        raise _unavailable("inject catalogue")
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from scenario_engine import injectors  # noqa: PLC0415 — deliberate lazy import
    except ImportError as exc:
        raise _unavailable("inject catalogue") from exc

    catalogue = []
    for action in injectors.list_injectors():
        inst = injectors.get_injector(action)
        if inst is None:
            continue
        technique_map = getattr(inst, "_TECHNIQUE_MAP", None) or {}
        catalogue.append({
            "name": action,
            "description": getattr(inst, "description", "") or "",
            "required_params": list(getattr(inst, "required_params", []) or []),
            "mitre_techniques": sorted(set(technique_map.values())),
        })
    return catalogue
