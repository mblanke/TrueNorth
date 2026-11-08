"""ADR 0009 tripwire: tests that go red the day the code drifts from the ADR.

Each test pins one DECISION of docs/adr/0009-vsphere-provisioning-path.md (the
"## Decision" section and the "Amendment 2026-10-09"; Consequences only where the
ADR states them as done and binding). The docstring quotes the ADR line it pins.

Read-only over the sources: the worker registry and provisioners, the API's
provisioner_choice, the worker Dockerfile, the Helm chart, the installer preflight
and env template, and the reference ``infra/vsphere/terraform/main.tf``. No mocks of
anything under test, no live services, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))
sys.path.insert(0, str(ROOT / "control-plane/api"))

from app import provisioner_choice  # noqa: E402
from worker import base_tasks, fencing  # noqa: E402
from worker.provisioners import _REGISTRY, get_provisioner, vsphere_api  # noqa: E402
from worker.provisioners import EXPERIMENTAL as WORKER_EXPERIMENTAL  # noqa: E402
from worker.provisioners.base import BaseProvisioner, ExperimentalProvisionerError  # noqa: E402
from worker.provisioners.vsphere_api import VsphereAPIProvisioner  # noqa: E402

TERRAFORM_BACKENDS = ["terraform", "terraform_proxmox", "terraform_vsphere", "terraform_hyperv"]
MAIN_TF = ROOT / "infra/vsphere/terraform/main.tf"
WORKER_DOCKERFILE = ROOT / "control-plane/worker/Dockerfile"
WORKER_PACKAGE = ROOT / "control-plane/worker/worker"
PREFLIGHT = ROOT / "install/roles/tn_preflight/tasks/main.yml"
ENV_TEMPLATE = ROOT / "install/roles/tn_config/templates/env.production.j2"


# ============================================================================
# Decision 1
# ============================================================================
def test_decision_1_vsphere_api_is_the_canonical_runtime_provisioner():
    """ADR 0009 Decision 1: "`vsphere_api` is the canonical runtime provisioner for
    vSphere. Provisioning features ... are built there, behind the `BaseProvisioner`
    interface (ADR 0001)."

    Pinned: the registry maps ``vsphere_api`` to the pyVmomi/API adapter, and that
    adapter implements the shared ``BaseProvisioner`` interface — not a side path."""
    assert _REGISTRY.get("vsphere_api") is VsphereAPIProvisioner
    assert issubclass(VsphereAPIProvisioner, BaseProvisioner)


# ============================================================================
# Decision 2
# ============================================================================
def test_decision_2_terraform_vsphere_is_not_a_supported_provisioner_backend():
    """ADR 0009 Decision 2: "`TERRAFORM_VSPHERE_DIR` is not pointed at it, and
    `terraform_vsphere` is not a supported `PROVISIONER_BACKEND` value ... It has no
    per-range port groups and no reservations, so it must not build ranges."

    Pinned: the API refuses ``terraform_vsphere`` as a range's backend (it is not a
    backend at all any more), and no Terraform backend is registered."""
    assert provisioner_choice.refusal("terraform_vsphere") is not None
    for name in TERRAFORM_BACKENDS:
        assert name not in _REGISTRY, name
    assert not (WORKER_PACKAGE / "provisioners" / "terraform.py").exists()
    # The supported backends still build (the refusal is only for the removed ones).
    assert provisioner_choice.refusal("vsphere_api") is None
    assert provisioner_choice.refusal("mock") is None


# ============================================================================
# Decision 3
# ============================================================================
def _single_line_ternary_branches(text: str) -> list[tuple[str, str, str]]:
    """(line, then-branch, else-branch) for every single-line HCL ternary."""
    out = []
    for line in text.splitlines():
        if "?" not in line or ":" not in line:
            continue
        _, rest = line.split("?", 1)
        if ":" not in rest:
            continue
        then_branch, else_branch = rest.split(":", 1)
        out.append((line.strip(), then_branch.strip(), else_branch.strip()))
    return out


def test_decision_3_reference_terraform_module_header_and_resource_pool_ternary():
    """ADR 0009 Decision 3: "`main.tf`'s header says so. Its resource-pool ternary
    (identical branches) is fixed."

    Pinned: ``infra/vsphere/terraform/main.tf``'s header names the module "NOT the
    range provisioning path" and points at ``vsphere_api`` and this ADR; and no
    ternary in it has identical branches — the resource-pool lookup that was the
    original identical-branch ternary is still there and still differs."""
    text = MAIN_TF.read_text()
    assert "NOT the range provisioning path" in text
    assert "vsphere_api" in text and "0009" in text
    branches = _single_line_ternary_branches(text)
    assert branches, "the resource-pool ternary is gone from main.tf"
    assert any("resource_pool" in line for line, _, _ in branches), (
        "the resource-pool ternary (Decision 3) is gone from main.tf"
    )
    for line, then_branch, else_branch in branches:
        assert then_branch != else_branch, f"identical ternary branches in main.tf: {line!r}"


# ============================================================================
# Amendment 2026-10-09, bullet 1: the Terraform backends are removed everywhere
# ============================================================================
def test_amendment_terraform_backends_removed_from_the_worker():
    """ADR 0009 Amendment: "`terraform`, `terraform_proxmox`, `terraform_vsphere` and
    `terraform_hyperv` are removed from the registry, and `provisioners/terraform.py`
    with them. ... The worker image no longer creates
    `/opt/truenorth/terraform/workspaces`, and the Helm chart no longer mounts it."

    Pinned: the registry is exactly the four surviving backends, no worker source
    names ``TerraformProvisioner`` any more, the worker image installs no terraform
    and creates no terraform workspace, and no chart file mentions terraform."""
    assert set(_REGISTRY) == {"mock", "proxmox_api", "vsphere_api", "hyperv"}
    for path in WORKER_PACKAGE.rglob("*.py"):
        assert "TerraformProvisioner" not in path.read_text(), path
    image = WORKER_DOCKERFILE.read_text()
    assert "terraform" not in image.lower(), "the worker image builds in terraform again"
    for path in (ROOT / "infra/k8s/helm").rglob("*"):
        if path.is_file():
            assert "terraform" not in path.read_text(errors="ignore").lower(), path


# ============================================================================
# Amendment, bullet 2: proxmox_api and hyperv registered but experimental
# ============================================================================
def test_amendment_experimental_backends_registered_but_off_by_default(monkeypatch):
    """ADR 0009 Amendment: "`proxmox_api` and `hyperv` stay registered but are
    **experimental**: off unless `EXPERIMENTAL_PROVISIONERS` is true. ... With the flag
    off: the worker's `get_provisioner()` raises `ExperimentalProvisionerError`."

    Pinned: both are in the registry, both are refused with the flag unset (and with
    it spelled false), both build once it is true, and a teardown is allowed even with
    the flag off (a range built while it was on must stay removable)."""
    assert {"proxmox_api", "hyperv"} == WORKER_EXPERIMENTAL
    assert {"proxmox_api", "hyperv"} <= set(_REGISTRY)
    monkeypatch.delenv("EXPERIMENTAL_PROVISIONERS", raising=False)
    for name in sorted(WORKER_EXPERIMENTAL):
        with pytest.raises(ExperimentalProvisionerError, match="EXPERIMENTAL_PROVISIONERS"):
            get_provisioner(name)
        monkeypatch.setenv("EXPERIMENTAL_PROVISIONERS", "false")
        with pytest.raises(ExperimentalProvisionerError):
            get_provisioner(name)
        monkeypatch.delenv("EXPERIMENTAL_PROVISIONERS", raising=False)
        assert isinstance(get_provisioner(name, teardown=True), BaseProvisioner)
        monkeypatch.setenv("EXPERIMENTAL_PROVISIONERS", "true")
        assert isinstance(get_provisioner(name), BaseProvisioner)
        monkeypatch.delenv("EXPERIMENTAL_PROVISIONERS", raising=False)


def test_amendment_the_experimental_refusal_is_final():
    """ADR 0009 Amendment: "the worker's `get_provisioner()` raises
    `ExperimentalProvisionerError`, a final error (no retry; the range is recorded
    `failed` with the reason)."

    Pinned: the error is one of fencing's FINAL_ERRORS and is excluded from
    ReliableTask's autoretry, so a switched-off experimental backend fails a build at
    once instead of retrying three times."""
    assert issubclass(ExperimentalProvisionerError, fencing.FINAL_ERRORS)
    assert ExperimentalProvisionerError in base_tasks.ReliableTask.dont_autoretry_for


def test_amendment_api_answers_409_naming_the_fix():
    """ADR 0009 Amendment: "the API answers 409, naming the fix, to `POST /ranges` and
    refuses a lab launch, before any range row exists (`app/provisioner_choice.py`)".

    Pinned: ``configured_backend()`` — the value POST /ranges records — raises 409 for
    a switched-off experimental backend with a detail that names the fix
    (``EXPERIMENTAL_PROVISIONERS``), 409 for a removed Terraform backend, and the
    backend itself for a supported one; and the lab-launch path runs the same refusal
    (app/lab_sessions/service.py)."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("PROVISIONER_BACKEND", "proxmox_api")
        monkeypatch.delenv("EXPERIMENTAL_PROVISIONERS", raising=False)
        with pytest.raises(HTTPException) as exc:
            provisioner_choice.configured_backend()
        assert exc.value.status_code == 409
        assert "EXPERIMENTAL_PROVISIONERS" in exc.value.detail  # the fix, named

        monkeypatch.setenv("PROVISIONER_BACKEND", "terraform_vsphere")
        with pytest.raises(HTTPException) as exc:
            provisioner_choice.configured_backend()
        assert exc.value.status_code == 409

        monkeypatch.setenv("PROVISIONER_BACKEND", "vsphere_api")
        assert provisioner_choice.configured_backend() == "vsphere_api"
    finally:
        monkeypatch.undo()
    launch = (ROOT / "control-plane/api/app/lab_sessions/service.py").read_text()
    assert "provisioner_choice.refusal" in launch, "the lab launch no longer checks the backend gate"


