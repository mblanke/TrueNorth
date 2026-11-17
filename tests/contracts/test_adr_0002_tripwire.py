"""Tripwire tests for docs/adr/0002-interface-versioning.md.

One test per enforceable DECISION the ADR states, pinned at the code that enforces it;
each docstring quotes the ADR line it holds in place. A test here turning red means the
code drifted from the ADR (or the ADR changed — then update both in one commit).

Decisions deliberately absent: the RFC 8594 Deprecation/Sunset requirement applies only
once a version is retired (only v1 exists today), and "breaking changes go to a new
version" is process, not code. Read-only over source and the in-process app: no network,
no live services, nothing mocked.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[2]
OPENAPI = ROOT / "docs/interfaces/openapi.json"
API_APP = ROOT / "control-plane/api/app"
WEB = ROOT / "control-plane/web"

# Every operation method an OpenAPI path item may carry (RFC: fixed set, not free text).
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def _committed_openapi() -> dict:
    return json.loads(OPENAPI.read_text(encoding="utf-8"))


def _api_sources() -> list[Path]:
    return sorted(p for p in API_APP.rglob("*.py"))


def _hits(files: list[Path], pattern: re.Pattern[str]) -> list[str]:
    """file:line for every regex match, the way scripts/mosa_check.py reports locations."""
    out = []
    for f in files:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                out.append(f"{f.relative_to(ROOT)}:{i}")
    return out


def _load_script(name: str):
    """Import a repo script by path (scripts/ is not a package) and return its module.

    Some scripts tweak sys.path/os.environ at import; undo that so the suite's import
    resolution is unaffected by the order tests run in.
    """
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"_adr0002_tripwire_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    saved_path, saved_env = list(sys.path), dict(os.environ)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved_path
        for key in set(os.environ) - set(saved_env):
            del os.environ[key]
        os.environ.update(saved_env)
    return module


# -- Row "Control-plane HTTP API" + rule 1 -------------------------------------------


def test_committed_openapi_json_is_what_the_code_produces():
    """ADR 0002 rule 1: "The committed `openapi.json` must equal what the code produces."

    Renders the spec the way scripts/export_openapi.py does and compares byte for byte.
    """
    committed = OPENAPI.read_text(encoding="utf-8")
    assert _load_script("export_openapi.py").render() == committed, (
        "OPENAPI DRIFT: run `python scripts/export_openapi.py` and commit docs/interfaces/openapi.json"
    )


def test_openapi_contract_is_gated_in_dod_and_ci():
    """ADR 0002, table row 1, "Check" column: "`scripts/export_openapi.py --check` in
    `dod.sh` and CI" — same for the task contract's exporter (row 3)."""
    for gate in ("scripts/dod.sh", ".github/workflows/ci.yml"):
        text = (ROOT / gate).read_text(encoding="utf-8")
        assert "export_openapi.py --check" in text, f"{gate} no longer checks the API contract"
        assert "export_task_contracts.py --check" in text, f"{gate} no longer checks the task contract"


# -- Rule 2: unique, stable operationIds; one handler per method ---------------------


def test_every_operation_has_a_unique_operation_id():
    """ADR 0002 rule 2: "Every operation has a unique, stable `operationId`."

    Uniqueness is checkable here; stability is review discipline.
    """
    spec = _committed_openapi()
    ids: dict[str, str] = {}
    missing: list[str] = []
    for path, item in spec["paths"].items():
        for method in _HTTP_METHODS:
            op = item.get(method)
            if op is None:
                continue
            if "operationId" not in op:
                missing.append(f"{method.upper()} {path}")
                continue
            first = ids.setdefault(op["operationId"], f"{method.upper()} {path}")
            where = f"{method.upper()} {path}"
            assert first == where, f"duplicate operationId {op['operationId']!r}: {first} and {where}"
    assert not missing, f"operations without an operationId: {missing}"


def test_no_router_registers_two_methods_on_one_handler():
    """ADR 0002 rule 2: "Do not register one handler for several methods with
    `api_route(methods=[...])`; give each method its own decorator and `operation_id`."

    One route with two methods shares one operationId, and FastAPI derives its method
    suffix from a set, so the published contract changes from run to run
    (control-plane/api/app/routers/integrations.py:574).
    """
    offenders = _hits(_api_sources(), re.compile(r"\.(?:api_route|add_api_route)\("))
    assert not offenders, f"one-handler-for-several-methods registrations: {offenders}"


# -- Rule 3: versioned under /api/v1 -------------------------------------------------


