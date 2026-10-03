"""scripts/vsphere-discover.py: analysis, rendering and the read-only contract.

The fixture mirrors the real lab as discovered on 2026-08-18 (4 x Supermicro
E300-9D, Xeon D-2146NT 8C/16T, 512 GB, local VMFS only, vDS-10G, Enterprise Plus,
ISO folder on esx01-local). No pyVmomi objects are involved: analyse() and
render_markdown() take plain dicts.
"""

from __future__ import annotations

import copy
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "vsphere-discover.py"

_spec = importlib.util.spec_from_file_location("vsphere_discover", SCRIPT)
vd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vd)

GIB = 1024**3

LAB_ISOS = [
    "VMware-VMvisor-Installer-8.0U3e-24677879.x86_64.iso",
    "kali-linux-2026.1-installer-everything-amd64.iso",
    "Parrot-security-7.3_amd64.iso",
    "en_sharepoint_server_2019_x64_dvd_68e34c9e.iso",
    "en-us_windows_11_consumer_editions_version_24h2_updated_feb_2026_x64_dvd_4e400c9e.iso",
    "en_office_professional_plus_2021_x86_x64_dvd_c6dd6dc6.iso",
    "mu_exchange_server_2016_x64_dvd_7047456.iso",
    "en-us_windows_11_consumer_editions_version_26h1_updated_july_2026_x64_dvd_f69a9a1e.iso",
    "enu_sql_server_2022_enterprise_edition_x64_dvd_aa36de9e.iso",
    "enu_sql_server_2025_enterprise_edition_x64_dvd_dd8b2bab.iso",
    "en-us_windows_server_2025_updated_july_2026_x64_dvd_4e6f5a42.iso",
    "en-us_windows_server_2022_updated_july_2026_x64_dvd_75aa9e18.iso",
    "VMware-VCSA-all-8.0.3-24322831.iso",
    "ubuntu-24.04.4-live-server-amd64.iso",
    "ubuntu-24.04.3-desktop-amd64.iso",
    "ubuntu-23.10.1-desktop-amd64.iso",
    "Fedora-Workstation-Live-43-1.6.x86_64.iso",
    "debian-13.6.0-amd64-DVD-1.iso",
    "Rocky-10.2-x86_64-dvd1.iso",
]


def _host(n: int) -> dict:
    name = f"esx0{n}.truenorth.lab"
    return {
        "name": name,
        "cluster": "CL-Lab",
        "mgmt_ip": f"192.168.1.{10 + n}",
        "version": "8.0.3",
        "build": "24677879",
        "vendor": "Supermicro",
        "model": "SYS-E300-9D-8CN8TP",
        "cpu_model": "Intel(R) Xeon(R) D-2146NT CPU @ 2.30GHz",
        "sockets": 1,
        "cores": 8,
        "threads": 16,
        "memory_bytes": 512 * GIB,
        "memory_used_bytes": 5 * GIB,
        "connection_state": "connected",
        "maintenance": False,
        "pnics": [{"device": "vmnic1", "speed_mb": 1000, "driver": "igbn", "mac": "x"},
                  {"device": "vmnic5", "speed_mb": 10000, "driver": "ixgben", "mac": "y"}],
        "vswitches": [{"name": "vSwitch0", "mtu": 1500, "uplinks": ["vmnic1"],
                       "portgroups": [{"name": "VM Network", "vlan": 0}, {"name": "Management Network", "vlan": 0}]}],
        "vmknics": [{"device": "vmk0", "ip": f"192.168.1.{10 + n}", "netmask": "255.255.255.0",
                     "portgroup": "Management Network", "mtu": 1500, "services": ["management"]},
                    {"device": "vmk1", "ip": f"10.0.20.{10 + n}", "netmask": "255.255.255.0", "portgroup": None,
                     "dvs_portgroup_key": "dvportgroup-20", "mtu": 9000, "services": ["vmotion"]}],
        "dns": {"hostname": name.split(".")[0], "domain": "truenorth.lab", "servers": ["192.168.1.1"]},
        "ntp_servers": ["pool.ntp.org"],
        "hbas": [{"device": "vmhba0", "model": "AHCI", "driver": "vmw_ahci", "type": "BlockHba"}],
        "vm_count": 0,
    }


