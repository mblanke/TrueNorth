"""TN-DEPOT01's third NIC on the range uplink network at the work site (#214).

Owner decision 2026-10-20: the depot gets a NIC at 10.30.32.10 on the range uplink network,
so range firewalls keep their TN_DEPOT rule and never get a path to the management network.
Contracts kept here, in CI, where there is no vCenter, depot or nft:

* One set of values: the work-site kit (inventory), the scripts that deploy and check the
  depot (deploy-mgmt-vm.sh, build-phase.sh, create-portgroups.sh), the installer
  (tn_depot_*, the range uplink pool/gateway, TN_DEPOT_PORTS in .env.production) and the
  Packer example (depot_url WITH :8081/repository, on the BUILD address).
* The depot's range NIC firewall (tn_depot depot-range-fw.nft.j2), rendered with the kit's
  values: only TCP 8081/3142 from the uplink subnet to 10.30.32.10 comes in on that NIC;
  nothing else in, nothing new out, nothing forwarded. It is loaded before the address.
* No forwarding: sysctl and dockerd settings, and the role reads them back from the kernel.
* build-phase.sh depot adds/validates the third NIC after a y/N per vCenter change (a stub
  govc records what it was asked to do).

Rendered/static evidence and a stubbed govc only. That ranges reach the depot, and nothing
else, is checked on site (install/lab/README.md, "Ranges reach the depot").
"""

from __future__ import annotations

import ast
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "scripts/lab"
ROLE = ROOT / "install/lab/roles/tn_depot"
KIT = yaml.safe_load((ROOT / "install/lab/work-site/inventory/group_vars/all/main.yml").read_text())
HOSTS = yaml.safe_load((ROOT / "install/lab/work-site/inventory/hosts.yml").read_text())
INSTALLER = yaml.safe_load((ROOT / "install/inventory/group_vars/all/main.yml").read_text())
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())

RANGE_IP = "10.30.32.10"
RANGE_NET = ipaddress.ip_network("10.30.32.0/24")
RANGE_IF = "ens256"  # any name: the template only matches on it


def _sh_default(path: Path, pattern: str) -> str:
    m = re.search(pattern, path.read_text(), re.M)
    assert m, f"{pattern!r} not found in {path.name}"
    return m.group(1)


def _env() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    env.filters["to_nice_json"] = lambda v: json.dumps(v, indent=4, sort_keys=True)
    return env


def _role_vars(**extra) -> dict:
    """The role's defaults with the work-site kit on top, defaults' templates resolved."""
    v = {**DEFAULTS, **KIT, "ansible_managed": "Ansible managed", **extra}
    ports = _env().from_string(DEFAULTS["tn_depot_range_ports"]).render(**v)
    v["tn_depot_range_ports"] = ast.literal_eval(ports)
    return v


def _render(name: str, **extra) -> str:
    return _env().from_string((ROLE / "templates" / name).read_text()).render(**_role_vars(**extra))


def _task_names(path: Path) -> list[str]:
    return [t.get("name", "") for t in yaml.safe_load(path.read_text())]


# ── one set of values ─────────────────────────────────────────────────
def test_kit_gives_the_depot_three_addresses_on_three_networks():
    assert HOSTS["all"]["children"]["depot"]["hosts"]["tn-depot01"]["ansible_host"] == KIT["tn_depot_ip"] == "10.30.30.30"
    assert KIT["tn_depot_build_ip"] == "10.30.31.30"
    assert KIT["tn_depot_range_ip"] == RANGE_IP and KIT["tn_depot_range_prefix"] == 24
    assert ipaddress.ip_network(KIT["tn_depot_range_subnet"]) == RANGE_NET
    nets = {ipaddress.ip_interface(f"{ip}/24").network for ip in (KIT["tn_depot_ip"], KIT["tn_depot_build_ip"], RANGE_IP)}
    assert len(nets) == 3  # MGMT, BUILD, range uplink: never two NICs on one network
    assert (KIT["tn_depot_nexus_port"], KIT["tn_depot_acng_port"]) == (8081, 3142)
    assert _role_vars()["tn_depot_range_ports"] == [8081, 3142]
    assert DEFAULTS["tn_depot_range_ip"] == ""  # off unless an inventory sets it (earlier design)


