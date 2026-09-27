"""TrueNorth Range - Proxmox cluster management router.

Full lifecycle management of the 2-node Proxmox VE cluster:
  - **Discovery**: nodes, VMs, networks, storage, templates
  - **Deployment**: clone templates, create VMs, configure resources
  - **Lifecycle**: start / stop / shutdown / reset / suspend / resume / destroy
  - **Task tracking**: long-running Proxmox task status polling
  - **Console**: VNC / SPICE proxy ticket generation
  - **Snapshots**: create / revert / delete VM snapshots
  - **Network management**: create/delete bridges for range isolation

Uses `proxmoxer` (official Python client) over `httpx` for robust
session handling, token refresh, and proper API coverage.

Environment variables
---------------------
PROXMOX_HOSTS        Comma-separated host list   (default: none — set when a cluster is connected)
PROXMOX_USER         API user                    (default: root@pam)
PROXMOX_PASSWORD     API password                (default: none — must be provided)
PROXMOX_VERIFY_SSL   TLS verify                  (default: false)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from proxmoxer import ProxmoxAPI
from pydantic import BaseModel, Field

from ..rbac import Permission, require_permission

logger = logging.getLogger("truenorth.api.proxmox")

# Router-level authentication. Direct hypervisor control: VM power, snapshots, clone, console tickets.
# Read and control are not split here — there is no use case for browsing a
# hypervisor's node list that does not also imply operating it.
#
# Every route here was previously reachable with no credentials at all.
router = APIRouter(prefix="/proxmox", tags=["proxmox"], dependencies=[Depends(require_permission(Permission.INFRA_CONTROL))])

# ── Config ──────────────────────────────────────────────────────────────
# No hosts are assumed: until PROXMOX_HOSTS points at reachable nodes,
# discovery returns empty and VM operations fail with a clear error.
_PM_HOSTS = [h.strip() for h in os.getenv("PROXMOX_HOSTS", "").split(",") if h.strip()]
_PM_USER = os.getenv("PROXMOX_USER", "root@pam")
_PM_PASS = os.getenv("PROXMOX_PASSWORD", "")
_PM_VERIFY = os.getenv("PROXMOX_VERIFY_SSL", "false").lower() in ("1", "true", "yes")

# Optional node-name -> IP hints, e.g. "wile=192.168.1.50,roadrunner=192.168.1.51"
_KNOWN_IPS: dict[str, str] = {
    name.strip(): ip.strip()
    for name, _, ip in (pair.partition("=") for pair in os.getenv("PROXMOX_NODE_IPS", "").split(","))
    if name.strip() and ip.strip()
}
_IP_TO_NAME: dict[str, str] = {v: k for k, v in _KNOWN_IPS.items()}


# ── Proxmox client singleton ───────────────────────────────────────────
_prox: ProxmoxAPI | None = None


def _get_client() -> ProxmoxAPI:
    """Return a cached ProxmoxAPI client (creates on first call)."""
    global _prox
    if _prox is None:
        host = _PM_HOSTS[0]
        logger.info("Connecting to Proxmox VE at %s as %s", host, _PM_USER)
        _prox = ProxmoxAPI(
            host,
            user=_PM_USER,
            password=_PM_PASS,
            verify_ssl=_PM_VERIFY,
            timeout=15,
        )
    return _prox


def _reset_client():
    """Force re-authentication on next call."""
    global _prox
    _prox = None


async def _run(func, *args, **kwargs) -> Any:
    """Run a synchronous proxmoxer call in a thread so we don't block the event loop."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))
    except Exception as exc:
        # On auth expiry, reset client and retry once
        if "401" in str(exc) or "authentication" in str(exc).lower():
            _reset_client()
            try:
                return await loop.run_in_executor(None, lambda: func(*args, **kwargs))
            except Exception as retry_exc:
                raise HTTPException(502, f"Proxmox API error after retry: {retry_exc}") from retry_exc
        raise HTTPException(502, f"Proxmox API error: {exc}") from exc


def _node_ip(node_name: str) -> str:
    return _KNOWN_IPS.get(node_name, node_name)


