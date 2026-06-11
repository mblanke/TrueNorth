"""TrueNorth Range - Microsoft Hyper-V provisioner.

Provisions VMs on a Windows Hyper-V host via WinRM using PowerShell
cmdlets (Hyper-V module).  Requires the ``pywinrm`` package and a
Hyper-V host with the Windows Remote Management service enabled.

The provisioner expects VM golden images (VHDX files) to be pre-built
by Packer and stored at ``HYPERV_IMAGE_PATH`` on the Hyper-V host.
Each new VM gets a differencing disk backed by the golden image so
clones are fast and storage-efficient.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor

try:
    import winrm  # type: ignore[import]
    from winrm.protocol import Protocol  # type: ignore[import]
except ImportError:
    winrm = None  # type: ignore[assignment]

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
# Configuration from environment
# ---------------------------------------------------------------------------
HYPERV_HOST: str = os.environ.get("HYPERV_HOST", "hyperv.local")
HYPERV_USERNAME: str = os.environ.get("HYPERV_USERNAME", "Administrator")
HYPERV_PASSWORD: str = os.environ.get("HYPERV_PASSWORD", "")
HYPERV_PORT: int = int(os.environ.get("HYPERV_PORT", "5985"))
HYPERV_USE_SSL: bool = os.environ.get("HYPERV_USE_SSL", "false").lower() == "true"
HYPERV_SWITCH: str = os.environ.get("HYPERV_SWITCH", "TrueNorth-Switch")
HYPERV_IMAGE_PATH: str = os.environ.get("HYPERV_IMAGE_PATH", "C:\\TrueNorth\\images")
HYPERV_VM_PATH: str = os.environ.get("HYPERV_VM_PATH", "C:\\TrueNorth\\vms")
HYPERV_CONCURRENCY: int = int(os.environ.get("HYPERV_CONCURRENCY", "4"))


class HypervProvisioner(BaseProvisioner):
    """Microsoft Hyper-V provisioner using WinRM + PowerShell.

    VMs are created with a differencing disk whose parent is a pre-built
    VHDX golden image (``{HYPERV_IMAGE_PATH}\\{template_name}.vhdx``).
    """

    def __init__(self) -> None:
        # Defer the hard dependency check to first use so the class can be
        # instantiated (and registered) even when pywinrm is not installed.
        self._host = HYPERV_HOST
        self._username = HYPERV_USERNAME
        self._password = HYPERV_PASSWORD
        self._port = HYPERV_PORT
        self._use_ssl = HYPERV_USE_SSL
        self._switch = HYPERV_SWITCH
        self._image_path = HYPERV_IMAGE_PATH
        self._vm_path = HYPERV_VM_PATH
        self._semaphore = asyncio.Semaphore(HYPERV_CONCURRENCY)
        self._executor = ThreadPoolExecutor(max_workers=HYPERV_CONCURRENCY)

    # ------------------------------------------------------------------ #
    # WinRM helpers
    # ------------------------------------------------------------------ #

    def _session(self):  # -> winrm.Session
        if winrm is None:
            raise RuntimeError("pywinrm is required for HypervProvisioner: pip install pywinrm")
        transport = "ssl" if self._use_ssl else "ntlm"
        scheme = "https" if self._use_ssl else "http"
        return winrm.Session(
            f"{scheme}://{self._host}:{self._port}/wsman",
            auth=(self._username, self._password),
            transport=transport,
            server_cert_validation="ignore" if not self._use_ssl else "validate",
        )

    def _run_ps(self, script: str) -> tuple[str, str, int]:
        """Run a PowerShell script via WinRM.  Returns (stdout, stderr, exit_code)."""
        session = self._session()
        result = session.run_ps(script)
        return (
            result.std_out.decode(errors="replace"),
            result.std_err.decode(errors="replace"),
            result.status_code,
        )

    async def _run_ps_async(self, script: str) -> tuple[str, str, int]:
        """Run PowerShell asynchronously via the thread executor."""
        loop = asyncio.get_running_loop()
        async with self._semaphore:
            return await loop.run_in_executor(self._executor, self._run_ps, script)

    # ------------------------------------------------------------------ #
    # VM operations
    # ------------------------------------------------------------------ #

    async def _create_vm(
        self,
        vm_name: str,
        template_name: str,
        cores: int,
        memory_mb: int,
        disk_size_gb: int,
    ) -> str:
        """Create a VM with a differencing disk backed by the golden VHDX image."""
        parent_vhdx = f"{self._image_path}\\{template_name}.vhdx"
        vm_dir = f"{self._vm_path}\\{vm_name}"
        diff_vhdx = f"{vm_dir}\\{vm_name}.vhdx"

        script = f"""