def test_the_depot_sits_on_the_installers_range_uplink_network_outside_the_pool():
    lo, hi = (ipaddress.ip_address(a) for a in INSTALLER["tn_vsphere_range_uplink_pool"].split("-"))
    gw = ipaddress.ip_address(str(INSTALLER["tn_vsphere_range_uplink_gateway"]))
    uplink = ipaddress.ip_interface(f"{gw}/{INSTALLER['tn_vsphere_range_uplink_prefix']}").network
    depot = ipaddress.ip_address(RANGE_IP)
    assert uplink == RANGE_NET and lo in uplink and hi in uplink
    assert depot in uplink and depot != gw and not (lo <= depot <= hi)


def test_every_script_and_the_kit_name_the_same_range_port_group():
    pg = KIT["tn_depot_range_portgroup"]
    assert pg == "dPG-TN-SVC" and pg not in ("dPG-TN-MGMT", "dPG-TN-BUILD")
    assert pg != INSTALLER["tn_vsphere_mgmt_network"]
    assert _sh_default(LAB / "build-phase.sh", r'^RANGE_UPLINK_PG="\$\{TN_RANGE_UPLINK_PG:-([^}]+)\}"') == pg
    assert _sh_default(LAB / "deploy-mgmt-vm.sh", r'^RANGE_PG="\$\{TN_RANGE_UPLINK_PG:-([^}]+)\}"') == pg
    assert re.search(r'work\) WANT=\("' + re.escape(pg) + r':32"\)', (LAB / "create-portgroups.sh").read_text())
    # The installer's uplink network stays empty (ranges isolated) until the operator's last
    # step, and when set it must be this port group, never MGMT or BUILD.
    assert INSTALLER["tn_vsphere_range_uplink_network"] in ("", pg)


def test_scripts_deploy_and_check_the_range_nic_at_the_kits_address():
    deploy = (LAB / "deploy-mgmt-vm.sh").read_text()
    work = deploy[deploy.index("work:TN-DEPOT01)"):deploy.index("work:TN-BUILD01)")]
    assert f"RANGE_IP={RANGE_IP}" in work and "BUILD_NIC_ALWAYS=1" in work
    assert "IP=10.30.30.30" in work and "UPLINK_IP=10.30.31.30" in work
    # cloud-init leaves the range NIC unaddressed: the firewall goes on first (depot.yml).
    meta = deploy[deploy.index("render_metadata() {"):deploy.index("render_userdata() {")]
    assert "RANGE_IP" not in meta and "nic2" not in meta
    phase = (LAB / "build-phase.sh").read_text()
    assert f'DEPOT_RANGE_IP="{RANGE_IP}"' in phase
    assert '"tn_depot_range_mac=$DEPOT_RANGE_MAC"' in phase