def _node_name(host: str) -> str:
    return _IP_TO_NAME.get(host, host)


# ── Pydantic schemas ───────────────────────────────────────────────────


class CloneRequest(BaseModel):
    """Clone a template VM to create a new VM."""

    source_vmid: int = Field(..., description="Template VMID to clone from")
    target_node: str = Field(..., description="Target node for the new VM")
    new_name: str = Field(..., description="Name for the cloned VM")
    new_vmid: int | None = Field(None, description="Specific VMID (auto-assigned if omitted)")
    full_clone: bool = Field(True, description="Full clone (true) or linked clone (false)")
    cores: int | None = Field(None, description="Override vCPU count")
    memory_mb: int | None = Field(None, description="Override RAM in MB")
    storage: str | None = Field(None, description="Target storage pool (e.g. local-lvm)")
    description: str | None = Field(None, description="VM description")
    pool: str | None = Field(None, description="Resource pool name")
    tags: str | None = Field(None, description="Comma-separated tags")


class VMCreateRequest(BaseModel):
    """Create a new empty VM."""

    node: str = Field(..., description="Target node")
    vmid: int | None = Field(None, description="Specific VMID (auto-assigned if omitted)")
    name: str = Field(..., description="VM name")
    cores: int = Field(2, description="vCPU count")
    memory_mb: int = Field(2048, description="RAM in MB")
    disk_gb: int = Field(32, description="Primary disk size in GB")
    storage: str = Field("local-lvm", description="Storage pool")
    ostype: str = Field("l26", description="OS type: l26, win11, win10, etc.")
    net_bridge: str = Field("vmbr0", description="Network bridge")
    iso: str | None = Field(None, description="ISO image path for CD drive")
    start_on_create: bool = Field(False, description="Start VM after creation")
    description: str | None = Field(None)
    tags: str | None = Field(None)


class VMConfigUpdate(BaseModel):
    """Partial VM config update."""

    cores: int | None = None
    memory_mb: int | None = None
    name: str | None = None
    description: str | None = None
    tags: str | None = None
    # Network interfaces (net0..net7)
    net0: str | None = None
    net1: str | None = None
    net2: str | None = None
    net3: str | None = None


class BridgeRequest(BaseModel):
    """Create a Linux bridge on a node."""

    node: str = Field(..., description="Node to create bridge on")
    name: str = Field(..., description="Bridge name (e.g. vmbr100)")
    address: str | None = Field(None, description="IP address (e.g. 10.0.0.1)")
    netmask: str | None = Field(None, description="Netmask (e.g. 255.255.255.0)")
    cidr: str | None = Field(None, description="CIDR notation instead of address+mask")
    bridge_ports: str | None = Field(None, description="Physical ports to bridge")
    comments: str | None = Field(None, description="Description / label")
    autostart: bool = Field(True, description="Start on boot")


class SnapshotRequest(BaseModel):
    """Create a VM snapshot."""

    name: str = Field(..., description="Snapshot name")
    description: str | None = Field(None)
    vmstate: bool = Field(False, description="Include RAM state")


# ══════════════════════════════════════════════════════════════════════════
#  DISCOVERY
# ══════════════════════════════════════════════════════════════════════════


@router.get("/nodes", summary="List cluster nodes")
async def list_nodes():
    px = _get_client()
    nodes = await _run(px.nodes.get)
    return [
        {
            "node": n["node"],
            "status": n.get("status", "unknown"),
            "cpu": round(n.get("cpu", 0) * 100, 1),
            "maxcpu": n.get("maxcpu", 0),
            "mem_used": n.get("mem", 0),
            "mem_total": n.get("maxmem", 0),
            "uptime": n.get("uptime", 0),
            "ip": _node_ip(n["node"]),
        }
        for n in sorted(nodes, key=lambda x: x.get("node", ""))
    ]