@pytest.fixture
def lab() -> dict:
    hosts = [_host(n) for n in range(1, 5)]
    return {
        "collected_at": "2026-10-03T00:00:00+00:00",
        "vcenter": {"host": "192.168.1.10", "full_name": "VMware vCenter Server 8.0.3 build-24322831",
                    "version": "8.0.3", "build": "24322831", "api_version": "8.0.3.0"},
        "licenses": [
            {"name": "VMware vSphere 8 Enterprise Plus", "edition_key": "esx.enterprisePlus.cpuPackage",
             "product": "VMware ESX Server", "total": 8, "used": 4, "features": ["dvs", "drs", "vmotion"]},
            {"name": "VMware vCenter Server 8 Standard", "edition_key": "vc.standard.instance", "features": []},
        ],
        "license_assignments": [],
        "datacenters": [{"name": "DC-Lab"}],
        "clusters": [{"name": "CL-Lab", "ha_enabled": True, "drs_enabled": False, "drs_behavior": "",
                      "hosts": [h["name"] for h in hosts]}],
        "resource_pools": [{"name": "Resources", "owner": "CL-Lab", "parent": "CL-Lab"}],
        "vm_folders": ["DC-Lab/vm"],
        "hosts": hosts,
        "datastores": [{"name": f"esx0{n}-local", "type": "VMFS", "capacity_bytes": int(1660.25 * GIB),
                        "free_bytes": int(1658 * GIB), "accessible": True, "local": True,
                        "hosts": [f"esx0{n}.truenorth.lab"], "shared": False} for n in range(1, 5)],
        "dvswitches": [{"name": "vDS-10G", "version": "8.0.3", "mtu": 9000, "uplinks": ["Uplink 1"],
                        "hosts": [h["name"] for h in hosts], "portgroups": [
                            {"name": "dPG-vMotion", "uplink": False, "vlan_type": "vlan", "vlan_id": 20, "trunk_ranges": []},
                            {"name": "dPG-VM-40", "uplink": False, "vlan_type": "vlan", "vlan_id": 40, "trunk_ranges": []},
                            {"name": "dPG-TN-MGMT", "uplink": False, "vlan_type": "vlan", "vlan_id": 30, "trunk_ranges": []},
                            {"name": "vDS-10G-DVUplinks-25", "uplink": True, "vlan_type": "trunk", "vlan_id": None,
                             "trunk_ranges": [[0, 4094]]},
                        ]}],
        "vms": {"total": 2, "powered_on": 2, "per_host": {"esx01.truenorth.lab": 2},
                "vcenter_vm_host": "esx01.truenorth.lab"},
        "templates": [{"name": f"tmpl-ubuntu-{v}", "guest_os": "Ubuntu Linux (64-bit)", "host": "esx01.truenorth.lab"}
                      for v in ("2004", "2204", "2404")],
        "key_providers": {"status": "ok", "providers": []},
        "isos": [{"datastore": "esx01-local", "folder": "[esx01-local] ISO/", "file": f,
                  "path": f"[esx01-local] ISO/{f}", "size_bytes": 5 * GIB} for f in LAB_ISOS],
        "iso_search": {"hint": None, "datastores_searched": ["esx01-local"], "errors": {}},
        "content_libraries": {"status": "ok", "error": None, "libraries": []},
        "appliance": {},
    }


# ── storage ──────────────────────────────────────────────────────────────────

def test_local_only_storage_with_vds_recommends_spread(lab):
    a = vd.analyse(lab)
    assert a["storage"]["verdict"] == "local-only"
    assert a["env"]["VSPHERE_PLACEMENT"] == "spread"
    assert a["storage"]["per_host_datastore"]["esx03.truenorth.lab"] == "esx03-local"