def test_installer_points_ranges_at_the_range_nic_in_the_format_the_worker_expects():
    url = INSTALLER["tn_depot_url"]
    parsed = urlparse(url)
    # Scheme + IP only: the worker appends ":8081/repository/chocolatey/" itself, and the
    # firewalls' TN_DEPOT alias is the host (VyOS needs an IP address).
    assert (parsed.scheme, parsed.hostname, parsed.port, parsed.path) == ("http", RANGE_IP, None, "")
    ipaddress.ip_address(parsed.hostname)
    worker = (ROOT / "control-plane/worker/worker/provisioners/vsphere_api.py").read_text()
    assert 'f"{TN_DEPOT_URL}:8081/repository/chocolatey/"' in worker  # the derived default
    assert INSTALLER["tn_depot_choco_feed"] == f"{url}:8081/repository/chocolatey/"  # = derived default
    assert INSTALLER["tn_depot_apt_proxy"] == f"http://{RANGE_IP}:3142"
    assert INSTALLER["tn_depot_ports"] == "8081,3142"
    env_j2 = (ROOT / "install/roles/tn_config/templates/env.production.j2").read_text()
    for line in ("TN_DEPOT_URL={{ tn_depot_url }}", "TN_DEPOT_CHOCO_FEED={{ tn_depot_choco_feed }}",
                 "TN_DEPOT_APT_PROXY={{ tn_depot_apt_proxy }}", "TN_DEPOT_PORTS={{ tn_depot_ports"):
        assert line in env_j2
    assert "TN_DEPOT_PORTS: ${TN_DEPOT_PORTS:-8081,3142}" in (ROOT / "infra/platform/docker/compose.prod.yml").read_text()


def test_packer_example_uses_the_build_address_with_port_and_repository():
    text = (ROOT / "infra/vsphere/packer/lab.auto.pkrvars.hcl.example").read_text()
    depot = re.findall(r'^#?\s*depot_url\s*=\s*"([^"]*)"', text, re.M)
    proxy = re.findall(r'^#?\s*apt_proxy\s*=\s*"([^"]*)"', text, re.M)
    pk = (LAB / "pkrvars-work.py").read_text()
    assert depot == ["http://10.30.31.30:8081/repository"]
    assert f'DEFAULT_DEPOT_URL = "{depot[0]}"' in pk
    assert proxy == ["http://10.30.31.30:3142"] and f'DEFAULT_APT_PROXY = "{proxy[0]}"' in pk
    assert "TN-BUILD01" not in text  # the old-design build host is gone from the example


# ── the range NIC firewall ────────────────────────────────────────────
def _chains(nft: str) -> dict[str, list[str]]:
    body = nft[nft.index("table inet tn_depot_range {"):]
    chains: dict[str, list[str]] = {}
    for name, block in re.findall(r"chain (\w+) \{(.*?)\n\t\}", body, re.S):
        chains[name] = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
    return chains


def test_firewall_admits_only_the_depot_ports_from_the_uplink_subnet():
    nft = _render("depot-range-fw.nft.j2", tn_depot_range_if=RANGE_IF)
    # Replaces only its own table: Docker's rules survive a reload.
    assert "flush ruleset" not in nft
    assert nft.index("delete table inet tn_depot_range") < nft.index("table inet tn_depot_range {")
    chains = _chains(nft)
    assert set(chains) == {"input", "forward", "output"}
    assert chains["input"] == [
        "type filter hook input priority filter - 10; policy accept;",
        f'iifname != "{RANGE_IF}" accept',
        "ct state established,related accept",
        f"ip saddr 10.30.32.0/24 ip daddr {RANGE_IP} tcp dport {{ 8081, 3142 }} ct state new accept",
        "counter drop",
    ]
    assert chains["forward"] == [
        "type filter hook forward priority filter - 10; policy accept;",
        f'iifname "{RANGE_IF}" counter drop',
        f'oifname "{RANGE_IF}" counter drop',
    ]
    assert chains["output"] == [
        "type filter hook output priority filter - 10; policy accept;",
        f'oifname != "{RANGE_IF}" accept',
        "ct state established,related accept",
        "counter drop",
    ]
    # Exactly one rule opens anything, and it never names SSH, the Docker connectors or
    # another depot address.
    opens = [ln for c in chains.values() for ln in c if ln.endswith("accept") and "dport" in ln]
    assert len(opens) == 1
    for closed in ("22", "8082", "8083", "10.30.30.30", "10.30.31.30"):
        assert not re.search(rf"\b{re.escape(closed)}\b", opens[0])