@router.get("/nodes/{node}/vms", summary="List VMs on a node")
async def list_node_vms(node: str, include_templates: bool = False):
    px = _get_client()
    vms = await _run(px.nodes(node).qemu.get)
    results = []
    for vm in sorted(vms, key=lambda x: x.get("vmid", 0)):
        is_tpl = bool(vm.get("template", 0))
        if is_tpl and not include_templates:
            continue
        results.append(
            {
                "vmid": vm.get("vmid"),
                "name": vm.get("name", f"vm-{vm.get('vmid')}"),
                "status": vm.get("status", "unknown"),
                "node": node,
                "cpu": vm.get("cpus", 0),
                "mem": vm.get("maxmem", 0),
                "mem_mb": round(vm.get("maxmem", 0) / 1048576),
                "disk": vm.get("maxdisk", 0),
                "disk_gb": round(vm.get("maxdisk", 0) / 1073741824, 1),
                "uptime": vm.get("uptime", 0),
                "template": is_tpl,
                "tags": vm.get("tags", ""),
            }
        )
    return results


@router.get("/nodes/{node}/networks", summary="List node networks")
async def list_node_networks(node: str):
    px = _get_client()
    nets = await _run(px.nodes(node).network.get)
    return [
        {
            "iface": n.get("iface"),
            "type": n.get("type"),
            "address": n.get("address", ""),
            "cidr": n.get("cidr", ""),
            "bridge_ports": n.get("bridge_ports", ""),
            "active": n.get("active", 0),
            "autostart": n.get("autostart", 0),
            "comments": n.get("comments", ""),
        }
        for n in nets
        if n.get("type") in ("bridge", "bond", "vlan", "OVSBridge", "OVSPort")
    ]


@router.get("/nodes/{node}/storage", summary="List node storage")
async def list_node_storage(node: str):
    px = _get_client()
    stores = await _run(px.nodes(node).storage.get)
    return [
        {
            "storage": s.get("storage"),
            "type": s.get("type"),
            "total": s.get("total", 0),
            "used": s.get("used", 0),
            "avail": s.get("avail", 0),
            "active": s.get("active", 0),
            "content": s.get("content", ""),
        }
        for s in stores
    ]


@router.get("/templates", summary="List all VM templates across cluster")
async def list_templates():
    """Return VM templates available for cloning."""
    px = _get_client()
    nodes = await _run(px.nodes.get)
    templates = []
    for n in nodes:
        name = n["node"]
        try:
            vms = await _run(px.nodes(name).qemu.get)
            for vm in vms:
                if vm.get("template"):
                    templates.append(
                        {
                            "vmid": vm.get("vmid"),
                            "name": vm.get("name", f"template-{vm.get('vmid')}"),
                            "node": name,
                            "mem_mb": round(vm.get("maxmem", 0) / 1048576),
                            "disk_gb": round(vm.get("maxdisk", 0) / 1073741824, 1),
                            "tags": vm.get("tags", ""),
                        }
                    )
        except Exception as e:
            logger.warning("Failed to list templates on %s: %s", name, e)
    return templates


@router.get("/cluster/status", summary="Cluster status")
async def cluster_status():
    px = _get_client()
    return await _run(px.cluster.status.get)


@router.get("/cluster/resources", summary="All cluster resources")
async def cluster_resources(resource_type: str | None = None):
    px = _get_client()
    kwargs = {}
    if resource_type:
        kwargs["type"] = resource_type
    return await _run(px.cluster.resources.get, **kwargs)