def test_published_contract_declares_openapi_3_1_served_at_api_v1():
    """ADR 0002 rule 3: "The HTTP API is versioned under `/api/v1` (`app/versioning.py`)."
    Row 1 pins the format: "(OpenAPI 3.1, generated)"."""
    from app.versioning import CURRENT_VERSION, SERVER_PREFIX

    spec = _committed_openapi()
    assert spec["openapi"] == "3.1.0"
    assert spec["servers"] == [{"url": SERVER_PREFIX}], "published base path drifted"
    assert SERVER_PREFIX == f"/api/{CURRENT_VERSION.value}" == "/api/v1"


def test_version_prefix_middleware_maps_versions_and_rejects_unknown_ones():
    """ADR 0002 rule 3: "The HTTP API is versioned under `/api/v1` (`app/versioning.py`).""

    Behaviour of VersionPrefixMiddleware (control-plane/api/app/versioning.py): the
    versioned path resolves to the canonical route, the response carries
    ``X-API-Version``, and an unsupported version never reaches the app.
    """
    from app.versioning import SUPPORTED_VERSIONS, VersionPrefixMiddleware

    class _Inner:
        def __init__(self) -> None:
            self.paths: list[str] = []

        async def __call__(self, scope, receive, send):
            self.paths.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    def _exchange(path: str) -> tuple[_Inner, list[dict]]:
        inner = _Inner()
        messages: list[dict] = []

        async def receive():  # pragma: no cover - never called for a bare request
            raise AssertionError("no body expected")

        async def send(message: dict) -> None:
            messages.append(message)

        scope = {"type": "http", "path": path, "headers": []}
        asyncio.run(VersionPrefixMiddleware(inner)(scope, receive, send))
        return inner, messages

    inner, messages = _exchange("/v1/ranges")
    assert inner.paths == ["/ranges"]
    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = dict(start["headers"])
    assert headers.get(b"x-api-version") == b"v1", f"version stamp missing: {start['headers']}"

    inner, _ = _exchange("/api/v1/ranges")
    assert inner.paths == ["/ranges"], "the /api/v1 form must resolve too"
    inner, _ = _exchange("/ranges")
    assert inner.paths == ["/ranges"], "unversioned alias must keep working"

    inner, messages = _exchange("/v9/ranges")
    assert inner.paths == [], f"unsupported version reached the app: {inner.paths}"
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 404
    body = next(m for m in messages if m["type"] == "http.response.body")["body"]
    assert b"Unsupported API version" in body
    assert {"v1"} == SUPPORTED_VERSIONS, "a new version means a new contract artifact set"


def test_version_middleware_is_registered_outermost():
    """ADR 0002 rule 3 pins versioning to app/versioning.py; its own contract (control-
    plane/api/app/versioning.py:40) is "Register it last so it is outermost and every
    other middleware sees the canonical path". Starlette stores the last-added
    middleware first, so index 0 must be VersionPrefixMiddleware."""
    from app.main import app
    from app.versioning import VersionPrefixMiddleware

    assert app.user_middleware, "no middleware registered at all"
    assert app.user_middleware[0].cls is VersionPrefixMiddleware, (
        f"outermost middleware is {app.user_middleware[0].cls.__name__}, not VersionPrefixMiddleware"
    )


# -- Rule 4: clients are generated ---------------------------------------------------


def test_web_client_types_are_generated_from_the_published_contract():
    """ADR 0002 rule 4: "Clients are generated from the contract, not hand-written.""

    The Angular types come from docs/interfaces/openapi.json via openapi-typescript
    (control-plane/web/scripts/api-types.mjs), with a regenerate and a check script so
    drift is caught (ci.yml:297).
    """
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["gen:api"] == "node scripts/api-types.mjs --write"
    assert package["scripts"]["check:api"] == "node scripts/api-types.mjs"

    generator = (WEB / "scripts/api-types.mjs").read_text(encoding="utf-8")
    assert "../../docs/interfaces/openapi.json" in generator, "generator no longer reads the published contract"

    generated = (WEB / "src/app/core/api/schema.d.ts").read_text(encoding="utf-8")
    assert "This file was auto-generated by openapi-typescript" in generated.splitlines()[1], (
        "src/app/core/api/schema.d.ts no longer looks generated; hand edits are not clients"
    )


# -- Row "Scenario / template content" ----------------------------------------------


@pytest.mark.parametrize("name", ["scenario", "template"])
def test_scenario_schema_artifacts_are_valid_json_schema(name):
    """ADR 0002, table row 2: "Scenario / template content |
    `scenario-engine/schemas/*.schema.json` (JSON Schema)". The two published artifacts
    exist and are valid draft-07 schemas; document conformance is pinned by
    tests/scenario_engine/test_content_schema_conformance.py."""
    path = ROOT / "scenario-engine" / "schemas" / f"{name}.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    assert schema["type"] == "object"


# -- Row "API → worker tasks" + rule 5 ----------------------------------------------