def test_firewall_syntax_with_nft_when_available(tmp_path):
    nft_bin = shutil.which("nft")
    if nft_bin is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("nft -c needs nft and root (Linux)")
    f = tmp_path / "range.nft"
    f.write_text(_render("depot-range-fw.nft.j2", tn_depot_range_if=RANGE_IF))
    r = subprocess.run([nft_bin, "-c", "-f", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_firewall_loads_before_the_network_and_is_never_unloaded():
    unit = _render("tn-depot-range-fw.service.j2")
    assert "Before=network-pre.target" in unit and "Wants=network-pre.target" in unit
    assert "DefaultDependencies=no" in unit and "WantedBy=sysinit.target" in unit
    assert f"ExecStart=/usr/sbin/nft -f {DEFAULTS['tn_depot_range_nft']}" in unit
    assert not re.search(r"^ExecStop", unit, re.M)


def test_the_range_nic_gets_its_address_only_after_its_firewall_and_no_route():
    names = _task_names(ROLE / "tasks/range_nic.yml")
    fw = next(i for i, n in enumerate(names) if n.startswith("Range NIC — firewall loaded"))
    live = next(i for i, n in enumerate(names) if n.startswith("Range NIC — the live rules"))
    addr = names.index("Range NIC — netplan, the address with no gateway")
    apply = names.index("Range NIC — netplan apply")
    assert fw < live < addr < apply
    tasks = yaml.safe_load((ROLE / "tasks/range_nic.yml").read_text())
    rules = next(t for t in tasks if t.get("name", "").startswith("Range NIC — firewall rules, "))
    assert rules["ansible.builtin.template"]["validate"] == "nft -c -f %s"

    plan = yaml.safe_load(_render("netplan-range.yaml.j2", tn_depot_range_hwaddr="00:50:56:aa:bb:cc"))
    (nic,) = plan["network"]["ethernets"].values()
    assert nic["match"] == {"macaddress": "00:50:56:aa:bb:cc"}
    assert nic["addresses"] == [f"{RANGE_IP}/24"]
    assert nic["dhcp4"] is False and nic["dhcp6"] is False and nic["accept-ra"] is False
    assert not {"routes", "gateway4", "nameservers"} & set(nic)  # on-link only

    main = _task_names(ROLE / "tasks/main.yml")
    assert main.index("Depot — no routing between its NICs (dockerd, sysctl)") \
        < main.index("Depot — range uplink NIC (firewall, then address)") < main.index("Depot — Nexus container")


# ── no forwarding ─────────────────────────────────────────────────────
def test_forwarding_is_off_and_dockerd_cannot_turn_it_on():
    sysctl = dict(
        (k.strip(), v.strip())
        for k, v in (ln.split("=", 1) for ln in _render("sysctl-depot.conf.j2").splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#"))
    )
    for key in ("net.ipv4.ip_forward", "net.ipv4.conf.all.forwarding", "net.ipv4.conf.default.forwarding",
                "net.ipv6.conf.all.forwarding", "net.ipv6.conf.default.forwarding"):
        assert sysctl[key] == "0", key
    assert sysctl["net.ipv4.conf.all.arp_ignore"] == "1"
    assert json.loads(_render("docker-daemon.json.j2")) == {"ip-forward": False}

    # Nexus needs no forwarding: host networking, no published ports (Docker DNAT).
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    nexus = next(t for t in tasks if t.get("name") == "Depot — Nexus container")["community.docker.docker_container"]
    assert nexus["network_mode"] == "host" and "published_ports" not in nexus

    # The sysctl file is applied on every run (a first-run dockerd has already turned
    # forwarding on), and the kernel is read back twice: after the settings, and after
    # dockerd and Nexus are up.
    fwd = yaml.safe_load((ROLE / "tasks/forwarding.yml").read_text())
    apply = next(t for t in fwd if t["name"].startswith("Forwarding — apply the sysctl file"))
    assert "when" not in apply
    verify = yaml.safe_load((ROLE / "tasks/verify_forwarding.yml").read_text())
    read = verify[0]["ansible.builtin.command"]
    assert all(k in read for k in ("net.ipv4.ip_forward", "net.ipv4.conf.all.forwarding", "net.ipv6.conf.all.forwarding"))
    assert verify[1]["ansible.builtin.assert"]["that"] == "tn_depot_forwarding.stdout_lines | map('trim') | unique | list == ['0']"
    includes = [t.get("ansible.builtin.include_tasks") for t in tasks]
    assert includes.count("verify_forwarding.yml") == 1 and includes.index("verify_forwarding.yml") > [
        t.get("name") for t in tasks].index("Depot — wait for Nexus (first start takes a few minutes)")


# ── build-phase.sh depot: adds/validates the third NIC, y/N per change ───
GOVC_STUB = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ["STUB_STATE"]
args = [a for a in sys.argv[1:]]
with open(os.path.join(state, "calls"), "a") as f:
    f.write(" ".join(args) + "\n")
def load():
    with open(os.path.join(state, "nics.json")) as f:
        return json.load(f)
def save(n):
    with open(os.path.join(state, "nics.json"), "w") as f:
        json.dump(n, f)
pgs = json.loads(os.environ["STUB_PGS"])
cmd = args[0]
def opt(name):
    return args[args.index(name) + 1] if name in args else None
if cmd == "about":
    print('{"About":{"ApiType":"VirtualCenter"}}')
elif cmd == "find":
    kind, name = opt("-type"), opt("-name")
    if kind == "c": print("/DC-Lab/host/CL-Lab")
    elif kind == "m" and name in ("ubuntu-lts", "TN-DEPOT01"): print(f"/DC-Lab/vm/{name}")
    elif kind == "g" and name in pgs: print(f"/DC-Lab/network/{name}")
elif cmd == "object.collect":
    path, prop = args[-2], args[-1]
    if prop == "key": print(pgs[path.rsplit("/", 1)[1]])
    elif prop == "runtime.powerState": print("poweredOn")
elif cmd == "device.ls":
    for n in load(): print(f"{n['dev']}  VirtualVmxnet3  {n['key']}")
elif cmd == "device.info":
    n = next(n for n in load() if n["dev"] == args[-1])
    print(json.dumps({"Devices": [{"Name": n["dev"], "MacAddress": n["mac"],
        "Backing": {"Port": {"PortgroupKey": n["key"]}},
        "Connectable": {"Connected": n["connected"], "StartConnected": n["start"]}}]}))
elif cmd == "vm.network.add":
    nics = load()
    nics.append({"dev": f"ethernet-{len(nics)}", "key": pgs[opt("-net")], "mac": "00:50:56:aa:bb:cc",
                 "connected": True, "start": True})
    save(nics)
elif cmd == "device.connect":
    nics = load()
    for n in nics:
        if n["dev"] == args[-1]: n["connected"] = n["start"] = True
    save(nics)
'''

PGS = {"dPG-TN-MGMT": "dvportgroup-30", "dPG-TN-BUILD": "dvportgroup-31", "dPG-TN-SVC": "dvportgroup-32"}
TWO_NICS = [
    {"dev": "ethernet-0", "key": "dvportgroup-30", "mac": "00:50:56:00:00:30", "connected": True, "start": True},
    {"dev": "ethernet-1", "key": "dvportgroup-31", "mac": "00:50:56:00:00:31", "connected": True, "start": True},
]


@pytest.fixture
def depot_phase(tmp_path):
    if os.name != "posix" or shutil.which("jq") is None:
        pytest.skip("bash and jq")
    bindir, state = tmp_path / "bin", tmp_path / "state"
    bindir.mkdir()
    state.mkdir()
    (bindir / "govc").write_text(GOVC_STUB)
    for name, body in {"curl": "#!/bin/sh\nexit 7\n", "ansible-playbook": "#!/bin/sh\necho \"$@\" >> \"$STUB_STATE/ansible\"\n",
                       "ansible-galaxy": "#!/bin/sh\nexit 0\n"}.items():
        (bindir / name).write_text(body)
    for f in bindir.iterdir():
        f.chmod(0o755)
    for tool in ("jq",):
        (bindir / tool).symlink_to(shutil.which(tool))
    (bindir / "python3").symlink_to(sys.executable)

    def run(*args, nics=TWO_NICS, pgs=PGS):
        (state / "nics.json").write_text(json.dumps(nics))
        (state / "calls").write_text("")
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "STUB_STATE": str(state),
               "STUB_PGS": json.dumps(pgs), "GOVC_USERNAME": "u", "GOVC_PASSWORD": "p",
               "GOVC_URL": "https://vcsa.test/sdk", "TN_ALLOW_NON_ROOT": "1"}
        # start_new_session: no controlling terminal, so confirm() (reads /dev/tty) answers no.
        r = subprocess.run(["bash", str(LAB / "build-phase.sh"), "depot", *args], capture_output=True,
                           text=True, env=env, stdin=subprocess.DEVNULL, start_new_session=True, timeout=120)
        calls = (state / "calls").read_text().splitlines()
        return r, calls, json.loads((state / "nics.json").read_text())

    return run


def _changes(calls):
    return [c for c in calls if c.startswith(("vm.network.add", "device.connect", "device.disconnect"))]


def test_depot_phase_asks_before_adding_the_range_nic(depot_phase):
    r, calls, nics = depot_phase()
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "plan  govc vm.network.add -vm TN-DEPOT01 -net dPG-TN-SVC" in out
    assert "range NIC not added" in out and not _changes(calls) and len(nics) == 2


def test_depot_phase_adds_the_range_nic_on_yes_and_hands_its_mac_to_depot_yml(depot_phase):
    r, calls, nics = depot_phase("--yes")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert _changes(calls) == ["vm.network.add -vm /DC-Lab/vm/TN-DEPOT01 -net dPG-TN-SVC -net.adapter vmxnet3"]
    assert [n["key"] for n in nics] == ["dvportgroup-30", "dvportgroup-31", "dvportgroup-32"]
    assert "MAC 00:50:56:aa:bb:cc" in out and "10.30.32.10/24" in out


def test_depot_phase_connects_a_disconnected_range_nic_and_leaves_a_good_one_alone(depot_phase):
    off = [*TWO_NICS, {"dev": "ethernet-2", "key": "dvportgroup-32", "mac": "00:50:56:00:00:32",
                       "connected": False, "start": False}]
    r, calls, _ = depot_phase("--yes", nics=off)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _changes(calls) == ["device.connect -vm /DC-Lab/vm/TN-DEPOT01 ethernet-2"]
    on = [*TWO_NICS, {**off[2], "connected": True, "start": True}]
    r, calls, _ = depot_phase("--yes", nics=on)
    assert r.returncode == 0 and not _changes(calls)
    assert "ok    ethernet-2 is on dPG-TN-SVC" in r.stdout and "MAC 00:50:56:00:00:32" in r.stdout


def test_depot_phase_without_the_port_group_changes_nothing_and_says_what_to_do(depot_phase):
    r, calls, _ = depot_phase("--yes", pgs={k: v for k, v in PGS.items() if k != "dPG-TN-SVC"})
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "TODO(site)" in out and "create-portgroups.sh --site work" in out and not _changes(calls)


@pytest.mark.parametrize("pg", ["dPG-TN-MGMT", "dPG-TN-BUILD"])
def test_depot_phase_refuses_management_or_build_as_the_range_network(depot_phase, pg):
    r, calls, _ = depot_phase("--yes", "--range-pg", pg)
    assert r.returncode != 0 and "must never share" in r.stderr and not _changes(calls)


def test_depot_uplink_refuses_the_work_site_depot():
    text = (LAB / "depot-uplink.sh").read_text()
    guard = text.index("this is the work-site depot")
    assert guard < text.index('case "$CMD" in')  # before any connect/disconnect