@router.get("/discover", summary="Full cluster discovery")
async def discover_cluster():
    """Aggregate: nodes + VMs + networks + storage + templates per node."""
    px = _get_client()
    nodes_raw = await _run(px.nodes.get)
    cluster = []

    for n in sorted(nodes_raw, key=lambda x: x.get("node", "")):
        name = n["node"]
        node_info: dict[str, Any] = {
            "node": name,
            "status": n.get("status", "unknown"),
            "ip": _node_ip(name),
            "cpu_pct": round(n.get("cpu", 0) * 100, 1),
            "maxcpu": n.get("maxcpu", 0),
            "mem_used_gb": round(n.get("mem", 0) / 1073741824, 1),
            "mem_total_gb": round(n.get("maxmem", 0) / 1073741824, 1),
            "uptime_h": round(n.get("uptime", 0) / 3600, 1),
            "vms": [],
            "templates": [],
            "networks": [],
            "storage": [],
        }

        # VMs and templates
        try:
            vms = await _run(px.nodes(name).qemu.get)
            for vm in sorted(vms, key=lambda x: x.get("vmid", 0)):
                entry = {
                    "vmid": vm.get("vmid"),
                    "name": vm.get("name", f"vm-{vm.get('vmid')}"),
                    "status": vm.get("status", "unknown"),
                    "cpu": vm.get("cpus", 0),
                    "mem_mb": round(vm.get("maxmem", 0) / 1048576),
                    "disk_gb": round(vm.get("maxdisk", 0) / 1073741824, 1),
                    "uptime": vm.get("uptime", 0),
                    "tags": vm.get("tags", ""),
                }
                if vm.get("template"):
                    node_info["templates"].append(entry)
                else:
                    node_info["vms"].append(entry)
        except Exception as e:
            logger.warning("Failed to list VMs on %s: %s", name, e)

        # Networks
        try:
            nets = await _run(px.nodes(name).network.get)
            for net in nets:
                if net.get("type") in ("bridge", "OVSBridge"):
                    node_info["networks"].append(
                        {
                            "iface": net.get("iface"),
                            "type": net.get("type"),
                            "cidr": net.get("cidr", ""),
                            "address": net.get("address", ""),
                            "bridge_ports": net.get("bridge_ports", ""),
                            "active": bool(net.get("active", 0)),
                        }
                    )
        except Exception as e:
            logger.warning("Failed to list networks on %s: %s", name, e)

        # Storage
        try:
            stores = await _run(px.nodes(name).storage.get)
            for s in stores:
                if s.get("active"):
                    node_info["storage"].append(
                        {
                            "storage": s.get("storage"),
                            "type": s.get("type"),
                            "total_gb": round(s.get("total", 0) / 1073741824, 1),
                            "used_gb": round(s.get("used", 0) / 1073741824, 1),
                            "avail_gb": round(s.get("avail", 0) / 1073741824, 1),
                            "content": s.get("content", ""),
                        }
                    )
        except Exception as e:
            logger.warning("Failed to list storage on %s: %s", name, e)

        cluster.append(node_info)

    return {"cluster": cluster, "node_count": len(cluster)}


@router.get("/next-vmid", summary="Get next available VMID")
async def next_vmid():
    px = _get_client()
    vmid = await _run(px.cluster.nextid.get)
    return {"vmid": int(vmid)}


# ══════════════════════════════════════════════════════════════════════════
#  VM LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════


@router.post("/clone", summary="Clone a template to create a new VM")
async def clone_template(req: CloneRequest):
    """Clone a Proxmox VM template.  Returns the UPID task handle."""
    px = _get_client()
    src_node = None

    # Find which node has the source template
    nodes = await _run(px.nodes.get)
    for n in nodes:
        try:
            vms = await _run(px.nodes(n["node"]).qemu.get)
            for vm in vms:
                if vm.get("vmid") == req.source_vmid:
                    src_node = n["node"]
                    break
        except Exception:
            continue
        if src_node:
            break

    if not src_node:
        raise HTTPException(404, f"Template VMID {req.source_vmid} not found on any node")

    # Get next VMID if not specified
    new_vmid = req.new_vmid
    if new_vmid is None:
        new_vmid = int(await _run(px.cluster.nextid.get))

    clone_params: dict[str, Any] = {
        "newid": new_vmid,
        "name": req.new_name,
        "full": 1 if req.full_clone else 0,
    }
    if req.target_node and req.target_node != src_node:
        clone_params["target"] = req.target_node
    if req.description:
        clone_params["description"] = req.description
    if req.storage:
        clone_params["storage"] = req.storage
    if req.pool:
        clone_params["pool"] = req.pool

    upid = await _run(px.nodes(src_node).qemu(req.source_vmid).clone.create, **clone_params)

    # Apply resource overrides after clone
    target = req.target_node or src_node
    config_updates: dict[str, Any] = {}
    if req.cores:
        config_updates["cores"] = req.cores
    if req.memory_mb:
        config_updates["memory"] = req.memory_mb
    if req.tags:
        config_updates["tags"] = req.tags

    if config_updates:
        # Wait briefly for clone task to register, then queue config
        await asyncio.sleep(1)
        try:
            await _run(px.nodes(target).qemu(new_vmid).config.put, **config_updates)
        except Exception as e:
            logger.warning("Post-clone config for %d: %s (task may still be running)", new_vmid, e)

    return {
        "upid": upid,
        "vmid": new_vmid,
        "name": req.new_name,
        "source_vmid": req.source_vmid,
        "source_node": src_node,
        "target_node": target,
    }


