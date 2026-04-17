"""TrueNorth Range — Template rendering engine.

Renders a range template YAML into Terraform-consumable variables,
including VM definitions, network definitions, VLAN maps, and
cloud-init user-data per VM role.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import yaml

from .cloud_init import CloudInitGenerator


class TemplateRenderer:
    """Renders a range template YAML into Terraform-consumable variables."""

    def __init__(self, template_path: str) -> None:
        self.template_path = Path(template_path)
        self.template: dict[str, Any] = yaml.safe_load(self.template_path.read_text(encoding="utf-8-sig"))
        self._cloud_init = CloudInitGenerator()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render(
        self,
        range_id: str,
        tenant_id: str,
        vlan_base: int = 100,
        ip_base: str = "10.0",
    ) -> dict[str, Any]:
        """Render template into Terraform variable definitions.

        Returns a dict with keys:
            range_id, range_name, tenant_id,
            vm_definitions, network_definitions, vlan_map
        """
        range_name = self.template.get("name") or self.template.get("id", range_id)

        # --- Resolve network segments ---
        vlans_raw = self._extract_vlans()
        vlan_map: dict[str, int] = {}
        network_definitions: list[dict[str, Any]] = []

        for offset, seg in enumerate(vlans_raw):
            vlan_id = vlan_base + offset
            seg_name = seg["name"]
            vlan_map[seg_name] = vlan_id

            cidr = seg.get("cidr") or f"{ip_base}.{vlan_id - vlan_base}.0/24"
            network_definitions.append(
                {
                    "name": seg_name,
                    "vlan_id": vlan_id,
                    "cidr": cidr,
                    "gateway": str(ipaddress.IPv4Network(cidr, strict=False).network_address + 1),
                    "description": seg.get("description", ""),
                }
            )

        # --- Resolve VM / node definitions ---
        nodes = self._extract_nodes()
        vm_definitions: list[dict[str, Any]] = []

        for _idx, node in enumerate(nodes):
            vm_vlan = node.get("vlan", "default")
            vm_vlan_id = vlan_map.get(vm_vlan, vlan_base)

            # Compute count (small-enterprise uses count, large uses individual nodes)
            count = int(node.get("count", 1))
            for replica in range(count):
                vm_id_suffix = f"{node['id']}-{replica}" if count > 1 else node["id"]
                vm_name = f"{range_id}-{vm_id_suffix}"

                specs = node.get("specs", {})
                vm_def: dict[str, Any] = {
                    "vmid": None,  # Allocated later by ResourceAllocator
                    "name": vm_name,
                    "node_id": node["id"],
                    "role": node.get("role", "generic"),
                    "os": node.get("os", node.get("type", "ubuntu-2204")),
                    "vlan_id": vm_vlan_id,
                    "ip": node.get("ip", ""),
                    "cores": specs.get("cores", 2),
                    "memory_mb": specs.get("memory_mb", 4096),
                    "disk_gb": specs.get("disk_gb", 60),
                    "services": node.get("services", []),
                    "tags": node.get("tags", []),
                    "cloud_init": self._cloud_init.generate(
                        role=node.get("role", "generic"),
                        hostname=vm_name,
                        ip=node.get("ip", ""),
                        domain=self.template.get("domain", "corp.truenorth.local"),
                        os_type=self._detect_os_family(node.get("os", "")),
                        services=node.get("services", []),
                    ),
                }
                vm_definitions.append(vm_def)

        return {
            "range_id": range_id,
            "range_name": range_name,
            "tenant_id": tenant_id,
            "vm_definitions": vm_definitions,
            "network_definitions": network_definitions,
            "vlan_map": vlan_map,
        }

    def render_tfvars(self, range_id: str, **kwargs: Any) -> str:
        """Render directly to .tfvars HCL format."""
        data = self.render(range_id, **kwargs)
        lines: list[str] = []

        lines.append(f'range_id   = "{data["range_id"]}"')
        lines.append(f'range_name = "{data["range_name"]}"')
        lines.append(f'tenant_id  = "{data["tenant_id"]}"')
        lines.append("")

        # Networks
        lines.append("network_definitions = [")
        for net in data["network_definitions"]:
            lines.append("  {")
            lines.append(f'    name        = "{net["name"]}"')
            lines.append(f"    vlan_id     = {net['vlan_id']}")
            lines.append(f'    cidr        = "{net["cidr"]}"')
            lines.append(f'    gateway     = "{net["gateway"]}"')
            lines.append(f'    description = "{net["description"]}"')
            lines.append("  },")
        lines.append("]")
        lines.append("")

        # VMs
        lines.append("vm_definitions = [")
        for vm in data["vm_definitions"]:
            lines.append("  {")
            lines.append(f'    name      = "{vm["name"]}"')
            lines.append(f'    role      = "{vm["role"]}"')
            lines.append(f'    os        = "{vm["os"]}"')
            lines.append(f"    vlan_id   = {vm['vlan_id']}")
            lines.append(f'    ip        = "{vm["ip"]}"')
            lines.append(f"    cores     = {vm['cores']}")
            lines.append(f"    memory_mb = {vm['memory_mb']}")
            lines.append(f"    disk_gb   = {vm['disk_gb']}")
            lines.append("  },")
        lines.append("]")

        return "\n".join(lines) + "\n"

    def render_cloudinit(self, vm: dict[str, Any]) -> str:
        """Generate cloud-init userdata for a specific VM definition dict."""
        return self._cloud_init.generate(
            role=vm.get("role", "generic"),
            hostname=vm.get("name", "vm"),
            ip=vm.get("ip", ""),
            domain=vm.get("domain", "corp.truenorth.local"),
            os_type=self._detect_os_family(vm.get("os", "")),
            services=vm.get("services", []),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_vlans(self) -> list[dict[str, Any]]:
        """Extract VLAN / network segment definitions from template."""
        # Large-enterprise format
        if "network" in self.template and "vlans" in self.template["network"]:
            return self.template["network"]["vlans"]

        # Small-enterprise format (derive from inputs)
        inputs = self.template.get("inputs", {})
        segments: list[dict[str, Any]] = []
        for key, val in inputs.items():
            if key.startswith("cidr_"):
                seg_name = key.removeprefix("cidr_")
                segments.append({"name": seg_name, "cidr": val, "description": seg_name})
        return segments or [{"name": "default", "cidr": "10.0.0.0/24", "description": "Default"}]

    def _extract_nodes(self) -> list[dict[str, Any]]:
        """Extract node / asset definitions from template."""
        if "nodes" in self.template:
            return self.template["nodes"]
        if "assets" in self.template:
            # Small format: convert assets to node-like dicts
            nodes = []
            for asset in self.template["assets"]:
                nodes.append(
                    {
                        "id": asset.get("role", "vm"),
                        "name": asset.get("role", "vm"),
                        "role": asset.get("role", "generic"),
                        "os": asset.get("os", "ubuntu-2204"),
                        "type": asset.get("type", "vm"),
                        "count": asset.get("count", 1),
                        "tags": asset.get("tags", []),
                        "services": asset.get("services", []),
                    }
                )
            return nodes
        return []

    @staticmethod
    def _detect_os_family(os_name: str) -> str:
        """Return 'windows' or 'linux' based on the OS identifier."""
        if not os_name:
            return "linux"
        lower = os_name.lower()
        if any(w in lower for w in ("windows", "win-", "win10", "win11")):
            return "windows"
        return "linux"
