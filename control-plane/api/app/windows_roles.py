"""Windows Server role catalogue: minimum sizing, install method and placement rules.

A node's ``services`` list is free-form (``rdp``, ``owa``, ``active_directory`` ...).
Entries that name a Windows Server role -- by its id or any alias -- select that role;
everything else is left alone. Roles drive three things:

* sizing: the node gets at least the largest vCPU / RAM / disk minimum of its roles
  (enforced). ``recommended`` is the build-sheet deploy spec, shown but not forced;
* install: ``feature`` roles are added after boot with Install-WindowsFeature, ``image``
  roles clone a pre-built role snapshot instead of the bare OS template (build sheet §0);
* checks: errors block a save or deploy, warnings are shown but allowed.

``recommended`` comes from docs/vm-build-sheet.md §3 where it lists the role (that table is
the largest spec any shipped range asks for, so it is too big to be a floor); ``min`` is a
workable lab floor. Roles the build sheet does not list are estimates.

This file is copied byte-for-byte to control-plane/worker/worker/windows_roles.py (the API
and worker images each ship only their own directory); a test keeps the copies identical.
Standard library only.
"""

from __future__ import annotations

GROUPS: tuple[str, ...] = ("Identity", "Infrastructure", "Apps", "Management")

# Sizing floor for any Windows Server node, whatever its roles.
BASE_SPECS: dict[str, int] = {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60}

_WS2019 = "windows-server-2019"
_WS2022 = "windows-server-2022"