def test_local_only_storage_with_vss_recommends_per_range_host(lab):
    lab["licenses"] = [{"name": "VMware vSphere 8 Standard", "edition_key": "esx.standard.cpuPackage",
                        "features": ["vmotion"]}]
    lab["dvswitches"] = []
    a = vd.analyse(lab)
    assert a["env"]["VSPHERE_RANGE_SWITCH_MODE"] == "vss"
    assert a["env"]["VSPHERE_PLACEMENT"] == "per-range-host"


def test_shared_storage_recommends_cluster(lab):
    lab["datastores"].append({"name": "nfs-shared", "type": "NFS", "capacity_bytes": 4 * 1024 * GIB,
                              "free_bytes": 3 * 1024 * GIB, "accessible": True, "local": None,
                              "hosts": [h["name"] for h in lab["hosts"]]})
    a = vd.analyse(lab)
    assert a["storage"]["verdict"] == "shared"
    assert a["env"]["VSPHERE_PLACEMENT"] == "cluster"


def test_partially_shared_storage_is_not_cluster(lab):
    lab["datastores"].append({"name": "pair", "type": "VMFS", "capacity_bytes": GIB, "free_bytes": GIB,
                              "accessible": True, "hosts": ["esx01.truenorth.lab", "esx02.truenorth.lab"]})
    a = vd.analyse(lab)
    assert a["storage"]["verdict"] == "partially-shared"
    assert a["env"]["VSPHERE_PLACEMENT"] == "spread"


# ── switch / vTPM ────────────────────────────────────────────────────────────

def test_enterprise_plus_licence_recommends_vds(lab):
    a = vd.analyse(lab)
    assert a["switch"]["has_vds"] is True
    assert a["env"]["VSPHERE_RANGE_SWITCH_MODE"] == "vds"


def test_standard_licence_recommends_vss(lab):
    lab["licenses"] = [{"name": "VMware vSphere 8 Standard", "edition_key": "esx.standard.cpuPackage",
                        "features": ["vmotion"]}]
    lab["dvswitches"] = []
    a = vd.analyse(lab)
    assert a["switch"]["has_vds"] is False
    assert a["env"]["VSPHERE_RANGE_SWITCH_MODE"] == "vss"


def test_vtpm_verdicts(lab):
    assert vd.analyse(lab)["vtpm"]["verdict"] == "none"
    lab["key_providers"] = {"status": "ok", "providers": [{"id": "nkp", "type": "nativeProvider"}]}
    assert vd.analyse(lab)["vtpm"]["verdict"] == "available"
    lab["key_providers"] = {"status": "unavailable", "error": "no cryptoManager", "providers": []}
    assert vd.analyse(lab)["vtpm"]["verdict"] == "unknown"


# ── VLANs ────────────────────────────────────────────────────────────────────

def test_used_vlans_skip_uplink_trunk_and_untagged(lab):
    used, _ = vd.used_vlans(lab)
    assert used == [20, 30, 40]


def test_reserved_range_block_used_when_clean(lab):
    a = vd.analyse(lab, range_vlans="100-199")
    assert a["vlan"]["source"] == "reserved"
    assert a["env"]["VSPHERE_VLAN_POOL"] == "100-199"


def test_reserved_block_with_clash_falls_back_to_free_search(lab):
    lab["dvswitches"][0]["portgroups"].append(
        {"name": "rogue", "uplink": False, "vlan_type": "vlan", "vlan_id": 150, "trunk_ranges": []})
    a = vd.analyse(lab, range_vlans="100-199")
    assert a["vlan"]["source"] == "free-search"
    assert a["vlan"]["reserved_clashes"] == [150]
    assert a["env"]["VSPHERE_VLAN_POOL"] == "900-999"
    assert "clashes" in vd.render_markdown(lab, a)


def test_free_vlan_block_avoids_used_ids():
    assert vd.suggest_vlan_block([]) == (900, 999)
    assert vd.suggest_vlan_block([950]) == (951, 1050)
    assert vd.suggest_vlan_block([950, 1000, 1049]) == (1050, 1149)
    assert vd.suggest_vlan_block(list(range(900, 4000))) is None


