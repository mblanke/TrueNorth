"""TN-DEPOT01's third NIC on the range uplink network at the work site (#214).

Owner decision 2026-10-20: the depot gets a NIC at 10.30.32.10 on the range uplink network,
so range firewalls keep their TN_DEPOT rule and never get a path to the management network.
Contracts kept here, in CI, where there is no vCenter, depot or nft:

* One set of values: the work-site kit (inventory), the scripts that deploy and check the
  depot (deploy-mgmt-vm.sh, build-phase.sh, create-portgroups.sh), the installer
  (tn_depot_*, the range uplink pool/gateway, TN_DEPOT_PORTS in .env.production) and the
  Packer example (depot_url WITH :8081/repository, on the BUILD address).
* The depot's range NIC firewall (tn_depot depot-range-fw.nft.j2), rendered with the kit's
  values: only TCP 8081/3142 from the uplink subnet to 10.30.32.10 comes in; matched both
  by interface (tnrange0, renamed by MAC) and by source subnet on any interface; nothing
  new out, nothing forwarded; nothing at all while the caches are online. Loaded before
  the address; the link comes up only behind it; a guard timer downs it if the table goes.
* No forwarding: sysctl and dockerd settings, and the role reads them back from the kernel.
* Going online is refused while range VMs exist; apt-cacher-ng is not an open proxy
  (ForceManaged, port 80) and its admin pages need AdminAuth; Nexus anonymous reads only
  the Chocolatey feed; Packer reads installers as tn-build-read (secret never printed).
* build-phase.sh depot adds/validates the third NIC after a y/N per vCenter change (a stub
  govc records what it was asked to do).
* A network-namespace test loads the rendered table into a real kernel and probes it; it
  needs Linux, root, nft and ip, so it is skipped in CI (no such lane) and on the Mac.

Rendered/static evidence, stubs and (where root) a netns probe. That ranges reach the depot,
and nothing else, is checked on site (install/lab/README.md, "Ranges reach the depot").
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
RANGE_IF = "tnrange0"  # the fixed name netplan gives the range NIC (matched by MAC)
MAC = "00:50:56:aa:bb:cc"


def _sh_default(path: Path, pattern: str) -> str:
    m = re.search(pattern, path.read_text(), re.M)
    assert m, f"{pattern!r} not found in {path.name}"
    return m.group(1)


def _env() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    env.filters["to_nice_json"] = lambda v: json.dumps(v, indent=4, sort_keys=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
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


PORTS_RULE = f"ip daddr {RANGE_IP} tcp dport {{ 8081, 3142 }}"


def test_firewall_admits_only_the_depot_ports_from_the_uplink_subnet():
    assert DEFAULTS["tn_depot_range_if"] == RANGE_IF
    nft = _render("depot-range-fw.nft.j2")
    # Replaces only its own table: Docker's rules survive a reload.
    assert "flush ruleset" not in nft
    assert nft.index("delete table inet tn_depot_range") < nft.index("table inet tn_depot_range {")
    chains = _chains(nft)
    assert set(chains) == {"from_range", "input", "forward", "output"}
    # By source subnet, on ANY interface (a renamed or extra NIC fails closed).
    assert chains["from_range"] == [f"{PORTS_RULE} return", "counter drop"]
    assert chains["input"] == [
        "type filter hook input priority filter - 10; policy accept;",
        "ip saddr 10.30.32.0/24 jump from_range",
        f'iifname != "{RANGE_IF}" accept',
        "ct state established,related accept",
        f"ip saddr 10.30.32.0/24 {PORTS_RULE} ct state new accept",
        "counter drop",
    ]
    assert chains["forward"] == [
        "type filter hook forward priority filter - 10; policy accept;",
        f'iifname "{RANGE_IF}" counter drop',
        f'oifname "{RANGE_IF}" counter drop',
        "ip saddr 10.30.32.0/24 counter drop",
        "ip daddr 10.30.32.0/24 counter drop",
    ]
    assert chains["output"] == [
        "type filter hook output priority filter - 10; policy accept;",
        f"ip daddr 10.30.32.0/24 ip daddr != {RANGE_IP} ct state new counter drop",
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


def test_firewall_while_online_lets_nothing_in_from_the_range_network():
    chains = _chains(_render("depot-range-fw.nft.j2", tn_depot_upstream_online=True))
    assert chains["from_range"] == ["counter drop"]
    assert not any("dport" in ln or ln.endswith("new accept") for ln in chains["input"])
    assert chains["input"][-1] == "counter drop"


@pytest.mark.parametrize("online", [False, True])
def test_firewall_syntax_with_nft_when_available(tmp_path, online):
    nft_bin = shutil.which("nft")
    if nft_bin is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("nft -c needs nft and root (Linux); no CI lane has both")
    f = tmp_path / "range.nft"
    f.write_text(_render("depot-range-fw.nft.j2", tn_depot_upstream_online=online))
    r = subprocess.run([nft_bin, "-c", "-f", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# A real kernel: the rendered table in a network namespace, probed from a "range WAN"
# namespace over a veth pair. Needs Linux, root, nft, ip and python3 in the namespaces.
# CI has no lane with root and nft, so this is skipped there (and on the Mac); run it as
# root on a Linux box or in a privileged container (install/lab/README.md).
NETNS_PROBE = r"""
set -u
D=tnd$$; R=tnr$$
PIDS=""
cleanup() { kill $PIDS 2>/dev/null; ip netns del "$D" 2>/dev/null; ip netns del "$R" 2>/dev/null; }
trap cleanup EXIT
ip netns add "$D"; ip netns add "$R"
ip link add tnrange0 netns "$D" type veth peer name wan0 netns "$R"
ip -n "$D" link set lo up; ip -n "$R" link set lo up
ip -n "$D" addr add 10.30.32.10/24 dev tnrange0; ip -n "$D" link set tnrange0 up
ip -n "$D" link add mgmt0 type dummy; ip -n "$D" addr add 10.30.30.30/24 dev mgmt0; ip -n "$D" link set mgmt0 up
ip -n "$R" addr add 10.30.32.101/24 dev wan0; ip -n "$R" link set wan0 up
ip -n "$R" route add 10.30.30.30/32 via 10.30.32.10
ip netns exec "$D" nft -f "$NFT_FILE" || exit 3
for p in 8081 3142 22 8082 8083; do
  ip netns exec "$D" python3 -c "import socket,sys;s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',int(sys.argv[1])));s.listen(8);import time;time.sleep(30)" "$p" &
  PIDS="$PIDS $!"