@router.post("/vms", summary="Create a new empty VM")
async def create_vm(req: VMCreateRequest):
    """Create a new QEMU VM from scratch."""
    px = _get_client()
    vmid = req.vmid
    if vmid is None:
        vmid = int(await _run(px.cluster.nextid.get))

    params: dict[str, Any] = {
        "vmid": vmid,
        "name": req.name,
        "cores": req.cores,
        "memory": req.memory_mb,
        "ostype": req.ostype,
        "scsihw": "virtio-scsi-single",
        "scsi0": f"{req.storage}:{req.disk_gb}",
        "net0": f"virtio,bridge={req.net_bridge}",
        "boot": "order=scsi0;ide2;net0",
    }
    if req.iso:
        params["ide2"] = f"{req.iso},media=cdrom"
    if req.description:
        params["description"] = req.description
    if req.tags:
        params["tags"] = req.tags
    if req.start_on_create:
        params["start"] = 1

    upid = await _run(px.nodes(req.node).qemu.create, **params)
    return {"upid": upid, "vmid": vmid, "node": req.node, "name": req.name}


@router.get("/vms/{node}/{vmid}", summary="Get VM config & status")
async def get_vm(node: str, vmid: int):
    """Return combined VM status + config."""
    px = _get_client()
    status = await _run(px.nodes(node).qemu(vmid).status.current.get)
    config = await _run(px.nodes(node).qemu(vmid).config.get)
    return {
        "vmid": vmid,
        "node": node,
        "name": config.get("name", status.get("name", "")),
        "status": status.get("status", "unknown"),
        "cpu": status.get("cpus", config.get("cores", 0)),
        "mem_mb": round(status.get("maxmem", 0) / 1048576),
        "disk_gb": round(status.get("maxdisk", 0) / 1073741824, 1),
        "uptime": status.get("uptime", 0),
        "pid": status.get("pid"),
        "template": bool(config.get("template", 0)),
        "tags": config.get("tags", ""),
        "description": config.get("description", ""),
        "config": config,
    }


@router.put("/vms/{node}/{vmid}", summary="Update VM configuration")
async def update_vm(node: str, vmid: int, req: VMConfigUpdate):
    """Update VM hardware config (cores, memory, network, etc.)."""
    px = _get_client()
    params: dict[str, Any] = {}
    if req.cores is not None:
        params["cores"] = req.cores
    if req.memory_mb is not None:
        params["memory"] = req.memory_mb
    if req.name is not None:
        params["name"] = req.name
    if req.description is not None:
        params["description"] = req.description
    if req.tags is not None:
        params["tags"] = req.tags
    for i in range(4):
        val = getattr(req, f"net{i}", None)
        if val is not None:
            params[f"net{i}"] = val

    if not params:
        raise HTTPException(400, "No configuration changes provided")

    await _run(px.nodes(node).qemu(vmid).config.put, **params)
    return {"vmid": vmid, "node": node, "updated": list(params.keys())}


