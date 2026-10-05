"""Shipped scenario and range-template YAML vs the engine's JSON Schemas (draft-07).

``scenario-engine/schemas/*.schema.json`` is the contract the API (engine_bridge.validate_yaml)
and the runner (runner/run.py) enforce. Every document we ship must satisfy it. Files that
do not are listed in ``_KNOWN_NONCONFORMANT`` as strict xfails: fixing the file (or
deliberately changing the schema) turns the xfail into an XPASS, which fails the run, so
the list cannot go stale.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft7Validator

_REPO = Path(__file__).resolve().parents[2]
_SCHEMAS = _REPO / "scenario-engine" / "schemas"

# schema name -> globs (relative to repo root) of documents that schema governs
_CORPUS: dict[str, tuple[str, ...]] = {
    "scenario": ("content/scenarios/**/scenario.yaml", "scenario-engine/examples/*.yaml"),
    "template": ("content/ranges/**/template.yaml",),
}

# Files known not to conform, as strict xfails with the reason. Empty since 2026-10-03,
# when timeline[].t was widened to "H:MM" or "HH:MM": the examples, the worker and
# routers/exercises.py (f"{i}:00") all write one-digit hours.
_KNOWN_NONCONFORMANT: dict[str, str] = {}


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def _corpus(name: str) -> list[Path]:
    return sorted({p for pattern in _CORPUS[name] for p in _REPO.glob(pattern)})


def _params() -> list:
    params = []
    for name in _CORPUS:
        for path in _corpus(name):
            rel = path.relative_to(_REPO).as_posix()
            marks = []
            if rel in _KNOWN_NONCONFORMANT:
                marks.append(pytest.mark.xfail(strict=True, reason=_KNOWN_NONCONFORMANT[rel]))
            params.append(pytest.param(name, path, id=f"{name}:{rel}", marks=marks))
    return params


@pytest.mark.parametrize("name", sorted(_CORPUS))
def test_schema_is_valid_draft7(name):
    Draft7Validator.check_schema(_load_schema(name))


@pytest.mark.parametrize("name", sorted(_CORPUS))
def test_corpus_is_not_empty(name):
    # An empty glob would make the conformance test below collect nothing and pass vacuously.
    assert _corpus(name), f"no documents found for the {name} schema under {_CORPUS[name]}"


def test_known_nonconformant_entries_exist():
    for rel in _KNOWN_NONCONFORMANT:
        assert (_REPO / rel).is_file(), f"{rel} is listed as known-nonconformant but does not exist"


@pytest.mark.parametrize(("name", "path"), _params())
def test_document_conforms_to_schema(name, path):
    with open(path, encoding="utf-8-sig") as fh:
        doc = yaml.safe_load(fh)
    errors = [
        f"{'.'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in Draft7Validator(_load_schema(name)).iter_errors(doc)
    ]
    assert not errors, f"{path.relative_to(_REPO)} violates {name}.schema.json:\n  " + "\n  ".join(errors)


@pytest.mark.parametrize("name", sorted(_CORPUS))
def test_api_validates_against_the_same_schema(name):
    # engine_bridge is the API's view of the engine; it must read these files, not a copy.
    from app.engine_bridge import load_schema

    assert load_schema(name) == _load_schema(name)
