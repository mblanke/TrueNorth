"""Tripwires for ADR 0001 — docs/adr/0001-adapter-registry.md.

Each test pins one DECISION of the ADR (quoted in its docstring) so the day the code
drifts from the accepted decision, a test goes red — not just the MOSA ratchet.

1. "Any new external system is reached only through an ABC in a ``*_backends/`` (or
   ``provisioners/``) package, selected by a registry keyed on config or a DB column."
     -> test_adr_seam_table_is_still_the_code          (documented seams exist where the ADR says)
     -> test_registries_select_by_config_or_column_key (selection really is config/column keyed)
     -> test_adapter_packages_keep_the_abc_registry_factory_shape
2. "Vendor SDKs (``proxmoxer``, ``pyVmomi``, ``opensearchpy``, …) are imported only inside
   adapter packages. Routers, tasks and services never import them."
     -> test_vendor_sdks_are_imported_only_inside_adapter_packages
3. "No ``if <kind> == \"<vendor>\"`` dispatch in routers; ask the registry."
     -> test_routers_never_dispatch_on_a_kind_literal
4. "Each ABC has one contract test suite that every registered implementation runs."
     -> test_every_adapter_abc_has_a_contract_suite

Scan scope mirrors ``scripts/mosa_check.py`` (the ADR's own enforcement, see its
Consequences): product code under control-plane/, scenario-engine/, ai-orchestrator/
and tools/. Operator scripts and tests sit outside the rule; the recorded debt lives
in ``.dod-mosa-baseline``, not here.

Read-only over source and in-process app objects; no network (tests/contracts/conftest.py
blocks sockets), no mocks of the unit under test, no skips.
"""

from __future__ import annotations

import ast
import importlib
import sys
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = ROOT / "tests"

#: Where product code lives (mosa_check.SDK_SCAN_DIRS).
PRODUCT_DIRS = ("control-plane", "scenario-engine", "ai-orchestrator", "tools")

#: Vendor SDKs named by ADR 0001 decision 2 (both spellings of pyVmomi, as in mosa_check).
VENDOR_SDKS = frozenset({"proxmoxer", "pyVmomi", "pyvmomi", "opensearchpy"})

#: The kind variables decision 3 forbids dispatching on in routers
#: (mosa_check metric ``hardcoded_backend_branches``).
KIND_NAMES = frozenset({"hypervisor_type", "platform_type"})

ROUTERS_DIR = ROOT / "control-plane" / "api" / "app" / "routers"


def _product_py_files() -> list[Path]:
    files: list[Path] = []
    for d in PRODUCT_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        files.extend(p for p in base.rglob("*.py") if ".venv" not in p.parts and "node_modules" not in p.parts)
    return files


def _is_adapter_dir(dir_path: Path) -> bool:
    """True for the adapter packages ADR 0001 decision 1 names: ``*_backends/`` (or
    ``provisioners/``) packages, plus the two seam packages the ADR table rows use that
    predate that naming (``app/notifications/``, ``app/lms/``)."""
    rel = dir_path.relative_to(ROOT).as_posix()
    parts = rel.split("/")
    if rel.startswith("scenario-engine/scenario_engine/event_stores"):
        return rel == "scenario-engine/scenario_engine/event_stores" or len(parts) > 3
    if rel.startswith("ai-orchestrator/app/backends"):
        return rel == "ai-orchestrator/app/backends" or len(parts) > 2
    if rel.startswith("control-plane/worker/worker"):
        return len(parts) >= 4 and parts[3] in ("provisioners", "feed_backends")
    if rel.startswith("control-plane/api/app"):
        if len(parts) >= 4 and (parts[3].endswith("_backends") or parts[3] in ("notifications", "lms")):
            return True
        if len(parts) >= 5 and parts[3] == "scheduler" and parts[4] == "calendar_backends":
            return True
    return False


def _adapter_packages() -> list[Path]:
    """Directories of every adapter package (each has an ``__init__.py``)."""
    pkgs = []
    for d in PRODUCT_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for init in base.rglob("__init__.py"):
            if ".venv" not in init.parts and _is_adapter_dir(init.parent):
                pkgs.append(init.parent)
    return sorted(set(pkgs))


def _ast_trees(paths: list[Path]):
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        yield path, tree


def _is_abc_class(node: ast.AST) -> bool:
    return isinstance(node, ast.ClassDef) and any(
        isinstance(b, (ast.Name, ast.Attribute)) and getattr(b, "id", getattr(b, "attr", None)) in ("ABC", "ABCMeta")
        for b in node.bases
    )