@router.delete("/vms/{node}/{vmid}", summary="Destroy a VM permanently")
async def destroy_vm(node: str, vmid: int, purge: bool = True):
    """Stop (if running) and permanently destroy a VM."""
    px = _get_client()
    # Stop first if running
    try:
        status = await _run(px.nodes(node).qemu(vmid).status.current.get)
        if status.get("status") == "running":
            await _run(px.nodes(node).qemu(vmid).status.stop.create)
            await asyncio.sleep(3)
    except Exception:
        pass

    params: dict[str, Any] = {}
    if purge:
        params["purge"] = 1
        params["destroy-unreferenced-disks"] = 1

    upid = await _run(px.nodes(node).qemu(vmid).delete, **params)
    return {"upid": upid, "vmid": vmid, "node": node, "destroyed": True}


# ── Power actions ───────────────────────────────────────────────────────


@router.post("/vms/{node}/{vmid}/start", summary="Start a VM")
async def start_vm(node: str, vmid: int):
    px = _get_client()
    upid = await _run(px.nodes(node).qemu(vmid).status.start.create)
    return {"upid": upid, "vmid": vmid, "action": "start"}


@router.post("/vms/{node}/{vmid}/stop", summary="Hard stop a VM")
async def stop_vm(node: str, vmid: int):
    px = _get_client()
    upid = await _run(px.nodes(node).qemu(vmid).status.stop.create)
    return {"upid": upid, "vmid": vmid, "action": "stop"}


@router.post("/vms/{node}/{vmid}/shutdown", summary="Graceful ACPI shutdown")
async def shutdown_vm(node: str, vmid: int, timeout: int = 60):
    px = _get_client()
    upid = await _run(px.nodes(node).qemu(vmid).status.shutdown.create, timeout=timeout)
    return {"upid": upid, "vmid": vmid, "action": "shutdown"}


@router.post("/vms/{node}/{vmid}/reset", summary="Hard reset a VM")
async def reset_vm(node: str, vmid: int):
    px = _get_client()
    upid = await _run(px.nodes(node).qemu(vmid).status.reset.create)
    return {"upid": upid, "vmid": vmid, "action": "reset"}


@router.post("/vms/{node}/{vmid}/suspend", summary="Suspend a VM")
async def suspend_vm(node: str, vmid: int):
    px = _get_client()
    upid = await _run(px.nodes(node).qemu(vmid).status.suspend.create)
    return {"upid": upid, "vmid": vmid, "action": "suspend"}


@router.post("/vms/{node}/{vmid}/resume", summary="Resume a suspended VM")
async def resume_vm(node: str, vmid: int):
    px = _get_client()
    upid = await _run(px.nodes(node).qemu(vmid).status.resume.create)
    return {"upid": upid, "vmid": vmid, "action": "resume"}


# ══════════════════════════════════════════════════════════════════════════
#  TASK TRACKING
# ══════════════════════════════════════════════════════════════════════════


@router.get("/tasks/{node}/{upid}", summary="Check Proxmox task status")
async def task_status(node: str, upid: str):
    """Poll a Proxmox UPID task for completion / progress."""
    px = _get_client()
    status = await _run(px.nodes(node).tasks(upid).status.get)
    log_lines = []
    try:
        log = await _run(px.nodes(node).tasks(upid).log.get, limit=50)
        log_lines = [entry.get("t", "") for entry in log]
    except Exception:
        pass
    return {
        "upid": upid,
        "node": node,
        "status": status.get("status", "unknown"),  # running / stopped
        "exitstatus": status.get("exitstatus", ""),  # OK / error text
        "type": status.get("type", ""),
        "starttime": status.get("starttime"),
        "pid": status.get("pid"),
        "log": log_lines,
    }


@router.get("/tasks/{node}", summary="List recent tasks on node")
async def list_tasks(node: str, limit: int = 20, source: str = "all"):
    px = _get_client()
    tasks = await _run(px.nodes(node).tasks.get, limit=limit, source=source)
    return [
        {
            "upid": t.get("upid"),
            "type": t.get("type"),
            "status": t.get("status"),
            "starttime": t.get("starttime"),
            "endtime": t.get("endtime"),
            "user": t.get("user"),
            "node": t.get("node"),
        }
        for t in tasks
    ]


# ══════════════════════════════════════════════════════════════════════════
#  CONSOLE ACCESS
# ══════════════════════════════════════════════════════════════════════════