def test_narrow_dvpg_trunk_counts_as_used(lab):
    lab["dvswitches"][0]["portgroups"].append(
        {"name": "router-trunk", "uplink": False, "vlan_type": "trunk", "vlan_id": None,
         "trunk_ranges": [[900, 905]]})
    a = vd.analyse(lab)
    assert {900, 905} <= set(a["vlan"]["used"])
    assert a["env"]["VSPHERE_VLAN_POOL"] == "906-1005"


def test_bad_vlan_range_rejected():
    with pytest.raises(ValueError):
        vd.parse_vlan_range("199-100")


# ── ISO coverage ─────────────────────────────────────────────────────────────

def _files(a: dict, image: str) -> list[str]:
    return [Path(f.split("] ", 1)[-1]).name for f in next(r for r in a["iso"]["rows"] if r["image"] == image)["files"]]


def test_iso_matching_against_the_lab_folder(lab):
    a = vd.analyse(lab)
    assert _files(a, "ubuntu-lts") == ["ubuntu-24.04.4-live-server-amd64.iso"]  # desktop is not server
    assert _files(a, "srv2022") == ["en-us_windows_server_2022_updated_july_2026_x64_dvd_75aa9e18.iso"]
    assert _files(a, "srv2025") == ["en-us_windows_server_2025_updated_july_2026_x64_dvd_4e6f5a42.iso"]
    assert _files(a, "win11-24h2") == [LAB_ISOS[4]]
    assert _files(a, "win11-26h1") == [LAB_ISOS[7]]
    assert _files(a, "rocky") == ["Rocky-10.2-x86_64-dvd1.iso"]  # catalogue now targets Rocky 10
    assert _files(a, "rocky-9") == []
    assert _files(a, "debian-13") == ["debian-13.6.0-amd64-DVD-1.iso"]
    assert _files(a, "parrot") == ["Parrot-security-7.3_amd64.iso"]
    assert _files(a, "fedora") == ["Fedora-Workstation-Live-43-1.6.x86_64.iso"]
    assert _files(a, "kali") == ["kali-linux-2026.1-installer-everything-amd64.iso"]
    assert _files(a, "sql2022") == ["enu_sql_server_2022_enterprise_edition_x64_dvd_aa36de9e.iso"]
    assert _files(a, "sql2025") == ["enu_sql_server_2025_enterprise_edition_x64_dvd_dd8b2bab.iso"]
    assert _files(a, "exchange") == ["mu_exchange_server_2016_x64_dvd_7047456.iso"]
    assert _files(a, "sharepoint") == ["en_sharepoint_server_2019_x64_dvd_68e34c9e.iso"]
    assert _files(a, "office") == ["en_office_professional_plus_2021_x86_x64_dvd_c6dd6dc6.iso"]
    # Role media must never satisfy an OS row.
    assert _files(a, "srv2019") == [] and _files(a, "srv2016") == []
    assert a["iso"]["ubuntu_ok"] and a["iso"]["windows_server_ok"]
    assert a["iso"]["missing"] == ["srv2019", "srv2016", "win10-22h2", "win7-sp1", "pfsense", "securityonion"]
    # virtio-win is Proxmox only; Rocky 10 satisfies rocky; 2025 media is present alongside 2022.
    for present in ("virtio-win", "rocky", "rocky-9", "srv2022", "vmware-tools"):
        assert present not in a["iso"]["missing"]
    assert "Proxmox only" in next(r["label"] for r in a["iso"]["rows"] if r["image"] == "virtio-win")
    assert a["iso"]["unmatched_files"] == []