# requires: ("in_range", role) -> some node in the range carries the role (warning if not)
# conflicts: roles that must not share a VM with this one (error)
ROLES: tuple[dict, ...] = (
    # ── Identity ──
    {"id": "ad-ds", "label": "Active Directory (AD DS + DNS)", "group": "Identity",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60},
     "recommended": {"vcpu": 4, "ram_mb": 8192, "disk_gb": 100},
     "install": {"method": "feature", "features": ["AD-Domain-Services", "DNS", "GPMC",
                                                   "RSAT-AD-Tools", "RSAT-DNS-Server"]},
     "aliases": ["active_directory", "ad", "ad_ds", "adds", "domain_controller"]},
    {"id": "ad-cs", "label": "AD Certificate Services (PKI)", "group": "Identity",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60},
     "install": {"method": "feature", "features": ["ADCS-Cert-Authority", "ADCS-Web-Enrollment",
                                                   "ADCS-Online-Cert"]},
     "requires": [("in_range", "ad-ds")],
     "aliases": ["adcs", "ad_cs", "ca", "pki", "ocsp"]},
    {"id": "ad-fs", "label": "AD Federation Services", "group": "Identity",
     "min": {"vcpu": 2, "ram_mb": 8192, "disk_gb": 80},
     "install": {"method": "feature", "features": ["ADFS-Federation"]},
     "requires": [("in_range", "ad-ds"), ("in_range", "ad-cs")],
     "conflicts": ["ad-ds"],
     "aliases": ["adfs", "ad_fs"]},
    {"id": "entra-connect-sim", "label": "Entra Connect (simulated)", "group": "Identity",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60},
     "install": {"method": "image", "images": {_WS2022: "srv2022-entraconnect-sim"}},
     "requires": [("in_range", "ad-ds")],
     "aliases": ["azure_ad_connect_sim", "entra_connect_sim", "aad_connect"]},
    # ── Infrastructure ──
    {"id": "dns", "label": "DNS Server", "group": "Infrastructure",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60},
     "install": {"method": "feature", "features": ["DNS", "RSAT-DNS-Server"]},
     "aliases": ["dns_server"]},
    {"id": "dhcp", "label": "DHCP Server", "group": "Infrastructure",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60},
     "install": {"method": "feature", "features": ["DHCP", "RSAT-DHCP"]},
     "aliases": ["dhcp_server"]},
    {"id": "file", "label": "File Server (SMB + DFS)", "group": "Infrastructure",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 100},
     "recommended": {"vcpu": 2, "ram_mb": 8192, "disk_gb": 500},
     "install": {"method": "feature", "features": ["FS-FileServer", "FS-DFS-Namespace",
                                                   "FS-DFS-Replication"]},
     "aliases": ["smb", "dfs", "file_server", "file-print", "vshadow"]},
    {"id": "print", "label": "Print Server", "group": "Infrastructure",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60},
     "install": {"method": "feature", "features": ["Print-Server"]},
     "aliases": ["print_spooler", "print_server"]},
    {"id": "nps", "label": "NPS / RADIUS", "group": "Infrastructure",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60},
     "install": {"method": "feature", "features": ["NPAS"]},
     "aliases": ["radius", "npas"]},
    {"id": "rras", "label": "Routing & Remote Access (VPN)", "group": "Infrastructure",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60},
     "install": {"method": "feature", "features": ["RemoteAccess", "Routing", "DirectAccess-VPN"]},
     "aliases": ["vpn", "remote_access"]},
    {"id": "wds", "label": "Windows Deployment Services", "group": "Infrastructure",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 200},
     "install": {"method": "feature", "features": ["WDS"]},
     "aliases": ["pxe"]},
    {"id": "kms", "label": "Volume Activation (KMS)", "group": "Infrastructure",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60},
     "install": {"method": "feature", "features": ["VolumeActivation"]},
     "aliases": ["volume_activation"]},
    {"id": "failover-cluster", "label": "Failover Clustering", "group": "Infrastructure",
     "min": {"vcpu": 2, "ram_mb": 8192, "disk_gb": 100},
     "install": {"method": "feature", "features": ["Failover-Clustering"]},
     "aliases": ["failover_clustering", "wsfc"]},
    {"id": "hyper-v", "label": "Hyper-V (nested)", "group": "Infrastructure",
     "min": {"vcpu": 4, "ram_mb": 16384, "disk_gb": 200},
     "recommended": {"vcpu": 8, "ram_mb": 32768, "disk_gb": 500},
     "install": {"method": "feature", "features": ["Hyper-V"]},
     "notes": "needs nested virtualisation enabled on the host VM",
     "aliases": ["hyperv"]},
    # ── Apps ──
    {"id": "iis", "label": "IIS Web Server", "group": "Apps",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60},
     "install": {"method": "feature", "features": ["Web-Server", "Web-Mgmt-Console", "Web-Asp-Net45"]},
     "aliases": ["web_server", "w3svc"]},
    {"id": "sql-server", "label": "SQL Server", "group": "Apps",
     "min": {"vcpu": 4, "ram_mb": 16384, "disk_gb": 200},
     "recommended": {"vcpu": 8, "ram_mb": 32768, "disk_gb": 500},
     "install": {"method": "image", "images": {_WS2022: "srv2022-sql2022"}},
     "aliases": ["mssql", "mssql_2019", "mssql_2022", "sql", "sql_server", "ssrs"]},
    {"id": "exchange", "label": "Exchange Server", "group": "Apps",
     "min": {"vcpu": 4, "ram_mb": 16384, "disk_gb": 200},
     "install": {"method": "image", "images": {_WS2022: "srv2022-exchange2019"}},
     "requires": [("in_range", "ad-ds")],
     "conflicts": ["ad-ds"],
     "aliases": ["exchange_2019", "exchange_se"]},
    {"id": "sharepoint", "label": "SharePoint", "group": "Apps",
     "min": {"vcpu": 4, "ram_mb": 16384, "disk_gb": 200},
     "install": {"method": "image", "images": {_WS2019: "srv2019-sharepoint2019",
                                                _WS2022: "srv2022-sharepointse"}},
     "requires": [("in_range", "ad-ds"), ("in_range", "sql-server")],
     "conflicts": ["ad-ds"],
     "aliases": ["sharepoint_2019", "sharepoint_se"]},
    {"id": "erp-app", "label": "ERP / line-of-business app", "group": "Apps",
     "min": {"vcpu": 4, "ram_mb": 16384, "disk_gb": 200},
     "install": {"method": "image", "images": {_WS2022: "srv2022-erp-app"}},
     "aliases": ["erp"]},
    {"id": "historian", "label": "Historian (simulated)", "group": "Apps",
     "min": {"vcpu": 4, "ram_mb": 8192, "disk_gb": 200},
     "recommended": {"vcpu": 4, "ram_mb": 8192, "disk_gb": 500},
     "install": {"method": "image", "images": {_WS2019: "srv2019-historian-sim"}},
     "aliases": ["osisoft_pi_sim", "historian_sim"]},
    # ── Management ──
    {"id": "mecm", "label": "MECM / SCCM", "group": "Management",
     "min": {"vcpu": 4, "ram_mb": 16384, "disk_gb": 200},
     "install": {"method": "image", "images": {_WS2022: "srv2022-mecm"}},
     "requires": [("in_range", "ad-ds")],
     "conflicts": ["ad-ds"],
     "aliases": ["sccm", "configmgr"]},
    {"id": "wsus", "label": "WSUS", "group": "Management",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 150},
     "recommended": {"vcpu": 2, "ram_mb": 8192, "disk_gb": 300},
     "install": {"method": "feature", "features": ["UpdateServices-Services", "UpdateServices-WidDB",
                                                   "UpdateServices-UI"]},
     "aliases": ["update_services"]},
    {"id": "rds", "label": "Remote Desktop Services", "group": "Management",
     "min": {"vcpu": 2, "ram_mb": 8192, "disk_gb": 100},
     "install": {"method": "feature", "features": ["RDS-RD-Server", "RDS-Gateway", "RDS-Licensing"]},
     "aliases": ["remote_desktop", "rds_session_host"]},
    {"id": "jump", "label": "Admin jump box (RSAT)", "group": "Management",
     "min": {"vcpu": 2, "ram_mb": 4096, "disk_gb": 60},
     "install": {"method": "feature", "features": ["RSAT-AD-Tools", "RSAT-DNS-Server", "GPMC"]},
     "aliases": ["jump_host", "rsat"]},
)