def test_amendment_installer_preflight_refuses_experimental_backend_without_the_flag():
    """ADR 0009 Amendment: "the installer preflight refuses
    `tn_provisioner_backend: proxmox_api|hyperv` unless
    `tn_experimental_provisioners: true` (rendered to `EXPERIMENTAL_PROVISIONERS`)."

    Pinned: the preflight's backend assert knows the same supported and experimental
    lists as the code and requires ``tn_experimental_provisioners`` for an
    experimental one; the env template renders that inventory flag to
    ``EXPERIMENTAL_PROVISIONERS``; and no removed Terraform backend is valid there."""
    tasks = yaml.safe_load(PREFLIGHT.read_text())
    backend_task = next(
        t for t in tasks
        if isinstance(t, dict) and "the provisioner backend is supported" in (t.get("name") or "")
    )
    assert backend_task["vars"]["_supported"] == ["mock", "vsphere_api"]
    assert backend_task["vars"]["_experimental"] == ["proxmox_api", "hyperv"]
    condition = backend_task["ansible.builtin.assert"]["that"]
    assert "tn_experimental_provisioners" in condition
    for name in TERRAFORM_BACKENDS:
        assert name not in condition, name
    rendered = ENV_TEMPLATE.read_text()
    line = next(ln for ln in rendered.splitlines() if ln.startswith("EXPERIMENTAL_PROVISIONERS="))
    assert "tn_experimental_provisioners" in line