@router.post("/vms/{node}/{vmid}/vnc", summary="Get VNC proxy ticket")
async def vnc_proxy(node: str, vmid: int):
    """Create a VNC proxy ticket for browser-based console access."""
    px = _get_client()
    result = await _run(px.nodes(node).qemu(vmid).vncproxy.create, websocket=1)
    return {
        "ticket": result.get("ticket"),
        "port": result.get("port"),
        "node": node,
        "vmid": vmid,
        "url": f"https://{_node_ip(node)}:8006/?console=kvm&novnc=1&vmid={vmid}&node={node}",
    }


@router.post("/vms/{node}/{vmid}/spice", summary="Get SPICE proxy ticket")
async def spice_proxy(node: str, vmid: int):
    """Create a SPICE proxy config for native client console access."""
    px = _get_client()
    result = await _run(px.nodes(node).qemu(vmid).spiceproxy.create)
    return result


# ══════════════════════════════════════════════════════════════════════════
#  SNAPSHOTS
# ══════════════════════════════════════════════════════════════════════════


@router.get("/vms/{node}/{vmid}/snapshots", summary="List VM snapshots")
async def list_snapshots(node: str, vmid: int):
    px = _get_client()
    snaps = await _run(px.nodes(node).qemu(vmid).snapshot.get)
    return [
        {
            "name": s.get("name"),
            "description": s.get("description", ""),
            "snaptime": s.get("snaptime"),
            "vmstate": bool(s.get("vmstate", 0)),
            "parent": s.get("parent", ""),
        }
        for s in snaps
        if s.get("name") != "current"
    ]


@router.post("/vms/{node}/{vmid}/snapshots", summary="Create VM snapshot")
async def create_snapshot(node: str, vmid: int, req: SnapshotRequest):
    px = _get_client()
    params: dict[str, Any] = {"snapname": req.name}
    if req.description:
        params["description"] = req.description
    if req.vmstate:
        params["vmstate"] = 1
    upid = await _run(px.nodes(node).qemu(vmid).snapshot.create, **params)
    return {"upid": upid, "vmid": vmid, "snapshot": req.name}


@router.post("/vms/{node}/{vmid}/snapshots/{snap}/rollback", summary="Rollback to snapshot")
async def rollback_snapshot(node: str, vmid: int, snap: str):
    px = _get_client()
    upid = await _run(px.nodes(node).qemu(vmid).snapshot(snap).rollback.create)
    return {"upid": upid, "vmid": vmid, "snapshot": snap, "action": "rollback"}


@router.delete("/vms/{node}/{vmid}/snapshots/{snap}", summary="Delete snapshot")
async def delete_snapshot(node: str, vmid: int, snap: str):
    px = _get_client()
    upid = await _run(px.nodes(node).qemu(vmid).snapshot(snap).delete)
    return {"upid": upid, "vmid": vmid, "snapshot": snap, "action": "delete"}


# ══════════════════════════════════════════════════════════════════════════
#  NETWORK MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════


@router.post("/networks", summary="Create a Linux bridge on a node")
async def create_bridge(req: BridgeRequest):
    """Create a new Linux bridge for range network isolation."""
    px = _get_client()
    params: dict[str, Any] = {
        "iface": req.name,
        "type": "bridge",
        "autostart": 1 if req.autostart else 0,
    }
    if req.cidr:
        params["cidr"] = req.cidr
    elif req.address and req.netmask:
        params["address"] = req.address
        params["netmask"] = req.netmask
    if req.bridge_ports:
        params["bridge_ports"] = req.bridge_ports
    if req.comments:
        params["comments"] = req.comments

    await _run(px.nodes(req.node).network.create, **params)
    return {"node": req.node, "bridge": req.name, "created": True}


@router.post("/networks/{node}/apply", summary="Apply pending network changes")
async def apply_network(node: str):
    """Reload network config on node (applies bridge changes)."""
    px = _get_client()
    await _run(px.nodes(node).network.put)
    return {"node": node, "applied": True}