done
sleep 1
probe() { ip netns exec "$R" python3 -c "import socket,sys;s=socket.socket();s.settimeout(1.5);
try: s.connect((sys.argv[1],int(sys.argv[2]))); print('open')
except Exception: print('closed')" "$1" "$2"; }
for t in 10.30.32.10:8081 10.30.32.10:3142 10.30.32.10:22 10.30.32.10:8082 10.30.32.10:8083 10.30.30.30:8081; do
  echo "$t $(probe "${t%:*}" "${t#*:}")"
done
"""


@pytest.mark.parametrize("online", [False, True])
def test_netns_range_side_reaches_only_the_depot_ports(tmp_path, online):
    if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0 \
            or not all(shutil.which(t) for t in ("nft", "ip", "python3")):
        pytest.skip("netns probe needs Linux, root, nft, ip and python3; CI has no such lane")
    f = tmp_path / "range.nft"
    f.write_text(_render("depot-range-fw.nft.j2", tn_depot_upstream_online=online))
    r = subprocess.run(["bash", "-c", NETNS_PROBE], capture_output=True, text=True, timeout=120,
                       env={**os.environ, "NFT_FILE": str(f)})
    assert r.returncode == 0, r.stderr
    got = dict(line.split() for line in r.stdout.splitlines() if line.count(" ") == 1)
    want_open = set() if online else {"10.30.32.10:8081", "10.30.32.10:3142"}
    assert got == {t: ("open" if t in want_open else "closed") for t in (
        "10.30.32.10:8081", "10.30.32.10:3142", "10.30.32.10:22", "10.30.32.10:8082", "10.30.32.10:8083",
        "10.30.30.30:8081")}, r.stdout


def test_firewall_loads_before_the_network_and_is_never_unloaded():
    unit = _render("tn-depot-range-fw.service.j2")
    assert "Before=network-pre.target" in unit and "Wants=network-pre.target" in unit
    assert "DefaultDependencies=no" in unit and "WantedBy=sysinit.target" in unit
    assert f"ExecStart=/usr/sbin/nft -f {DEFAULTS['tn_depot_range_nft']}" in unit
    assert not re.search(r"^ExecStop", unit, re.M)


def test_the_range_nic_gets_its_address_only_after_its_firewall_and_no_route():
    names = _task_names(ROLE / "tasks/range_nic.yml")
    fw = names.index("Range NIC — firewall (rules, unit, live check)")
    guard = names.index("Range NIC — guard script")
    addr = names.index("Range NIC — netplan, the address with no gateway")
    apply = names.index("Range NIC — netplan apply")
    up = names.index("Range NIC — link up behind the firewall, guard timer on")
    assert fw < guard < addr < apply < up < names.index("Range NIC — the guard passes now")
    fw_tasks = yaml.safe_load((ROLE / "tasks/range_fw.yml").read_text())
    rules = next(t for t in fw_tasks if t.get("name", "").startswith("Range firewall — rules, "))
    assert rules["ansible.builtin.template"]["validate"] == "nft -c -f %s"

    plan = yaml.safe_load(_render("netplan-range.yaml.j2", tn_depot_range_hwaddr=MAC.upper()))
    nic = plan["network"]["ethernets"][RANGE_IF]
    assert nic["match"] == {"macaddress": MAC} and nic["set-name"] == RANGE_IF
    assert nic["activation-mode"] == "manual"  # networkd never brings it up; the link unit does
    assert nic["addresses"] == [f"{RANGE_IP}/24"]
    assert nic["dhcp4"] is False and nic["dhcp6"] is False and nic["accept-ra"] is False
    assert not {"routes", "gateway4", "nameservers"} & set(nic)  # on-link only


def test_the_nic_is_taken_by_mac_only_never_guessed():
    text = (ROLE / "tasks/range_nic.yml").read_text()
    assert "unaddressed" not in text and "vmxnet3" not in text
    tasks = yaml.safe_load(text)
    check = next(t for t in tasks if t.get("name") == "Range NIC — the MAC names exactly this NIC")
    that = check["ansible.builtin.assert"]["that"]
    assert "tn_depot_range_mac | length > 0 and tn_depot_range_by_mac | length > 0" in that
    assert "tn_depot_range_mac | length == 0 and tn_depot_range_renamed" in that  # re-run: renamed by MAC before


def test_the_link_comes_up_only_behind_the_firewall_and_goes_down_without_it():
    link = _render("tn-depot-range-link.service.j2")
    assert "Requires=tn-depot-range-fw.service" in link and "After=tn-depot-range-fw.service" in link
    assert f"ExecStart={DEFAULTS['tn_depot_range_guard']} up" in link
    timer = _render("tn-depot-range-guard.timer.j2")
    assert "OnUnitActiveSec=60s" in timer and "WantedBy=timers.target" in timer
    assert f"ExecStart={DEFAULTS['tn_depot_range_guard']} check" in _render("tn-depot-range-guard.service.j2")
    dropin = _render("nftables-tn-depot-range.conf.j2")  # Ubuntu's nftables.service flushes the ruleset
    assert f"ExecStartPost=-/usr/sbin/nft -f {DEFAULTS['tn_depot_range_nft']}" in dropin
    assert f"ExecReload=-/usr/sbin/nft -f {DEFAULTS['tn_depot_range_nft']}" in dropin


def _guard(tmp_path, mode: str, *, table: bool, forward: str = "0"):
    script = tmp_path / "guard.sh"
    script.write_text(_render("tn-depot-range-guard.sh.j2"))
    live = _render("depot-range-fw.nft.j2")[_render("depot-range-fw.nft.j2").index("table inet tn_depot_range {"):]
    (tmp_path / "live.nft").write_text(live.replace("counter drop", "counter packets 0 bytes 0 drop"))
    stubs = {
        "nft": f"#!/bin/sh\n{'cat ' + str(tmp_path / 'live.nft') if table else 'echo no such table >&2; exit 1'}\n",
        "ip": f"#!/bin/sh\necho \"$@\" >> {tmp_path / 'ip.calls'}\n",
        "sysctl": f"#!/bin/sh\necho {forward}\n",
    }
    for name, body in stubs.items():
        (tmp_path / name).write_text(body)
        (tmp_path / name).chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "TN_GUARD_NFT": str(tmp_path / "nft"), "TN_GUARD_IP": str(tmp_path / "ip"),
           "TN_GUARD_SYSCTL": str(tmp_path / "sysctl")}
    r = subprocess.run(["bash", str(script), mode], capture_output=True, text=True, env=env)
    calls = (tmp_path / "ip.calls").read_text().splitlines() if (tmp_path / "ip.calls").exists() else []
    return r.returncode, calls


@pytest.mark.parametrize(("mode", "table", "forward", "rc", "calls"), [
    ("up", True, "0", 0, [f"link set {RANGE_IF} up"]),
    ("up", False, "0", 1, [f"link set {RANGE_IF} down"]),      # firewall unit failed: stays down
    ("check", True, "0", 0, []),
    ("check", False, "0", 1, [f"link set {RANGE_IF} down"]),   # nftables.service flushed the ruleset
    ("check", True, "1", 1, [f"link set {RANGE_IF} down"]),    # forwarding came back on
])
def test_guard_fails_closed(tmp_path, mode, table, forward, rc, calls):
    if shutil.which("bash") is None:
        pytest.skip("bash")
    assert _guard(tmp_path, mode, table=table, forward=forward) == (rc, calls)

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
               "GOVC_URL": "https://vcsa.test/sdk", "TN_ALLOW_NON_ROOT": "1",
               "TN_DEPOT_BUILD_ENV": str(state / "depot-build.env")}
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


def test_depot_phase_generates_the_packer_read_secret_privately_and_never_prints_it(depot_phase, tmp_path):
    r, _, _ = depot_phase("--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    env_file = tmp_path / "state/depot-build.env"
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    (value,) = re.findall(r"^TN_DEPOT_BUILD_READ_PASSWORD=([0-9a-f]{48})$", env_file.read_text(), re.M)
    assert value not in r.stdout + r.stderr and "not shown" in r.stdout
    r2, _, _ = depot_phase("--yes")  # kept, not regenerated
    assert re.findall(r"^TN_DEPOT_BUILD_READ_PASSWORD=(\S+)$", env_file.read_text(), re.M) == [value]
    assert value not in r2.stdout + r2.stderr
    phase = (LAB / "build-phase.sh").read_text()
    assert "sudo --preserve-env=TN_DEPOT_BUILD_READ_PASSWORD" in phase  # environment, not argv
    assert not re.search(r"-e\s+\"?tn_depot_build_read_password", phase)


def test_build_phase_ignores_an_inherited_apply():
    phase = (LAB / "build-phase.sh").read_text()
    lines = phase.splitlines()
    i = lines.index('[[ "$DRY" == "1" ]] || APPLY=1')
    assert lines[i - 2:i] == ["unset APPLY", "APPLY=0"]


# ── prefetch / online windows ─────────────────────────────────────────
def test_going_online_is_refused_while_range_vms_exist_and_shuts_the_range_nic_first():
    up = _task_names(ROLE / "tasks/upstream.yml")
    tasks = yaml.safe_load((ROLE / "tasks/upstream.yml").read_text())
    inc = [(t.get("name"), t.get("ansible.builtin.include_tasks"), t.get("when")) for t in tasks]
    acng = next(i for i, n in enumerate(up) if n.startswith("Upstream — apt-cacher-ng Offlinemode"))
    assert inc[0][1] == "range_guard.yml" and inc[0][2] == "tn_depot_upstream_online | bool"
    assert inc[1][1] == "range_fw.yml" and "tn_depot_upstream_online | bool" in inc[1][2] and acng > 1
    assert inc[-1][1] == "range_fw.yml" and "not (tn_depot_upstream_online | bool)" in inc[-1][2]
    prefetch = yaml.safe_load((ROOT / "install/lab/prefetch.yml").read_text())
    first = prefetch[0]["tasks"][0]["ansible.builtin.include_role"]
    assert first == {"name": "tn_depot", "tasks_from": "range_guard.yml"}

    guard = yaml.safe_load((ROLE / "tasks/range_guard.yml").read_text())
    block = next(t for t in guard if "block" in t)
    assert block["delegate_to"] == "localhost" and block["when"] == "not (tn_depot_force_online | bool)"
    by_name = {t["name"]: t for t in block["block"]}
    assert by_name["Range guard — refuse without vCenter"]["when"] == "tn_depot_guard_about.rc != 0"  # fails closed
    assert "tn_depot_guard_vms.stdout_lines | length > 0" in by_name["Range guard — refuse while range VMs exist"]["when"]
    assert DEFAULTS["tn_depot_force_online"] is False


def test_apt_cacher_ng_is_not_an_open_proxy_and_its_admin_pages_need_auth():
    conf = _render("acng.conf.j2", tn_depot_apt_proxy_url="http://10.30.30.30:3142")
    lines = [ln.strip() for ln in conf.splitlines() if ln.strip() and not ln.startswith("#")]
    assert "ForceManaged: 1" in lines and "AllowUserPorts: 80" in lines and "PassThroughPattern: ^$" in lines
    assert "Remap-tnrocky: dl.rockylinux.org/pub/rocky ; dl.rockylinux.org/pub/rocky" in lines
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    auth = next(t for t in tasks if t.get("name", "").startswith("Depot — apt-cacher-ng AdminAuth"))
    script = auth["ansible.builtin.shell"]
    assert auth["no_log"] is True and "openssl rand -hex 24" in script and "chmod 0640" in script
    assert "echo created" in script and not re.search(r"echo[^\n]*AdminAuth", script)  # value only into the file
    names = [t.get("name") for t in tasks]
    assert names.index(auth["name"]) < names.index("Depot — apt-cacher-ng enabled")


def test_nexus_anonymous_reads_only_the_feed_and_packer_reads_installers_as_its_own_user():
    assert DEFAULTS["tn_depot_anonymous_privileges"] == [
        "nx-repository-view-nuget-chocolatey-read", "nx-repository-view-nuget-chocolatey-browse"]
    assert DEFAULTS["tn_depot_build_read_privileges"] == [
        "nx-repository-view-raw-installers-read", "nx-repository-view-raw-installers-browse"]
    assert "TN_DEPOT_BUILD_READ_PASSWORD" in DEFAULTS["tn_depot_build_read_password"]
    access = yaml.safe_load((ROLE / "tasks/nexus_access.yml").read_text())
    assert all(t.get("no_log") is True for t in access if "ansible.builtin.uri" in t)
    anon = next(t for t in access if t["name"].startswith("Nexus access — the anonymous user has only"))
    assert "'roles': [tn_depot_anonymous_role]" in anon["ansible.builtin.uri"]["body"]
    nexus = _task_names(ROLE / "tasks/nexus.yml")
    assert nexus.index("Nexus — anonymous role and the Packer read user") > nexus.index("Nexus — create missing repositories")
    main = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    probe = next(t for t in main if t.get("name") == "Depot — anonymous cannot read the installers repository")
    assert probe["ansible.builtin.uri"]["status_code"] == [401, 403]


def test_packer_authenticates_for_installers_and_keeps_the_password_sensitive():
    pk = ROOT / "infra/vsphere/packer"
    variables = (pk / "variables.pkr.hcl").read_text()
    block = variables[variables.index('variable "depot_password"'):]
    assert "sensitive   = true" in block[:block.index("\n}\n")]
    for f in ("roles.pkr.hcl", "variants.pkr.hcl"):
        text = (pk / f).read_text()
        assert text.count("DEPOT_USER=${var.depot_user}") == text.count("DEPOT_PASSWORD=${var.depot_password}") >= 1
    for script in ("files/windows/role-image.ps1", "variants/scripts/office-ltsc-from-depot.ps1"):
        ps = (pk / script).read_text()
        calls = [ln for ln in ps.splitlines() if "Invoke-WebRequest" in ln]  # every depot request
        assert calls and all("-Headers $depotHeaders" in c for c in calls), script
        assert "'Basic ' + [Convert]::ToBase64String" in ps
