"""TrueNorth Range - Terraform provisioner.

Provisions infrastructure by generating .tfvars, running terraform
init / plan / apply / destroy via subprocess, one workspace per range.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from .base import BaseProvisioner
from .results import (
    DestroyResult,
    HealthResult,
    ProvisionResult,
    SnapshotResult,
    StartResult,
    StopResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TERRAFORM_BIN: str = os.environ.get("TERRAFORM_BIN", "terraform")
TERRAFORM_TIMEOUT: int = int(os.environ.get("TERRAFORM_TIMEOUT", "600"))
TERRAFORM_PARALLELISM: int = int(os.environ.get("TERRAFORM_PARALLELISM", "10"))

# Per-hypervisor template directories.  Each directory must contain a valid
# Terraform root module (main.tf / variables.tf / versions.tf).
_TERRAFORM_TEMPLATE_DIRS: dict[str, str] = {
    "proxmox": os.environ.get("TERRAFORM_PROXMOX_DIR", "/opt/truenorth/terraform/proxmox"),
    "vsphere": os.environ.get("TERRAFORM_VSPHERE_DIR", "/opt/truenorth/terraform/vsphere"),
    "hyperv": os.environ.get("TERRAFORM_HYPERV_DIR", "/opt/truenorth/terraform/hyperv"),
}
# Workspace root — sub-directories per range are created under here.
TERRAFORM_WORKSPACE_ROOT: str = os.environ.get("TERRAFORM_WORKSPACE_ROOT", "/opt/truenorth/terraform/workspaces")


class TerraformProvisioner(BaseProvisioner):
    """Terraform-based provisioner — one workspace per range.

    The ``hypervisor_type`` parameter selects which Terraform root module is
    copied into the per-range workspace.  Supported values: ``proxmox``
    (default), ``vsphere``, ``hyperv``.
    """

    def __init__(self, hypervisor_type: str = "proxmox") -> None:
        if hypervisor_type not in _TERRAFORM_TEMPLATE_DIRS:
            raise ValueError(
                f"Unsupported hypervisor_type {hypervisor_type!r}. "
                f"Available: {sorted(_TERRAFORM_TEMPLATE_DIRS)}"
            )
        self._tf_bin = TERRAFORM_BIN
        self._hypervisor_type = hypervisor_type
        self._template_dir = Path(_TERRAFORM_TEMPLATE_DIRS[hypervisor_type])
        self._workspace_root = Path(TERRAFORM_WORKSPACE_ROOT)
        self._timeout = TERRAFORM_TIMEOUT

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _workspace_dir(self, range_id: str) -> Path:
        """Return the workspace directory for a range."""
        return self._workspace_root / self._hypervisor_type / f"range-{range_id}"

    def _write_tfvars(self, ws: Path, template: dict, allocations: dict) -> Path:
        """Write a terraform.tfvars.json file and return its path."""
        tfvars = {
            "range_name": template.get("name", "unnamed"),
            "vms": template.get("vms", []),
            "networks": template.get("networks", []),
            "allocations": allocations,
        }
        tfvars_path = ws / "terraform.tfvars.json"
        tfvars_path.write_text(json.dumps(tfvars, indent=2))
        return tfvars_path

    async def _run_tf(
        self,
        args: list[str],
        cwd: Path,
        *,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        """Run a terraform command asynchronously and return (rc, stdout, stderr)."""
        cmd = [self._tf_bin] + args
        timeout = timeout or self._timeout
        logger.info("terraform: %s (cwd=%s)", " ".join(cmd), cwd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "TF_IN_AUTOMATION": "1"},
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise TimeoutError(f"Terraform command timed out after {timeout}s: {' '.join(cmd)}") from None

        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        logger.debug("terraform rc=%d stdout=%d bytes stderr=%d bytes", proc.returncode, len(stdout), len(stderr))
        return proc.returncode, stdout, stderr

    def _parse_outputs(self, ws: Path) -> dict:
        """Parse terraform output -json."""
        output_file = ws / "tf_output.json"
        if output_file.exists():
            return json.loads(output_file.read_text())
        return {}

    # ------------------------------------------------------------------ #
    # provision
    # ------------------------------------------------------------------ #
    async def provision(
        self,
        range_id: str,
        template: dict,
        allocations: dict,
    ) -> ProvisionResult:
        start = time.monotonic()
        ws = self._workspace_dir(range_id)
        ws.mkdir(parents=True, exist_ok=True)

        # Copy Terraform module files from the hypervisor-specific template dir
        if self._template_dir.exists():
            for f in self._template_dir.glob("*.tf"):
                shutil.copy2(f, ws / f.name)
            # Copy versions lockfile if present
            lock = self._template_dir / ".terraform.lock.hcl"
            if lock.exists():
                shutil.copy2(lock, ws / ".terraform.lock.hcl")
        else:
            logger.warning("Terraform template dir not found: %s", self._template_dir)

        self._write_tfvars(ws, template, allocations)
        errors: list[str] = []
        tf_state: str | None = None

        try:
            # init
            rc, out, err = await self._run_tf(
                ["init", "-input=false", "-no-color"],
                ws,
            )
            if rc != 0:
                errors.append(f"terraform init failed (rc={rc}): {err[:500]}")
                return ProvisionResult(
                    status="failed",
                    errors=errors,
                    duration_seconds=time.monotonic() - start,
                )

            # plan
            rc, out, err = await self._run_tf(
                ["plan", "-input=false", "-no-color", f"-parallelism={TERRAFORM_PARALLELISM}", "-out=tfplan"],
                ws,
            )
            if rc != 0:
                errors.append(f"terraform plan failed (rc={rc}): {err[:500]}")
                return ProvisionResult(
                    status="failed",
                    errors=errors,
                    duration_seconds=time.monotonic() - start,
                )

            # apply
            rc, out, err = await self._run_tf(
                ["apply", "-input=false", "-no-color", f"-parallelism={TERRAFORM_PARALLELISM}", "tfplan"],
                ws,
            )
            if rc != 0:
                errors.append(f"terraform apply failed (rc={rc}): {err[:500]}")
                return ProvisionResult(
                    status="failed",
                    errors=errors,
                    duration_seconds=time.monotonic() - start,
                )

            # capture outputs
            rc, out, err = await self._run_tf(
                ["output", "-json", "-no-color"],
                ws,
            )
            if rc == 0:
                (ws / "tf_output.json").write_text(out)

            outputs = self._parse_outputs(ws)
            vms = outputs.get("vms", {}).get("value", [])
            networks = outputs.get("networks", {}).get("value", [])

            # capture state
            state_file = ws / "terraform.tfstate"
            if state_file.exists():
                tf_state = state_file.read_text()

        except TimeoutError as exc:
            errors.append(str(exc))
            return ProvisionResult(
                status="failed",
                errors=errors,
                duration_seconds=time.monotonic() - start,
            )

        return ProvisionResult(
            status="ok",
            vms=vms,
            networks=networks,
            duration_seconds=time.monotonic() - start,
            terraform_state=tf_state,
        )

    # ------------------------------------------------------------------ #
    # destroy
    # ------------------------------------------------------------------ #
    async def destroy(
        self,
        range_id: str,
        provision_output: dict,
    ) -> DestroyResult:
        start = time.monotonic()
        ws = self._workspace_dir(range_id)
        errors: list[str] = []

        if not ws.exists():
            return DestroyResult(
                status="ok",
                resources_removed=0,
                duration_seconds=time.monotonic() - start,
            )

        try:
            rc, out, err = await self._run_tf(
                ["destroy", "-auto-approve", "-input=false", "-no-color", f"-parallelism={TERRAFORM_PARALLELISM}"],
                ws,
            )
            if rc != 0:
                errors.append(f"terraform destroy failed (rc={rc}): {err[:500]}")
                return DestroyResult(
                    status="failed",
                    errors=errors,
                    duration_seconds=time.monotonic() - start,
                )
        except TimeoutError as exc:
            errors.append(str(exc))
            return DestroyResult(
                status="failed",
                errors=errors,
                duration_seconds=time.monotonic() - start,
            )

        # Clean workspace
        shutil.rmtree(ws, ignore_errors=True)

        return DestroyResult(
            status="ok",
            resources_removed=provision_output.get("resource_count", 0),
            duration_seconds=time.monotonic() - start,
        )

    # ------------------------------------------------------------------ #
    # stop
    # ------------------------------------------------------------------ #
    async def stop(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StopResult:
        """Stop VMs by applying with power_state=off variable."""
        start = time.monotonic()
        ws = self._workspace_dir(range_id)
        errors: list[str] = []

        try:
            rc, out, err = await self._run_tf(
                [
                    "apply",
                    "-auto-approve",
                    "-input=false",
                    "-no-color",
                    "-var",
                    "power_state=off",
                    f"-parallelism={TERRAFORM_PARALLELISM}",
                ],
                ws,
            )
            if rc != 0:
                errors.append(f"terraform stop failed (rc={rc}): {err[:500]}")
                return StopResult(
                    status="failed",
                    errors=errors,
                    duration_seconds=time.monotonic() - start,
                )
        except TimeoutError as exc:
            errors.append(str(exc))
            return StopResult(
                status="failed",
                errors=errors,
                duration_seconds=time.monotonic() - start,
            )

        vm_count = len(provision_output.get("vms", []))
        return StopResult(
            status="ok",
            vms_stopped=vm_count,
            duration_seconds=time.monotonic() - start,
        )

    # ------------------------------------------------------------------ #
    # start
    # ------------------------------------------------------------------ #
    async def start(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StartResult:
        """Start VMs by applying with power_state=on variable."""
        start = time.monotonic()
        ws = self._workspace_dir(range_id)
        errors: list[str] = []

        try:
            rc, out, err = await self._run_tf(
                [
                    "apply",
                    "-auto-approve",
                    "-input=false",
                    "-no-color",
                    "-var",
                    "power_state=on",
                    f"-parallelism={TERRAFORM_PARALLELISM}",
                ],
                ws,
            )
            if rc != 0:
                errors.append(f"terraform start failed (rc={rc}): {err[:500]}")
                return StartResult(
                    status="failed",
                    errors=errors,
                    duration_seconds=time.monotonic() - start,
                )
        except TimeoutError as exc:
            errors.append(str(exc))
            return StartResult(
                status="failed",
                errors=errors,
                duration_seconds=time.monotonic() - start,
            )

        vm_count = len(provision_output.get("vms", []))
        return StartResult(
            status="ok",
            vms_started=vm_count,
            duration_seconds=time.monotonic() - start,
        )

    # ------------------------------------------------------------------ #
    # snapshot
    # ------------------------------------------------------------------ #
    async def snapshot(
        self,
        range_id: str,
        provision_output: dict,
        name: str,
    ) -> SnapshotResult:
        """Terraform does not natively snapshot; delegate to provider scripts."""
        start = time.monotonic()
        logger.warning(
            "TerraformProvisioner.snapshot is a no-op stub for range %s",
            range_id,
        )
        return SnapshotResult(
            status="ok",
            snapshot_name=name,
            vms_snapped=0,
            duration_seconds=time.monotonic() - start,
            errors=["Snapshot not natively supported via Terraform; use provider-specific tooling"],
        )

    # ------------------------------------------------------------------ #
    # health_check
    # ------------------------------------------------------------------ #
    async def health_check(
        self,
        range_id: str,
        provision_output: dict,
    ) -> HealthResult:
        start = time.monotonic()
        ws = self._workspace_dir(range_id)

        if not ws.exists():
            return HealthResult(
                healthy=False,
                status="unhealthy",
                errors=[f"Workspace not found for range {range_id}"],
                duration_seconds=time.monotonic() - start,
            )

        try:
            rc, out, err = await self._run_tf(
                ["plan", "-detailed-exitcode", "-input=false", "-no-color"],
                ws,
            )
            # rc 0 = no changes, 2 = changes detected, other = error
            if rc == 0:
                status = "ok"
                healthy = True
            elif rc == 2:
                status = "degraded"
                healthy = False
            else:
                status = "unhealthy"
                healthy = False
        except TimeoutError:
            status = "unhealthy"
            healthy = False

        return HealthResult(
            healthy=healthy,
            status=status,
            vm_statuses=[],
            duration_seconds=time.monotonic() - start,
        )