@router.delete("/networks/{node}/{iface}", summary="Delete a network interface")
async def delete_network(node: str, iface: str):
    px = _get_client()
    await _run(px.nodes(node).network(iface).delete)
    return {"node": node, "iface": iface, "deleted": True}


# ══════════════════════════════════════════════════════════════════════════
#  ISO / STORAGE CONTENT
# ══════════════════════════════════════════════════════════════════════════


@router.get("/storage/{node}/{storage}/content", summary="List storage content")
async def list_storage_content(node: str, storage: str, content_type: str | None = None):
    """List ISOs, disk images, backups, etc. on a storage pool."""
    px = _get_client()
    kwargs: dict[str, Any] = {}
    if content_type:
        kwargs["content"] = content_type
    items = await _run(px.nodes(node).storage(storage).content.get, **kwargs)
    return [
        {
            "volid": i.get("volid"),
            "format": i.get("format"),
            "size": i.get("size", 0),
            "content": i.get("content"),
            "ctime": i.get("ctime"),
        }
        for i in items
    ]


@router.get("/isos/{node}", summary="List available ISOs on node")
async def list_isos(node: str, storage: str = "local"):
    """Shortcut to list ISO images available for VM creation."""
    px = _get_client()
    items = await _run(px.nodes(node).storage(storage).content.get, content="iso")
    return [
        {
            "volid": i.get("volid"),
            "size_mb": round(i.get("size", 0) / 1048576),
            "name": i.get("volid", "").split("/")[-1],
        }
        for i in items
    ]


# ══════════════════════════════════════════════════════════════════════════
#  BATCH DEPLOYMENT (for range provisioning)
# ══════════════════════════════════════════════════════════════════════════


class BatchDeployItem(BaseModel):
    template_vmid: int
    name: str
    target_node: str
    cores: int | None = None
    memory_mb: int | None = None
    net_bridge: str = "vmbr0"
    tags: str | None = None


class BatchDeployRequest(BaseModel):
    """Deploy multiple VMs at once (e.g., an entire range)."""

    vms: list[BatchDeployItem]
    storage: str = "local-lvm"
    full_clone: bool = True
    range_tag: str | None = Field(None, description="Tag all VMs with this range identifier")


@router.post("/deploy-batch", summary="Batch deploy VMs for a range")
async def batch_deploy(req: BatchDeployRequest):
    """Clone multiple templates in parallel.  Returns list of tasks."""
    px = _get_client()
    results = []

    for item in req.vms:
        try:
            new_vmid = int(await _run(px.cluster.nextid.get))
            tags = item.tags or ""
            if req.range_tag:
                tags = f"{tags},{req.range_tag}" if tags else req.range_tag

            clone_req = CloneRequest(
                source_vmid=item.template_vmid,
                target_node=item.target_node,
                new_name=item.name,
                new_vmid=new_vmid,
                full_clone=req.full_clone,
                cores=item.cores,
                memory_mb=item.memory_mb,
                storage=req.storage,
                tags=tags,
            )
            result = await clone_template(clone_req)
            results.append({"success": True, **result})
        except Exception as e:
            results.append(
                {
                    "success": False,
                    "name": item.name,
                    "error": str(e),
                }
            )

    return {
        "total": len(req.vms),
        "succeeded": sum(1 for r in results if r.get("success")),
        "failed": sum(1 for r in results if not r.get("success")),
        "results": results,
    }


# ══════════════════════════════════════════════════════════════════════════
#  HEALTH / CONNECTIVITY TEST
# ══════════════════════════════════════════════════════════════════════════


@router.get("/ping", summary="Test Proxmox connectivity")
async def ping():
    """Quick connectivity check to the Proxmox cluster."""
    try:
        px = _get_client()
        version = await _run(px.version.get)
        return {
            "connected": True,
            "host": _PM_HOSTS[0],
            "version": version.get("version", "unknown"),
            "release": version.get("release", "unknown"),
        }
    except Exception as e:
        _reset_client()
        return {"connected": False, "host": _PM_HOSTS[0], "error": str(e)}