def _package_surface(pkg_dir: Path) -> tuple[set[str], set[str], set[str]]:
    """(ABC class names, get_* factory names, registry mapping names) parsed from a package."""
    classes, factories, registries = set(), set(), set()
    for _, tree in _ast_trees(sorted(pkg_dir.rglob("*.py"))):
        for node in ast.walk(tree):
            if _is_abc_class(node):
                classes.add(node.name)
            elif isinstance(node, ast.FunctionDef) and node.name.startswith("get_"):
                factories.add(node.name)
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and "registry" in t.id.lower() for t in node.targets):
                registries.add(node.targets[0].id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and "registry" in node.target.id.lower():
                registries.add(node.target.id)
    return classes, factories, registries


# ---------------------------------------------------------------------------
# Decision 1 — ABC + registry shape, documented seams, config/column selection
# ---------------------------------------------------------------------------

#: Rows of the ADR 0001 Decision table: (seam, interface module, interface attr,
#: registry module, registry attr). The ADR pins these exact paths and names.
ADR_SEAM_TABLE = [
    ("provisioning", "worker.provisioners.base", "BaseProvisioner", "worker.provisioners", "get_provisioner"),
    ("auth", "app.auth_backends.base", "BaseAuthBackend", "app.auth_backends", "get_auth_backend"),
    ("lms / xapi", "app.lms.base", "BaseLMSBackend", "app.lms", "get_lms_backend"),
    ("search", "app.search_backends.base", "BaseSearchBackend", "app.search_backends", "get_search_backend"),
    ("threat intel feeds", "app.threat_intel_backends.base", "BaseFeedBackend", "app.threat_intel_backends", "get_feed_backend"),
    ("notifications", "app.notifications.base", "NotificationChannel", "app.notifications.registry", "get_channel"),
    ("ai", "_ai_orch.backends.base", "BaseAIBackend", "_ai_orch.backends", "get_cloud_backend"),
    ("ai engine admin", "app.ai_backends.base", "BaseAIEngine", "app.ai_backends", "get_ai_engine"),
]


@pytest.mark.parametrize(
    ("seam", "interface_module", "interface_attr", "registry_module", "registry_attr"),
    ADR_SEAM_TABLE,
    ids=[row[0] for row in ADR_SEAM_TABLE],
)
def test_adr_seam_table_is_still_the_code(seam, interface_module, interface_attr, registry_module, registry_attr):
    """ADR 0001 decision 1 and its seam table: "Any new external system is reached only
    through an ABC in a ``*_backends/`` (or ``provisioners/``) package, selected by a
    registry keyed on config or a DB column." — every seam the ADR documents (Provisioning
    ``BaseProvisioner``/``get_provisioner()``, Auth, LMS/xAPI, Search, Threat intel feeds
    ``BaseFeedBackend``, Notifications, AI, AI engine admin ``BaseAIEngine``/``get_ai_engine()``)
    must still exist at the documented path. Renaming or deleting a documented seam is drift."""
    try:
        interface = importlib.import_module(interface_module)
    except ImportError as exc:  # _ai_orch.backends is a namespace stub: __spec__ is None
        raise AssertionError(f"{seam}: ADR 0001 interface module {interface_module} is gone") from exc
    assert hasattr(interface, interface_attr), f"{seam}: ADR 0001 interface attr {interface_attr} missing from {interface_module}"

    try:
        registry = importlib.import_module(registry_module)
    except ImportError as exc:
        raise AssertionError(f"{seam}: ADR 0001 registry module {registry_module} is gone") from exc
    assert callable(getattr(registry, registry_attr, None)), (
        f"{seam}: ADR 0001 registry getter {registry_attr} missing from {registry_module}"
    )


def test_registries_select_by_config_or_column_key(monkeypatch):
    """ADR 0001 decision 1: "... selected by a registry keyed on config or a DB column."

    Env-config-keyed seams answer to their documented variable (``AUTH_BACKEND``,
    ``LMS_BACKEND``, ``SEARCH_BACKEND``, ``VECTOR_BACKEND``, ``CALENDAR_BACKEND``); seams
    keyed on a DB column / row-carried key answer to that key (``hypervisor_type`` on a
    connection row, ``backend_type`` on an AI backend config, ``feed_type``, provisioner
    ``backend``, notification channel name, event-store name). All in-process, offline.
    """
    from app import auth_backends, lms, search_backends, vector_backends

    # config-keyed: setting the variable selects that registered backend.
    for pkg, getter, reset, var, key in (
        (auth_backends, "get_auth_backend", "_reset_backend", "AUTH_BACKEND", "disabled"),
        (lms, "get_lms_backend", "_reset_backend", "LMS_BACKEND", "null"),
        (search_backends, "get_search_backend", "_reset_backend", "SEARCH_BACKEND", "null"),
        (vector_backends, "get_vector_store", "reset_vector_store", "VECTOR_BACKEND", "null"),
    ):
        monkeypatch.setenv(var, key)
        getattr(pkg, reset)()
        try:
            backend = getattr(pkg, getter)()
            assert type(backend) is pkg._REGISTRY[key], f"{var}={key} did not select the registered backend"
        finally:
            getattr(pkg, reset)()

    from app.scheduler import calendar_backends

    monkeypatch.setenv("CALENDAR_BACKEND", "null")
    calendar_backends.reset_calendar_backend()
    try:
        assert calendar_backends.get_calendar_backend() is not None
    finally:
        calendar_backends.reset_calendar_backend()

    # column/argument-keyed: the key a DB row carries selects the implementation.
    from app.ai_backends import BaseAIEngine, get_ai_engine, known_engine_types
    from app.hypervisor_backends import BaseHypervisorBackend, get_hypervisor_backend, supported_hypervisor_types
    from app.notifications.base import NotificationChannel
    from app.notifications.registry import get_channel
    from app.threat_intel_backends import get_feed_backend
    from app.threat_intel_backends.base import BaseFeedBackend
    from worker.feed_backends import get_feed_source
    from worker.feed_backends.base import BaseFeedSource
    from worker.provisioners import get_provisioner
    from worker.provisioners.base import BaseProvisioner

    assert isinstance(get_provisioner("mock"), BaseProvisioner)  # ranges.provisioner_backend
    assert isinstance(get_feed_source("null"), BaseFeedSource)  # content feed config
    assert isinstance(get_feed_backend("null"), BaseFeedBackend)  # feed.feed_type
    for key in known_engine_types():
        assert isinstance(get_ai_engine(key), BaseAIEngine), key  # AIBackendConfig.backend_type
    for kind in supported_hypervisor_types():
        # HypervisorConnection.hypervisor_type
        assert isinstance(get_hypervisor_backend(kind), BaseHypervisorBackend), kind
    assert isinstance(get_channel("in_app"), NotificationChannel)  # notification channel name


def test_adapter_packages_keep_the_abc_registry_factory_shape():
    """ADR 0001 decision 1: "Any new external system is reached only through an ABC in a
    ``*_backends/`` (or ``provisioners/``) package, selected by a registry keyed on config
    or a DB column." — every adapter package keeps that shape: an ABC, a registry mapping,
    and a ``get_*`` factory. Checked by parsing, not importing, so a package that grew an
    import-time side effect cannot hide from this."""
    problems = []
    for pkg_dir in _adapter_packages():
        rel = pkg_dir.relative_to(ROOT).as_posix()
        classes, factories, registries = _package_surface(pkg_dir)
        if not classes:
            problems.append(f"{rel}: no ABC")
        if not registries:
            problems.append(f"{rel}: no registry mapping")
        if not factories:
            problems.append(f"{rel}: no get_* factory")
    assert not problems, "adapter packages drifted from the ADR 0001 shape:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# Decision 2 — vendor SDKs only inside adapter packages
# ---------------------------------------------------------------------------


def _vendor_sdk_offenders(paths: list[Path]) -> list[str]:
    """Files under *paths* (product code) importing a vendor SDK outside an adapter package."""
    offenders = []
    for path, tree in _ast_trees(paths):
        if _is_adapter_dir(path.parent):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name.split(".")[0] in VENDOR_SDKS:
                    rel = path.relative_to(ROOT).as_posix()
                    offenders.append(f"{rel}:{node.lineno} imports {name}")
    return offenders


def test_vendor_sdks_are_imported_only_inside_adapter_packages():
    """ADR 0001 decision 2: "Vendor SDKs (``proxmoxer``, ``pyVmomi``, ``opensearchpy``, …)
    are imported only inside adapter packages. Routers, tasks and services never import
    them." — AST-based, so it also catches indented/aliased imports a line-regex misses.
    Product code only; operator scripts and tests are out of scope (see module docstring)."""
    offenders = _vendor_sdk_offenders(_product_py_files())
    assert not offenders, (
        "vendor SDK imported outside an adapter package (ADR 0001 decision 2); route it through "
        "the adapter instead:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Decision 3 — no vendor dispatch in routers
# ---------------------------------------------------------------------------


def _kind_dispatch_offenders(router_dir: Path) -> list[str]:
    """Routers under *router_dir* comparing a kind variable to a string literal."""
    offenders = []
    for path, tree in _ast_trees(sorted(router_dir.rglob("*.py"))):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or not node.ops:
                continue
            if not all(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                continue
            left = node.left
            kind = left.attr if isinstance(left, ast.Attribute) else left.id if isinstance(left, ast.Name) else None
            if kind not in KIND_NAMES:
                continue
            if any(isinstance(c, ast.Constant) and isinstance(c.value, str) for c in node.comparators):
                rel = path.relative_to(ROOT).as_posix()
                offenders.append(f"{rel}:{node.lineno} compares {kind} to a string literal")
    return offenders


def test_routers_never_dispatch_on_a_kind_literal():
    """ADR 0001 decision 3: "No ``if <kind> == \"<vendor>\"`` dispatch in routers; ask the
    registry." — pins the two kind variables the ADR/mosa_check track (``hypervisor_type``,
    ``platform_type``, matching a Name or an attribute like ``conn.hypervisor_type``) against
    any string-literal comparison in ``app/routers/``. Routers must ask
    ``get_hypervisor_backend(conn.hypervisor_type)`` (routers/hypervisors.py) instead."""
    offenders = _kind_dispatch_offenders(ROUTERS_DIR)
    assert not offenders, (
        "vendor dispatch crept back into a router (ADR 0001 decision 3); ask the registry instead:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Scanner self-tests — a tripwire that cannot trip is decoration
# ---------------------------------------------------------------------------

_ROUTER_DRIFT = 'def f(c):\n    if c.hypervisor_type == "vsphere":\n        return 1\n'
_ROUTER_CLEAN = "def f(c):\n    return get_hypervisor_backend(c.hypervisor_type)\n"
_SDK_DRIFT = "import proxmoxer as px\nfrom pyVmomi import vim\n"
_SDK_IN_ADAPTER = "from opensearchpy import OpenSearch\n"


def _scan_synthetic(monkeypatch, source: str, fake_path: str, helper):
    """Run a scanner helper over an in-memory source pinned at a fake repo-relative path."""
    module = sys.modules[__name__]

    def fake_trees(paths):
        return iter([(Path(fake_path), ast.parse(source))])

    monkeypatch.setattr(module, "ROOT", Path("/"))
    monkeypatch.setattr(module, "_ast_trees", fake_trees)
    return helper(Path(fake_path).parent)


def test_kind_dispatch_scanner_trips_on_drift_and_passes_clean_code(monkeypatch):
    """Self-test for decision 3's scanner: it must fire on a router that dispatches on a
    kind literal and stay quiet when the router asks the registry instead."""
    drift = _scan_synthetic(monkeypatch, _ROUTER_DRIFT, "/fake/app/routers/drifted.py", _kind_dispatch_offenders)
    clean = _scan_synthetic(monkeypatch, _ROUTER_CLEAN, "/fake/app/routers/clean.py", _kind_dispatch_offenders)
    assert drift and not clean, f"scanner did not trip on drift: {drift} / {clean}"


def test_vendor_sdk_scanner_trips_on_drift_and_spares_adapters(monkeypatch):
    """Self-test for decision 2's scanner: it must fire on a vendor SDK import in product
    code, and stay quiet for the same import inside an adapter package."""
    drift = _scan_synthetic(monkeypatch, _SDK_DRIFT, "/fake/app/tasks.py", lambda d: _vendor_sdk_offenders([d / "tasks.py"]))
    adapter = _scan_synthetic(
        monkeypatch,
        _SDK_IN_ADAPTER,
        "/control-plane/api/app/search_backends/store.py",
        lambda d: _vendor_sdk_offenders([d / "store.py"]),
    )
    assert drift and not adapter, f"scanner did not trip on drift: {drift} / {adapter}"


# ---------------------------------------------------------------------------
# Decision 4 — every ABC has one contract test suite
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _test_sources() -> dict[str, str]:
    sources = {}
    for path in TESTS_DIR.rglob("*.py"):
        try:
            sources[path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return {name: src for name, src in sources.items() if not name.endswith("test_adr_0001_tripwire.py")}


def test_every_adapter_abc_has_a_contract_suite():
    """ADR 0001 decision 4: "Each ABC has one contract test suite that every registered
    implementation runs." — for every adapter package (discovered by scanning, so a newly
    added one is covered automatically), some test file must reference its ABC or its
    ``get_*`` factory: the suite that enrols its registered implementations. A new adapter
    package without a contract suite fails here, not only at review."""
    problems = []
    for pkg_dir in _adapter_packages():
        rel = pkg_dir.relative_to(ROOT).as_posix()
        classes, factories, _ = _package_surface(pkg_dir)
        names = classes | factories
        enrolled = [file for file, src in _test_sources().items() if any(name in src for name in names)]
        if not enrolled:
            problems.append(f"{rel}: ABC/factory {sorted(names)} referenced by no test under tests/")
    assert not problems, "adapter ABCs without a contract suite (ADR 0001 decision 4):\n" + "\n".join(problems)