def test_classic_eval_iso_names():
    names = ["SERVER_EVAL_x64FRE_en-us.iso", "SERVER_2019_EVAL_x64FRE_en-us.iso", "Win10_Enterprise_Eval_x64.iso",
             "pfSense-CE-2.7.2-RELEASE-amd64.iso", "securityonion-2.4.10-20240220.iso", "virtio-win.iso",
             "Rocky-9-latest-x86_64-dvd.iso", "vyos-1.4-rolling.iso", "VMware-tools-windows-12.4.0.iso"]
    a = vd.match_isos([{"file": n, "path": n} for n in names])
    hit = {r["image"]: r["files"] for r in a["rows"]}
    assert hit["srv2022"] == ["SERVER_EVAL_x64FRE_en-us.iso"]
    assert hit["srv2019"] == ["SERVER_2019_EVAL_x64FRE_en-us.iso"]
    assert hit["win10-22h2"] == ["Win10_Enterprise_Eval_x64.iso"]
    assert hit["rocky"] == []
    for image in ("pfsense", "securityonion", "virtio-win", "rocky-9", "vyos", "vmware-tools"):
        assert hit[image], image


# ── capacity ─────────────────────────────────────────────────────────────────

def test_capacity_excludes_vcsa_host_and_is_cpu_bound(lab):
    a = vd.analyse(lab)
    c = a["capacity"]
    assert c["excluded_host"] == "esx01.truenorth.lab"
    assert c["range_hosts"] == ["esx02.truenorth.lab", "esx03.truenorth.lab", "esx04.truenorth.lab"]
    assert c["physical_cores"] == 32 and c["logical_cpus"] == 64
    # 3 hosts x 8 cores x 4 = 96 vCPU, minus 34 vCPU of management VMs.
    assert c["range_vcpu"] == 96 - 34
    medium = next(r for r in c["ranges"] if r["name"] == "medium-enterprise")
    assert medium["limit"] == "CPU"
    # spread (local storage + vDS) pools the three range hosts: 62 // 20 = 3.
    assert medium["concurrent"] == medium["pooled"] == 3
    assert medium["per_host"] == 2  # reported for reference
    assert medium["one_host_down"] == 1  # lose esx04 (26 vCPU): 36 // 20
    large = next(r for r in c["ranges"] if r["name"] == "large-enterprise")
    assert large["per_host"] == 0  # 142 vCPU never fits on one 32-vCPU host
    assert large["concurrent"] == 0  # nor in 62 pooled vCPU
    md = vd.render_markdown(lab, a)
    assert "excluded from range placement" in md
    assert "`spread` placement" in md


def test_per_range_host_capacity_is_packed(lab):
    lab["licenses"] = [{"name": "VMware vSphere 8 Standard", "edition_key": "esx.standard.cpuPackage"}]
    lab["dvswitches"] = []
    medium = next(r for r in vd.analyse(lab)["capacity"]["ranges"] if r["name"] == "medium-enterprise")
    assert medium["concurrent"] == medium["per_host"] == 2


def test_capacity_without_vcsa_vm_assumes_first_host(lab):
    lab["vms"]["vcenter_vm_host"] = None
    c = vd.analyse(lab)["capacity"]
    assert c["excluded_host"] == "esx01.truenorth.lab"
    assert "assumed" in c["excluded_reason"]


# ── readiness gate ───────────────────────────────────────────────────────────

def test_readiness_gate_marks_unknowns(lab):
    a = vd.analyse(lab, range_vlans="100-199")
    gate = {g["item"]: g["status"] for g in a["gate"]}
    assert len(gate) == 22
    assert gate["vCenter endpoint/version."] == "known"
    assert gate["All four ESXi versions."] == "known"
    assert gate["ESX-01 ISO datastore/path."] == "known"
    assert gate["VLAN availability."] == "known"
    assert gate["DHCP/IPAM plan."] == "unknown"
    assert gate["TrueNorth service account plan."] == "unknown"
    assert gate["Management/range isolation plan."] == "unknown"
    assert a["gate_passed"] is False


def test_gate_degrades_with_missing_data(lab):
    lab["hosts"] = lab["hosts"][:2]
    lab["isos"] = []
    lab["content_libraries"] = {"status": "unavailable", "error": "HTTP 401", "libraries": []}
    gate = {g["item"]: g["status"] for g in vd.analyse(lab)["gate"]}
    assert gate["All four ESXi versions."] == "partial"
    assert gate["ESX-01 ISO datastore/path."] == "unknown"
    assert gate["Ubuntu Server ISO availability."] == "unknown"
    assert gate["Content Library status."] == "unknown"
    assert gate["VLAN availability."] == "partial"  # free block found, trunking unconfirmed