def test_published_task_contract_artifacts_match_the_source():
    """ADR 0002, table row 3: "`control-plane/worker/worker/contracts.py` (source) →
    `api/app/task_contracts.py` copy + `docs/interfaces/worker-tasks.schema.json`".
    Renders both artifacts the way scripts/export_task_contracts.py does and compares
    byte for byte. The copy is "Generate[d] ... never hand-edit[ed]" (rule 5)."""
    mirror = ROOT / "control-plane/api/app/task_contracts.py"
    schema = ROOT / "docs/interfaces/worker-tasks.schema.json"
    outputs = _load_script("export_task_contracts.py").render()
    assert outputs[mirror] == mirror.read_text(encoding="utf-8"), "run scripts/export_task_contracts.py"
    assert outputs[schema] == schema.read_text(encoding="utf-8"), "run scripts/export_task_contracts.py"


def test_api_image_contains_no_worker_package():
    """ADR 0002 rule 5: "The API image does not contain the `worker` package."

    The api Dockerfile is an allowlist COPY (control-plane/api/Dockerfile:16-20); nothing
    may copy a worker package in, and no API module may import one — imports would work
    under pytest and fail in production, which is why mosa_check counts them
    (`api_imports_worker`, baseline 0).
    """
    dockerfile = (ROOT / "control-plane/api/Dockerfile").read_text(encoding="utf-8")
    assert "COPY app ./app" in dockerfile, "api Dockerfile no longer allowlists the app package"
    copied_worker = [
        line for line in dockerfile.splitlines() if line.lstrip().startswith(("COPY", "ADD")) and "worker" in line
    ]
    assert not copied_worker, f"api image gains a worker package: {copied_worker}"
    assert _hits(_api_sources(), re.compile(r"^\s*(?:from|import)\s+worker\b", re.M)) == [], (
        "API code imports the worker package (mosa_check api_imports_worker must stay 0)"
    )


def test_api_dispatches_only_through_celery_client():
    """ADR 0002 rule 5: "The API reaches the worker only through
    `celery_client.dispatch(name, *args)`, which validates against the task contract."

    A second Celery client or a raw `send_task` in API code would bypass contract
    validation; dispatch itself is pinned behaviourally by
    tests/contracts/test_task_contracts.py."""
    others = [f for f in _api_sources() if f.name != "celery_client.py"]
    offenders = _hits(others, re.compile(r"\bsend_task\(|\bCelery\("))
    assert not offenders, f"API code bypassing celery_client.dispatch: {offenders}"


# -- Row "Learning records" ----------------------------------------------------------


def test_published_xapi_schema_is_the_1_0_3_statement_model():
    """ADR 0002, table row 4: "Learning records | xAPI 1.0.3 statements, LTI 1.3".

    The published artifact must stay pinned to xAPI 1.0.3; statement conformance is
    pinned by tests/contracts/test_xapi_statement_conformance.py."""
    schema = json.loads((ROOT / "docs/interfaces/xapi-statement.schema.json").read_text(encoding="utf-8"))
    assert "xAPI 1.0.3" in schema["title"], f"published schema drifted off xAPI 1.0.3: {schema['title']!r}"


def test_lti_launches_speak_lti_1_3():
    """ADR 0002, table row 4: "Learning records | xAPI 1.0.3 statements, LTI 1.3".

    The tool mints messages in the LTI 1.3 claim namespace with the 1.3.0 version claim
    (control-plane/api/app/lti13.py:42-49, :349)."""
    from app import lti13

    assert lti13.CLAIM_MESSAGE_TYPE == "https://purl.imsglobal.org/spec/lti/claim/message_type"
    source = (ROOT / "control-plane/api/app/lti13.py").read_text(encoding="utf-8")
    assert '"https://purl.imsglobal.org/spec/lti/claim/version": "1.3.0"' in source, (
        "lti13 no longer stamps the LTI 1.3.0 version claim"
    )


# -- Row "Identity" ------------------------------------------------------------------


def test_auth_backends_keep_a_vendor_neutral_oidc_backend():
    """ADR 0002, table row 5: "Identity | OIDC (any compliant IdP) | `auth_backends`".

    The registry must keep a backend that is not bound to one IdP (generic_oidc), and
    every entry must implement BaseAuthBackend (control-plane/api/app/auth_backends/__init__.py:41)."""
    from app.auth_backends import _REGISTRY, BaseAuthBackend

    assert "generic_oidc" in _REGISTRY, "no IdP-agnostic OIDC backend: 'any compliant IdP' is gone"
    off_contract = {name: cls.__name__ for name, cls in _REGISTRY.items() if not issubclass(cls, BaseAuthBackend)}
    assert not off_contract, f"registry entries that are not BaseAuthBackend implementations: {off_contract}"