# ============================================================================
# Amendment, bullet 3: the supported backends
# ============================================================================
def test_amendment_supported_backends_are_vsphere_api_and_mock():
    """ADR 0009 Amendment: "Supported backends are `vsphere_api` and `mock` (a site
    without vCenter, tests)." — and bullet 2's "a contract test keeps its list equal
    to the worker registry".

    Pinned: the API's SUPPORTED list is exactly {mock, vsphere_api}; the worker
    registry minus the experimental set is the same two; the API's experimental set
    equals the worker's; and registry = supported + experimental, so the API accepts
    no backend a worker cannot build."""
    assert {"mock", "vsphere_api"} == provisioner_choice.SUPPORTED
    assert set(_REGISTRY) - WORKER_EXPERIMENTAL == {"mock", "vsphere_api"}
    assert provisioner_choice.EXPERIMENTAL == WORKER_EXPERIMENTAL
    assert set(_REGISTRY) == provisioner_choice.SUPPORTED | provisioner_choice.EXPERIMENTAL


# ============================================================================
# Consequence, stated done and binding (v1.0.0 H6): the range's own connection
# ============================================================================
def test_consequence_every_operation_takes_the_ranges_own_connection(monkeypatch):
    """ADR 0009 Consequences (done 2026-10-09): "`base_tasks._get_backend(backend,
    range_id)` passes the range's own tenant's vSphere connection ... to
    `VsphereAPIProvisioner` for build, destroy, stop/start, snapshot/restore/delete
    and health ... With no connection, the `VSPHERE_*` environment is the fallback."

    Pinned: only ``vsphere_api`` takes connection credentials (mapped to the
    ``vsphere`` connection family); a resolution with no range reaches the
    ``VSPHERE_*`` environment, not any database row; and the build and destroy tasks
    ask ``_get_backend`` for the recorded connection (``build=True`` /
    ``teardown=True``). The operation-level binding behaviour is regression-covered by
    tests/worker/test_vsphere_connection_binding.py."""
    from worker.provisioners import CREDENTIALS_HYPERVISOR

    assert CREDENTIALS_HYPERVISOR == {"vsphere_api": "vsphere"}
    monkeypatch.setattr(vsphere_api, "VSPHERE_URL", "https://env-vcenter.test")
    monkeypatch.setattr(vsphere_api, "VSPHERE_USERNAME", "env-svc")
    monkeypatch.setattr(vsphere_api, "VSPHERE_PASSWORD", "env-pw")
    prov = base_tasks._get_backend("vsphere_api", None)  # no range: nothing to resolve
    assert (prov._base_url, prov._username, prov._password) == ("https://env-vcenter.test", "env-svc", "env-pw")
    tasks_source = (WORKER_PACKAGE / "tasks.py").read_text()
    assert "_get_backend(backend, range_id, build=True)" in tasks_source  # the build records its connection
    assert "teardown=True" in tasks_source  # destroy goes to the vCenter it was built on