$ErrorActionPreference = 'Stop'

# Create VM directory
New-Item -ItemType Directory -Force -Path '{vm_dir}' | Out-Null

# Create differencing disk backed by golden image
New-VHD -Path '{diff_vhdx}' -ParentPath '{parent_vhdx}' -Differencing | Out-Null

# Create the VM
$vm = New-VM -Name '{vm_name}' \\
    -MemoryStartupBytes {memory_mb}MB \\
    -Generation 2 \\
    -VHDPath '{diff_vhdx}' \\
    -SwitchName '{self._switch}' \\
    -Path '{self._vm_path}'

# Configure processor count
Set-VMProcessor -VM $vm -Count {cores}

# Enable dynamic memory
Set-VMMemory -VM $vm -DynamicMemoryEnabled $true -MinimumBytes 512MB -MaximumBytes {memory_mb * 4}MB

# Enable guest services for IP reporting
Enable-VMIntegrationService -VM $vm -Name 'Guest Service Interface'
Enable-VMIntegrationService -VM $vm -Name 'Hyper-V Guest Service Interface'

$vm.Id.ToString()
"""
        stdout, stderr, rc = await self._run_ps_async(script)
        if rc != 0:
            raise RuntimeError(f"Failed to create VM {vm_name!r}: {stderr[:400]}")
        vm_id = stdout.strip()
        logger.info("Created Hyper-V VM %r (id=%s)", vm_name, vm_id)
        return vm_id or str(_uuid.uuid4())

    async def _power_action(self, vm_name: str, action: str) -> None:
        """Start, stop, or restart a VM by name.  action: Start|Stop|Restart."""
        script = f"{action}-VM -Name '{vm_name}' -Force -ErrorAction Stop"
        _, stderr, rc = await self._run_ps_async(script)
        if rc != 0:
            raise RuntimeError(f"VM {action} failed for {vm_name!r}: {stderr[:300]}")

    async def _delete_vm(self, vm_name: str) -> None:
        """Stop (if running) and delete a VM and its disk files."""
        script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$vm = Get-VM -Name '{vm_name}' -ErrorAction SilentlyContinue
if ($vm -and $vm.State -ne 'Off') {{
    Stop-VM -Name '{vm_name}' -TurnOff -Force
    Start-Sleep -Seconds 3
}}
$vhds = (Get-VMHardDiskDrive -VMName '{vm_name}' -ErrorAction SilentlyContinue).Path
Remove-VM -Name '{vm_name}' -Force -ErrorAction SilentlyContinue
foreach ($vhd in $vhds) {{
    Remove-Item -Path $vhd -Force -ErrorAction SilentlyContinue
}}
"""
        _, _, _ = await self._run_ps_async(script)

    async def _create_checkpoint(self, vm_name: str, name: str) -> None:
        """Create a checkpoint (snapshot) of a VM."""
        script = f"Checkpoint-VM -Name '{vm_name}' -SnapshotName '{name}' -ErrorAction Stop"
        _, stderr, rc = await self._run_ps_async(script)
        if rc != 0:
            raise RuntimeError(f"Checkpoint failed for {vm_name!r}: {stderr[:300]}")

    async def _get_vm_ip(self, vm_name: str) -> str | None:
        """Return the first IPv4 address reported by the VM's network adapter."""
        script = f"""
$addr = (Get-VMNetworkAdapter -VMName '{vm_name}' -ErrorAction SilentlyContinue).IPAddresses |
    Where-Object {{ $_ -match '^\\d{{1,3}}(\\.\\d{{1,3}}){{3}}$' }} |
    Select-Object -First 1
if ($addr) {{ $addr }} else {{ '' }}
"""
        stdout, _, rc = await self._run_ps_async(script)
        ip = stdout.strip()
        return ip if ip else None

    async def _wait_for_ip(self, vm_name: str, timeout: int = 120) -> str | None:
        """Poll for a VM IP until VMware Integration Services report it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ip = await self._get_vm_ip(vm_name)
            if ip:
                return ip
            await asyncio.sleep(5)
        return None

    async def _get_vm_state(self, vm_name: str) -> str:
        """Return the current power state of a VM."""
        script = f"(Get-VM -Name '{vm_name}' -ErrorAction SilentlyContinue).State"
        stdout, _, rc = await self._run_ps_async(script)
        return stdout.strip().lower() if rc == 0 else "unknown"

    # ------------------------------------------------------------------ #
    # BaseProvisioner implementation
    # ------------------------------------------------------------------ #

    async def provision(
        self,
        range_id: str,
        template: dict,
        allocations: dict,
    ) -> ProvisionResult:
        start_time = time.monotonic()
        errors: list[str] = []
        vms_out: list[dict] = []
        vm_defs = template.get("vms", [])

        async def _provision_one(vm_def: dict) -> dict:
            vm_name = f"{range_id}-{vm_def['name']}"
            template_name = vm_def.get("template_name", "ubuntu-2204")
            cores = vm_def.get("cores", 2)
            memory_mb = vm_def.get("memory", 2048)
            disk_gb = vm_def.get("disk_gb", 40)

            vm_id = await self._create_vm(vm_name, template_name, cores, memory_mb, disk_gb)
            await self._power_action(vm_name, "Start")
            ip = await self._wait_for_ip(vm_name)

            return {
                "vm_id": vm_id,
                "name": vm_def["name"],
                "status": "running",
                "ip": ip or vm_def.get("ip", ""),
            }

        tasks = [_provision_one(vm_def) for vm_def in vm_defs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for vm_def, res in zip(vm_defs, results):
            if isinstance(res, Exception):
                errors.append(f"VM {vm_def['name']}: {res}")
            else:
                vms_out.append(res)

        status = "ok" if not errors else ("partial" if vms_out else "failed")
        return ProvisionResult(
            status=status,
            vms=vms_out,
            networks=template.get("networks", []),
            duration_seconds=time.monotonic() - start_time,
            errors=errors,
        )

    async def destroy(
        self,
        range_id: str,
        provision_output: dict,
    ) -> DestroyResult:
        start_time = time.monotonic()
        errors: list[str] = []
        vms = provision_output.get("vms", [])

        tasks = [self._delete_vm(vm["name"] if "name" in vm else f"{range_id}-{vm.get('name', '')}") for vm in vms]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for vm, res in zip(vms, results):
            if isinstance(res, Exception):
                errors.append(f"VM {vm.get('name')}: {res}")

        return DestroyResult(
            status="ok" if not errors else "partial",
            resources_removed=len(vms) - len(errors),
            duration_seconds=time.monotonic() - start_time,
            errors=errors,
        )

    async def stop(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StopResult:
        start_time = time.monotonic()
        errors: list[str] = []
        vms = provision_output.get("vms", [])

        tasks = [self._power_action(f"{range_id}-{vm['name']}", "Stop") for vm in vms]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for vm, res in zip(vms, results):
            if isinstance(res, Exception):
                errors.append(f"VM {vm.get('name')}: {res}")

        return StopResult(
            status="ok" if not errors else "partial",
            vms_stopped=len(vms) - len(errors),
            duration_seconds=time.monotonic() - start_time,
            errors=errors,
        )

    async def start(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StartResult:
        start_time = time.monotonic()
        errors: list[str] = []
        vms = provision_output.get("vms", [])

        tasks = [self._power_action(f"{range_id}-{vm['name']}", "Start") for vm in vms]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for vm, res in zip(vms, results):
            if isinstance(res, Exception):
                errors.append(f"VM {vm.get('name')}: {res}")

        return StartResult(
            status="ok" if not errors else "partial",
            vms_started=len(vms) - len(errors),
            duration_seconds=time.monotonic() - start_time,
            errors=errors,
        )

    async def snapshot(
        self,
        range_id: str,
        provision_output: dict,
        name: str,
    ) -> SnapshotResult:
        start_time = time.monotonic()
        errors: list[str] = []
        vms = provision_output.get("vms", [])

        tasks = [self._create_checkpoint(f"{range_id}-{vm['name']}", name) for vm in vms]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for vm, res in zip(vms, results):
            if isinstance(res, Exception):
                errors.append(f"VM {vm.get('name')}: {res}")

        return SnapshotResult(
            status="ok" if not errors else "partial",
            snapshot_name=name,
            vms_snapped=len(vms) - len(errors),
            duration_seconds=time.monotonic() - start_time,
            errors=errors,
        )

    async def health_check(
        self,
        range_id: str,
        provision_output: dict,
    ) -> HealthResult:
        start_time = time.monotonic()
        errors: list[str] = []
        vm_statuses: dict[str, str] = {}
        vms = provision_output.get("vms", [])

        for vm in vms:
            vm_name = f"{range_id}-{vm['name']}"
            try:
                state = await self._get_vm_state(vm_name)
                vm_statuses[vm["name"]] = state
            except Exception as exc:
                vm_statuses[vm["name"]] = "error"
                errors.append(f"VM {vm['name']}: {exc}")

        healthy = not errors and all(s == "running" for s in vm_statuses.values())
        return HealthResult(
            healthy=healthy,
            status="ok" if healthy else "degraded",
            vm_statuses=vm_statuses,
            duration_seconds=time.monotonic() - start_time,
            errors=errors,
        )