BY_ID: dict[str, dict] = {r["id"]: r for r in ROLES}
_ALIAS: dict[str, str] = {}
for _r in ROLES:
    _ALIAS[_r["id"]] = _r["id"]
    for _a in _r.get("aliases", ()):
        _ALIAS[_a] = _r["id"]


def is_windows_server(os_name: str) -> bool:
    return (os_name or "").strip().lower().startswith("windows-server")


def role_id(name: str) -> str | None:
    """The role an entry in a node's services names, or None if it is not a role."""
    key = str(name or "").strip().lower()
    return _ALIAS.get(key) or _ALIAS.get(key.replace("_", "-")) or _ALIAS.get(key.replace("-", "_"))


def roles_of(services) -> list[str]:
    """Role ids selected by a services list (or comma string), in catalogue order."""
    if isinstance(services, str):
        services = services.split(",")
    picked = {role_id(s) for s in (services or [])} - {None}
    return [r["id"] for r in ROLES if r["id"] in picked]


def min_specs(roles: list[str], key: str = "min") -> dict[str, int]:
    """Largest minimum of each resource among the roles, never below BASE_SPECS.

    ``key="recommended"`` gives the same for the recommended sizes."""
    out = dict(BASE_SPECS)
    for rid in roles:
        for k, v in BY_ID[rid].get(key, BY_ID[rid]["min"]).items():
            out[k] = max(out[k], v)
    return out


def image_roles(roles: list[str]) -> list[str]:
    return [r for r in roles if BY_ID[r]["install"]["method"] == "image"]


def role_image(roles: list[str], os_name: str) -> str | None:
    """Content Library item for the node's image role on this OS (None if it has none)."""
    imgs = image_roles(roles)
    if not imgs:
        return None
    return BY_ID[imgs[0]]["install"]["images"].get((os_name or "").strip().lower())


def role_features(roles: list[str]) -> list[str]:
    """Windows features to install for the node's feature roles, de-duplicated, in order."""
    out: list[str] = []
    for rid in roles:
        inst = BY_ID[rid]["install"]
        if inst["method"] == "feature":
            out.extend(f for f in inst["features"] if f not in out)
    return out


def check_node(name: str, os_name: str, roles: list[str], range_roles: set[str]) -> tuple[list[str], list[str]]:
    """(errors, warnings) for one node. ``range_roles`` is every role in the range.

    Only Windows Server nodes have roles: ``vpn`` on a firewall or ``rsat`` on a
    workstation is just a service name there."""
    errors: list[str] = []
    warnings: list[str] = []
    if not roles or not is_windows_server(os_name):
        return errors, warnings
    imgs = image_roles(roles)
    if len(imgs) > 1:
        errors.append(f"{name}: {', '.join(imgs)} are each a separate product image; put them on separate VMs")
    elif imgs and role_image(roles, os_name) is None:
        have = ", ".join(sorted(BY_ID[imgs[0]]["install"]["images"]))
        errors.append(f"{name}: no {imgs[0]} image for {os_name}; available on {have}")
    for rid in roles:
        role = BY_ID[rid]
        for other in role.get("conflicts", ()):
            if other in roles:
                errors.append(f"{name}: {rid} must not run on the same VM as {other}")
        for kind, other in role.get("requires", ()):
            if kind == "in_range" and other not in range_roles:
                warnings.append(f"{name}: {rid} expects a {other} server in the range")
        if role.get("notes"):
            warnings.append(f"{name}: {rid} {role['notes']}")
    return errors, warnings


def check_nodes(nodes: list[dict]) -> tuple[list[str], list[str]]:
    """(errors, warnings) for template nodes ({name|id, os, services})."""
    per_node = [(str(n.get("name") or n.get("id") or "node"), str(n.get("os") or ""), roles_of(n.get("services")))
                for n in nodes if isinstance(n, dict) and is_windows_server(str(n.get("os") or ""))]
    range_roles = {r for _, _, roles in per_node for r in roles}
    errors: list[str] = []
    warnings: list[str] = []
    for name, os_name, roles in per_node:
        e, w = check_node(name, os_name, roles, range_roles)
        errors.extend(e)
        warnings.extend(w)
    return errors, warnings


def catalogue() -> dict:
    """JSON-ready catalogue for the designer."""
    return {
        "groups": list(GROUPS),
        "base": dict(BASE_SPECS),
        "roles": [{"id": r["id"], "label": r["label"], "group": r["group"], "min": dict(r["min"]),
                   "recommended": dict(r.get("recommended", r["min"])),
                   "method": r["install"]["method"],
                   "images": sorted(r["install"].get("images", {})),
                   "requires": [o for _, o in r.get("requires", ())],
                   "conflicts": list(r.get("conflicts", ())),
                   "aliases": list(r.get("aliases", ())),
                   "notes": r.get("notes", "")} for r in ROLES],
    }