def test_empty_inventory_does_not_crash():
    a = vd.analyse({})
    assert a["storage"]["verdict"] == "unknown"
    assert a["capacity"] == {"known": False}
    assert "readiness gate" in vd.render_markdown({}, a).lower()


# ── rendering / baseline ─────────────────────────────────────────────────────

def test_markdown_has_spec_tables(lab):
    md = vd.render_markdown(lab, vd.analyse(lab, range_vlans="100-199"))
    assert "| Host | CPU | Cores | RAM | Free RAM | NICs | Storage | ESXi |" in md
    assert "| Network / Port Group | VLAN | Purpose | Hosts | Routed? | Candidate Use |" in md
    assert "| Range Size | VMs | vCPU | RAM | Storage | Concurrent Ranges |" in md
    assert "VSPHERE_PLACEMENT=spread" in md
    assert "VSPHERE_RANGE_SWITCH_MODE=vds" in md
    assert "VSPHERE_VLAN_POOL=100-199" in md
    assert "dPG-TN-MGMT (vDS-10G)" in md


def test_baseline_diff(lab):
    old = copy.deepcopy(lab)
    lab["templates"].append({"name": "tmpl-debian-13", "guest_os": "Debian", "host": "esx02.truenorth.lab"})
    lab["isos"] = lab["isos"][1:]
    lab["hosts"][1]["build"] = "99999999"
    a = vd.analyse(lab, baseline={"inventory": old, "analysis": {}})
    d = a["diff"]
    assert d["templates"]["added"] == ["tmpl-debian-13"]
    assert d["isos"]["removed"] == [f"[esx01-local] ISO/{LAB_ISOS[0]}"]
    assert any("esx02.truenorth.lab: build" in x for x in d["changes"])
    assert "Changes since baseline" in vd.render_markdown(lab, a)


# ── read-only contract ───────────────────────────────────────────────────────

MUTATING = re.compile(
    r"\.(Destroy\w*|Create\w*|Reconfig\w*|PowerOn\w*|PowerOff\w*|Remove\w*|Set\w*|Move\w*|Rename\w*|Register\w*|"
    r"Unregister\w*|Clone\w*|Relocate\w*|Delete\w*|Update\w*|Upload\w*|Mark\w*|Add\w*|Apply\w*|Enter\w*|Exit\w*|"
    r"Reset\w*|Shutdown\w*|Reboot\w*|Revert\w*|Rescan\w*|Refresh\w*|Generate\w*|Install\w*|Assign\w*)\("
)


def test_source_has_no_mutating_vsphere_calls():
    src = SCRIPT.read_text()
    calls = {m.group(1) for m in MUTATING.finditer(src)}
    assert calls <= {"CreateContainerView"}, f"mutating-looking calls: {sorted(calls)}"
    tasks = set(re.findall(r"\b(\w+_Task)\b", src))
    assert tasks <= set(vd.ALLOWED_TASKS), f"tasks outside the allowlist: {sorted(tasks - set(vd.ALLOWED_TASKS))}"
    http_verbs = set(re.findall(r"client\.(\w+)\(", src))
    assert http_verbs <= {"get", "post"}
    assert re.findall(r"client\.post\(\"([^\"]+)\"", src) == ["/api/session"]


def test_task_guard_refuses_anything_off_the_allowlist():
    class Obj:
        def Destroy_Task(self):  # noqa: N802 - mirrors the pyVmomi name
            raise AssertionError("must not be called")

    with pytest.raises(PermissionError):
        vd._readonly_task(Obj(), "Destroy_Task")


def test_password_is_never_printed_or_stored():
    src = SCRIPT.read_text()
    assert not re.search(r"print\([^)]*password", src)
    assert not re.search(r"inv\[[^\]]*\]\s*=\s*password|\"password\":", src)
