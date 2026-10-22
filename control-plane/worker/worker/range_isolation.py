"""Isolation evidence per security zone (range certification S4, ADR 0013 §4, issue #242).

After a build is ready, one probe VM in **every** range network (zone) runs the probes of
isolation_scripts.py through the backend's guest operations (``run_in_guest``, as the
least-privileged account the range declares on it), and the outcome is stored in the
range's provisioner_output under ``isolation``. The API turns it into the certification
record's ``isolation`` section (app/range_certification/evidence.py ``isolation``).

States and who moves them (one build, worker only):

    build ready ──> probes run (bounded) ──> ``isolation`` recorded with the output
                         │                          │
                         │ mode enforce and         └─> range ``ready`` (warn: always)
                         │ verdict not isolated ─────> range ``failed`` (range_alloc.build_outcome)
                         └─ no time left / no guest channel: zones ``inconclusive``, never a pass

``check_range_isolation`` (isolation_task.py) re-runs the probes on a ready range and
stores them as ``isolation_reprobe`` next to the build's own ``isolation`` (newest run
only); it never changes the range's state. A re-probe can lower the certification verdict
but raise it only when every zone was measured from a ``probe: true`` VM with a staff-only
account (app/range_certification/evidence.py).

Per zone:

* **probe VM**: a VM with a NIC on the zone, VMware Tools running and an account the range
  declares; a template node flagged ``probe: true`` first, then single-homed before
  multi-homed, then Linux before Windows (PowerShell). None: the zone is ``no_probe``. A
  multi-homed probe measures a target only if its route to it leaves by the zone's NIC
  (isolation_scripts ``Context.src``); otherwise that check is inconclusive.
* **account**: a staff-only account (not visible to Students: the built-in Administrator,
  the build-replacement login) before a Student-visible one, then the least privileged.
* **checks**: a self-test (the zone's gateway, else a peer, answers); TCP and ICMP to each
  site target (``TN_RANGE_ISOLATION_PROBE_TARGETS``); the permitted exceptions
  (``TN_RANGE_ISOLATION_EXCEPTIONS``, default the depot 10.30.32.10:8081 and :3142) must
  connect, every other port of the same hosts (``TN_RANGE_ISOLATION_CLOSED_PORTS``) and
  ICMP to them must not; DNS (the range's internal name resolves through its DNS server,
  if it has one; an external name must not resolve to anything outside the range); a TTL
  probe must not get past the range's own addresses (an ICMP "unreachable" from a router
  outside the range counts as reachable); IPv6: any route other than link-local,
  multicast or loopback, or any address other than fe80::/10 and ::1 (ULA included),
  means IPv6 is present, and only the site's IPv6 targets can then prove it contained.
* **result classes** (``classify``): ``proven_unreachable`` (timed out / no reply **and**
  the self-test passed), ``reachable``, ``inconclusive`` (self-test failed, the guest
  operation failed, no probe), ``connect_failed_only`` (refused or failed connect: never
  proof).

Verdict (``aggregate``): ``isolated`` only when every zone was probed, every check that
must fail is ``proven_unreachable`` and every exception connected; ``not_isolated`` when
anything that must fail was ``reachable``; otherwise ``incomplete``, naming what is missing.

Passwords are never logged or stored: the login is chosen here and handed to
``run_in_guest`` only; the record keeps the account's name, its class (staff or
Student-visible) and whether it is an admin.

**What this evidence is.** It is what the probe VM reported, "as measured from <VM> using
<account class>". Whoever controls that guest can fake it: an administrator inside the VM
(or a Student who has one of its accounts, for a Student-visible one) can make the probes
report silence. The build-time run happens before the range is handed over; re-probes from
Student-reachable VMs are therefore never allowed to raise the verdict.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import isolation_scripts as scripts
from .isolation_scripts import Probe
from .provisioners.base import GuestLogin

logger = logging.getLogger("truenorth.worker.range_isolation")

VERSION = 1
WARN, ENFORCE = "warn", "enforce"
MODES = (WARN, ENFORCE)

# Result classes (ADR 0013 §4)
PROVEN, REACHABLE, INCONCLUSIVE, CONNECT_FAILED = "proven_unreachable", "reachable", "inconclusive", \
    "connect_failed_only"
RESULTS = (PROVEN, REACHABLE, INCONCLUSIVE, CONNECT_FAILED)
# Verdicts
ISOLATED, NOT_ISOLATED, INCOMPLETE = "isolated", "not_isolated", "incomplete"
# What a check expects: must fail, must connect, a precondition, a recorded fact.
MUST_FAIL, MUST_CONNECT, PRECONDITION, FACT = "unreachable", "reachable", "precondition", "fact"
FACT_STATES = {0: "present", 1: "absent"}  # v6route / v6addr codes; anything else "unknown"
STAFF, STUDENT_VISIBLE = "staff", "student_visible"  # account classes

# Zones never probed: the background-noise control channel is an out-of-band management
# network by design (render.noise_mgmt_plan), not a range security zone.
NOT_ZONES = frozenset({"noise_mgmt"})
APPLIANCE_KEYS = ("pfsense", "opnsense", "vyos", "securityonion")
ATTEMPTS_PER_ZONE = 2  # (VM, login) pairs tried before a zone is inconclusive
MIN_ZONE_SECONDS = 20.0  # less than this left: the zone is not started


class SettingsError(ValueError):
    """An installer setting the probes cannot use (recorded, never a crash)."""


@dataclass(frozen=True)
class Target:
    host: str
    port: int | None = None

    @property
    def v6(self) -> bool:
        try:
            return ipaddress.ip_address(self.host).version == 6
        except ValueError:
            return False

    def __str__(self) -> str:
        host = f"[{self.host}]" if self.v6 else self.host
        return f"{host}:{self.port}" if self.port else host


def parse_target(text: str) -> Target:
    """``host:port``, ``[v6]:port``, or a bare host (ICMP only)."""
    raw = text.strip()
    host, port = raw, None
    if raw.startswith("["):
        host, _, rest = raw[1:].partition("]")
        if rest:
            if not rest.startswith(":"):
                raise SettingsError(f"target {text!r} is not [address]:port")
            port = rest[1:]
    elif raw.count(":") == 1:
        host, port = raw.split(":")
    if not scripts.valid_host(host):
        raise SettingsError(f"target {text!r}: {host!r} is not an address or host name")
    if port is not None:
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise SettingsError(f"target {text!r}: port {port!r} is not 1-65535")
        return Target(host, int(port))
    return Target(host)


@dataclass(frozen=True)
class ProbeAccount:
    """An account a probe may log in as. ``student_visible``: a Student can reveal its
    password (app/range_credentials), so a guest it opens may report what a Student wants."""

    username: str
    password: object  # vsphere_guest.Secret: never printed
    admin: bool = False
    student_visible: bool = True  # unknown: assumed visible (never trusted more than it is)


def load_accounts(session, range_id: str) -> dict[str, list[ProbeAccount]]:
    """Machine (node id) -> its accounts with their class (db_ops.range_accounts and
    range_account_visibility)."""
    from . import db_ops
    from .provisioners.vsphere_guest import Secret

    with session() as db:
        rows = db_ops.range_accounts(db, range_id)
        visible = db_ops.range_account_visibility(db, range_id)
    out: dict[str, list[ProbeAccount]] = {}
    for machine, username, password, role in rows:
        if password:
            out.setdefault(str(machine), []).append(ProbeAccount(
                username, Secret(password), role == "admin", visible.get((str(machine), username), True)))
    return out


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    mode: str
    targets: tuple[Target, ...]
    exceptions: tuple[Target, ...]
    closed_ports: tuple[int, ...]
    external_name: str
    simulated_cidrs: tuple[str, ...]
    probe_timeout: int
    budget: float
    zone_timeout: float
    notes: tuple[str, ...] = ()

    def public(self) -> dict:
        return {"mode": self.mode, "targets": [str(t) for t in self.targets],
                "exceptions": [str(t) for t in self.exceptions], "closed_ports": list(self.closed_ports),
                "external_name": self.external_name, "simulated_cidrs": list(self.simulated_cidrs),
                "probe_timeout": self.probe_timeout}


def mode() -> tuple[str, str]:
    """(mode, note): ``RANGE_ISOLATION_CHECK``; anything but ``enforce`` is ``warn``."""
    raw = os.getenv("RANGE_ISOLATION_CHECK", WARN).strip().lower() or WARN
    if raw in MODES:
        return raw, ""
    return WARN, f"RANGE_ISOLATION_CHECK={raw!r} is not warn or enforce; warn applies"


def settings() -> Settings:
    """The installer's settings (tn_range_isolation_*), read now. ``SettingsError`` when one
    cannot be used: the caller records the zones inconclusive and why."""
    current, note = mode()
    targets = tuple(parse_target(t) for t in _list("TN_RANGE_ISOLATION_PROBE_TARGETS", ""))
    exceptions = tuple(parse_target(t) for t in _list("TN_RANGE_ISOLATION_EXCEPTIONS",
                                                      "10.30.32.10:8081,10.30.32.10:3142"))
    if any(t.port is None for t in exceptions):
        raise SettingsError("every TN_RANGE_ISOLATION_EXCEPTIONS entry needs a port (host:port)")
    ports = _list("TN_RANGE_ISOLATION_CLOSED_PORTS", "22,80,443,8080")
    if not all(p.isdigit() and 0 < int(p) < 65536 for p in ports):
        raise SettingsError(f"TN_RANGE_ISOLATION_CLOSED_PORTS={','.join(ports)!r} is not a list of ports")
    name = os.getenv("TN_RANGE_ISOLATION_EXTERNAL_NAME", "example.com").strip()
    if not scripts.valid_name(name):
        raise SettingsError(f"TN_RANGE_ISOLATION_EXTERNAL_NAME={name!r} is not a host name")
    cidrs = tuple(_list("TN_RANGE_ISOLATION_SIMULATED_CIDRS", "198.18.0.0/15"))
    if not all(scripts.valid_cidr(c) for c in cidrs):
        raise SettingsError(f"TN_RANGE_ISOLATION_SIMULATED_CIDRS={','.join(cidrs)!r} is not a list of CIDRs")
    try:
        timeout = int(os.getenv("TN_RANGE_ISOLATION_PROBE_TIMEOUT", "2"))
        budget = float(os.getenv("TN_RANGE_ISOLATION_BUDGET", "900"))
        zone = float(os.getenv("TN_RANGE_ISOLATION_ZONE_TIMEOUT", "180"))
    except ValueError as exc:
        raise SettingsError(f"an isolation time setting is not a number: {exc}") from None
    return Settings(current, targets, exceptions, tuple(sorted({int(p) for p in ports})), name, cidrs,
                    max(1, min(timeout, 10)), max(budget, 0.0), max(zone, MIN_ZONE_SECONDS),
                    (note,) if note else ())


# --------------------------------------------------------------------------- #
# Zones and probe VMs
# --------------------------------------------------------------------------- #

def _ip(text) -> str:
    try:
        return str(ipaddress.ip_address(str(text)))
    except ValueError:
        return ""


def excluded(output: dict) -> list[str]:
    """Range networks deliberately not probed, each with why (stated with the verdict)."""
    return [f"{n.get('name')} (out-of-band background-noise control channel, not a range security zone)"
            for n in output.get("networks") or [] if isinstance(n, dict) and n.get("name") in NOT_ZONES]


def zones(output: dict) -> list[dict]:
    """Every range network of a build: ``{"zone", "key", "cidr", "gateway"}``. ``key`` is
    the name VM NICs carry for it (vSphere: the port group)."""
    out = []
    for net in output.get("networks") or []:
        if not isinstance(net, dict) or net.get("rspan") or net.get("name") in NOT_ZONES:
            continue
        name = str(net.get("name") or "")
        key = str(net.get("portgroup") or net.get("port_group") or name)
        if name:
            out.append({"zone": name, "key": key, "cidr": str(net.get("cidr") or ""),
                        "gateway": _ip(net.get("gateway"))})
    return out


def _vms(output: dict) -> list[dict]:
    return [v for v in output.get("vms") or [] if isinstance(v, dict)]


def _zone_keys(z: dict) -> set[str]:
    return {z["key"], z["zone"]}


def _on(vm: dict, z: dict) -> list[dict]:
    return [n for n in vm.get("nics") or [] if isinstance(n, dict) and n.get("network") in _zone_keys(z)]


def range_addresses(output: dict) -> list[str]:
    """Every IPv4 address the range's own machines hold (its NICs, Tools' report, the edge's
    WAN address): the hops a TTL probe may meet without having left the range."""
    seen: set[str] = set()
    for v in _vms(output):
        for a in [n.get("ip") for n in v.get("nics") or [] if isinstance(n, dict)] + list(v.get("guest_ips") or []):
            if (ip := _ip(a)) and ipaddress.ip_address(ip).version == 4:
                seen.add(ip)
    if isinstance(output.get("uplink"), dict) and (ip := _ip(output["uplink"].get("ip"))):
        seen.add(ip)
    return sorted(seen, key=ipaddress.ip_address)


def node_of(vm: dict, range_id: str) -> str:
    return str(vm.get("node_id") or str(vm.get("name") or "").removeprefix(f"{range_id[:8]}-"))


def _family(vm: dict) -> str:
    return str(vm.get("family") or "")


def probe_candidates(output: dict, z: dict, flagged: set[str], range_id: str, range_zone_keys: set[str]) -> list[dict]:
    """The VMs that can probe zone ``z``, best first."""
    out = []
    for v in _vms(output):
        if not v.get("vm_id") or not _on(v, z) or _family(v) not in scripts.STEP_BUILDERS:
            continue
        if any(k in v for k in APPLIANCE_KEYS) or v.get("tools_ready") is False or v.get("ip_verified") is False:
            continue
        out.append((node_of(v, range_id) not in flagged, multi_homed(v), _family(v) != "linux", str(v.get("name")), v))
    return [row[-1] for row in sorted(out, key=lambda r: r[:4])]


def multi_homed(vm: dict) -> bool:
    """More than one NIC (any network: a noise control NIC counts too)."""
    return len([n for n in vm.get("nics") or [] if isinstance(n, dict)]) > 1


def account_class(acct) -> str:
    return STUDENT_VISIBLE if getattr(acct, "student_visible", True) else STAFF


def logins(vm: dict, accounts: dict, range_id: str) -> list[tuple[GuestLogin, bool, str]]:
    """(login, is admin, account class) to try on ``vm``: staff-only accounts before
    Student-visible ones, then those the build verified (``accounts_applied``), then the
    least privileged. ``accounts``: node id -> [ProbeAccount]."""
    applied = {str(n).casefold() for n in vm.get("accounts_applied") or []}
    rows = sorted(accounts.get(node_of(vm, range_id)) or [],
                  key=lambda a: (account_class(a) != STAFF, a.username.casefold() not in applied, bool(a.admin),
                                 a.username))
    return [(GuestLogin(a.username, a.password.reveal(), _family(vm)), bool(a.admin), account_class(a)) for a in rows]


def zone_context(vm: dict, z: dict, output: dict, cfg: Settings) -> scripts.Context:
    """The probe's shared context in zone ``z``: its zone address when multi-homed (every
    probe must leave by that NIC), the range's own addresses, the CIDRs counted inside."""
    src = next((_ip(n.get("ip")) for n in _on(vm, z) if _ip(n.get("ip"))), "") if multi_homed(vm) else ""
    inside = sorted({z2["cidr"] for z2 in zones(output) if scripts.valid_cidr(z2["cidr"])} | set(cfg.simulated_cidrs))
    return scripts.Context(src, tuple(range_addresses(output)), tuple(inside))


def _peers(output: dict, z: dict, probe: dict) -> list[str]:
    """Who the self-test asks, in order: the zone's gateway when a range machine holds it,
    then up to two other machines on the zone (Linux first: Windows drops ping by default)."""
    held = {_ip(n.get("ip")) for v in _vms(output) for n in _on(v, z)}
    out = [z["gateway"]] if z["gateway"] and z["gateway"] in held else []
    others = sorted((v for v in _vms(output) if v is not probe and _on(v, z)),
                    key=lambda v: (_family(v) == "windows", str(v.get("name"))))
    for v in others:
        ip = next((_ip(n.get("ip")) for n in _on(v, z) if _ip(n.get("ip"))), "")
        if ip and ip not in out:
            out.append(ip)
    return out[:3]


def range_dns(output: dict, template: dict, range_id: str) -> tuple[str, str] | None:
    """(DNS server address, internal name) of the range, if it has a DNS server: the template's
    ``isolation: {dns_server, internal_name}``, else its first domain controller and that
    controller's AD domain."""
    block = template.get("isolation") if isinstance(template.get("isolation"), dict) else {}
    if block.get("dns_server") and block.get("internal_name"):
        server, name = _ip(block["dns_server"]), str(block["internal_name"])
        return (server, name) if server and scripts.valid_name(name) else None
    nodes = {str(n.get("id")): n for n in template.get("nodes") or [] if isinstance(n, dict)}
    for v in _vms(output):
        node = nodes.get(node_of(v, range_id)) or {}
        services = {str(s).lower() for s in node.get("services") or []} if isinstance(node.get("services"), list) else set()
        if node.get("role") == "domain_controller" or "ad-ds" in services:
            name = str(node.get("ad_domain") or node.get("ad_forest") or template.get("ad_domain")
                       or template.get("domain") or "")
            server = _ip(v.get("ip")) or next((_ip(n.get("ip")) for n in v.get("nics") or [] if _ip(n.get("ip"))), "")
            if server and scripts.valid_name(name):
                return server, name
    return None


def flagged_probes(template: dict) -> set[str]:
    """Template node ids marked ``probe: true`` (preferred probe VMs)."""
    return {str(n.get("id")) for n in template.get("nodes") or [] if isinstance(n, dict) and n.get("probe") is True}


# --------------------------------------------------------------------------- #
# The checks of one zone
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Check:
    check: str  # selftest | tcp | icmp | exception | closed_port | dns_internal | dns_external | route | ipv6_*
    target: str
    expect: str
    probe: Probe


def plan_checks(z: dict, probe_vm: dict, output: dict, cfg: Settings, dns: tuple[str, str] | None) -> list[Check]:
    peers = _peers(output, z, probe_vm)
    out: list[Check] = []
    if peers:
        out.append(Check("selftest", ",".join(peers), PRECONDITION, Probe("selftest", tuple(peers))))
    seen_icmp: set[str] = set()
    for t in cfg.targets:
        if t.port:
            out.append(Check("tcp", str(t), MUST_FAIL, Probe("tcp", (t.host, str(t.port)))))
        if t.host not in seen_icmp:
            seen_icmp.add(t.host)
            out.append(Check("icmp", t.host, MUST_FAIL, Probe("icmp", (t.host,))))
    allowed = {(t.host, t.port) for t in cfg.exceptions}
    for t in cfg.exceptions:
        out.append(Check("exception", str(t), MUST_CONNECT, Probe("tcp", (t.host, str(t.port)))))
    for host in sorted({t.host for t in cfg.exceptions}):
        for port in cfg.closed_ports:
            if (host, port) not in allowed:
                out.append(Check("closed_port", str(Target(host, port)), MUST_FAIL, Probe("tcp", (host, str(port)))))
        if host not in seen_icmp:
            seen_icmp.add(host)
            out.append(Check("icmp", host, MUST_FAIL, Probe("icmp", (host,))))
    if dns is not None:
        out.append(Check("dns_internal", f"{dns[1]} @ {dns[0]}", PRECONDITION, Probe("dns_int", dns)))
    out.append(Check("dns_external", cfg.external_name, MUST_FAIL, Probe("dns_ext", (cfg.external_name,))))
    route_to = next((t.host for t in cfg.targets if not t.v6 and _ip(t.host)), "")
    if route_to:
        out.append(Check("route", route_to, MUST_FAIL, Probe("route", (route_to,))))
    out.append(Check("ipv6_route", "route", FACT, Probe("v6route")))
    out.append(Check("ipv6_address", "address", FACT, Probe("v6addr")))
    return out


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

def classify(check: Check, code: int | None, selftest_ok: bool, dns_ok: bool | None = None) -> str:
    """One check's result class from its probe code (isolation_scripts). ``None`` (the
    guest step failed, timed out, or did not run): inconclusive. A silent target counts as
    proof only with a passing self-test (and, for the external name, a working range DNS)."""
    if code is None:
        return INCONCLUSIVE
    kind = check.probe.kind

    def silent() -> str:
        return PROVEN if selftest_ok else INCONCLUSIVE

    if kind == "tcp":
        return {0: REACHABLE, 1: CONNECT_FAILED, 2: CONNECT_FAILED, 3: silent()}.get(code, INCONCLUSIVE)
    if kind == "icmp":  # 3: rejected by one of the range's own routers; 4: a site router answered
        return {0: REACHABLE, 1: silent(), 3: CONNECT_FAILED, 4: REACHABLE}.get(code, INCONCLUSIVE)
    if kind == "selftest":
        return REACHABLE if code == 0 else INCONCLUSIVE
    if kind == "dns_int":
        return {0: REACHABLE, 1: INCONCLUSIVE}.get(code, INCONCLUSIVE)
    if kind == "dns_ext":
        if code == 0:
            return REACHABLE
        if code in (1, 2):
            return PROVEN if selftest_ok and dns_ok is not False else INCONCLUSIVE
        return INCONCLUSIVE
    if kind == "route":
        return {0: silent(), 1: REACHABLE}.get(code, INCONCLUSIVE)
    return INCONCLUSIVE


def passed(expect: str, result: str) -> bool | None:
    if expect == MUST_FAIL:
        return result == PROVEN
    if expect in (MUST_CONNECT, PRECONDITION):
        return result == REACHABLE
    return None  # a fact is not graded


def grade_zone(z: dict, planned: list[Check], codes: list[int | None], cfg: Settings) -> dict:
    """The zone's rows and verdict from each check's probe code (``None``: no outcome)."""
    by = dict(zip(planned, codes, strict=True))
    self_rows = [c for c in planned if c.check == "selftest"]
    selftest_ok = bool(self_rows) and classify(self_rows[0], by[self_rows[0]], True) == REACHABLE
    dns_rows = [c for c in planned if c.check == "dns_internal"]
    dns_ok = None if not dns_rows else classify(dns_rows[0], by[dns_rows[0]], selftest_ok) == REACHABLE
    rows = []
    for c in planned:
        if c.expect == FACT:
            continue
        result = classify(c, by[c], selftest_ok, dns_ok)
        row = {"check": c.check, "target": c.target, "expect": c.expect, "result": result, "code": by[c],
               "passed": passed(c.expect, result)}
        if by[c] == scripts.NOT_MEASURABLE and c.probe.kind != "selftest":
            row["note"] = "not measured from this zone: the probe VM's route leaves by another NIC"
        elif c.probe.kind == "icmp" and by[c] == 4:
            row["note"] = "an ICMP unreachable came from a router outside the range"
        rows.append(row)
    # IPv6 facts (not checks): with no default route and no global address the zone has no
    # IPv6 path out; with either, only the site's IPv6 targets (if any are set) can prove
    # it contained.
    ipv6 = {c.check.removeprefix("ipv6_"): FACT_STATES.get(by[c], "unknown") for c in planned if c.expect == FACT}
    v6_targets = [t for t in cfg.targets if t.v6]
    absent = ipv6.get("route") == "absent" and ipv6.get("address") == "absent"
    if absent:
        for r in rows:  # a refused/failed IPv6 connect is no weaker than having no IPv6 at all
            if r["check"] in ("tcp", "icmp") and r["result"] == CONNECT_FAILED and _is_v6(r["target"]):
                r.update(result=PROVEN if selftest_ok else INCONCLUSIVE, note="no IPv6 route and no IPv6 address")
                r["passed"] = passed(r["expect"], r["result"])
    if not v6_targets:
        if absent:
            v6, note = (PROVEN if selftest_ok else INCONCLUSIVE), "no IPv6 route and no IPv6 address (not even ULA)"
        elif "unknown" in ipv6.values():
            v6, note = INCONCLUSIVE, "the guest's IPv6 state could not be read"
        else:
            v6, note = INCONCLUSIVE, ("IPv6 is present (a route or an address, ULA included) but no site IPv6 "
                                      "targets are set to prove it contained (#238)")
        rows.append({"check": "ipv6_egress", "target": "IPv6", "expect": MUST_FAIL, "result": v6, "code": None,
                     "passed": passed(MUST_FAIL, v6), "note": note})
    return {**z, "selftest": {"passed": selftest_ok, "targets": self_rows[0].target if self_rows else ""},
            "ipv6": ipv6, "checks": rows, **verdict_of_checks(rows, selftest_ok, bool(self_rows))}


def _is_v6(target: str) -> bool:
    host = target[1:].partition("]")[0] if target.startswith("[") else target
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def verdict_of_checks(rows: list[dict], selftest_ok: bool, has_selftest: bool) -> dict:
    leaks = [f"{r['check']} {r['target']}" for r in rows if r["expect"] == MUST_FAIL and r["result"] == REACHABLE]
    if leaks:
        return {"verdict": NOT_ISOLATED, "reasons": [f"reachable outside its exceptions: {', '.join(leaks)}"]}
    reasons = []
    if not has_selftest:
        reasons.append("no gateway or peer to self-test against")
    elif not selftest_ok:
        reasons.append("self-test failed (no silence counts as proof)")
    weak = [f"{r['check']} {r['target']} ({r['result']})" for r in rows
            if r["expect"] == MUST_FAIL and r["result"] != PROVEN]
    if weak:
        reasons.append(f"not proven unreachable: {', '.join(weak)}")
    missing = [f"{r['target']} ({r['result']})" for r in rows if r["expect"] == MUST_CONNECT and r["result"] != REACHABLE]
    if missing:
        reasons.append(f"permitted exception did not connect: {', '.join(missing)}")
    return {"verdict": INCOMPLETE if reasons else ISOLATED, "reasons": reasons}


def unprobed(z: dict, status: str, reason: str) -> dict:
    return {**z, "status": status, "probe": None, "selftest": {"passed": False, "targets": ""}, "ipv6": {},
            "checks": [],
            "verdict": INCOMPLETE, "reasons": [reason]}


def measured_from(zone_rows: list[dict]) -> str:
    """"as measured from <VM> using <account class>", per probed zone."""
    parts = [f"{z['zone']}: {z['probe']['vm']} using a {z['probe'].get('account_class', STUDENT_VISIBLE).replace('_', '-')}"
             f" account" for z in zone_rows if z.get("probe")]
    return f"as measured from {'; '.join(parts)}" if parts else ""


def aggregate(zone_rows: list[dict], cfg: Settings | None, *, notes: list[str] | None = None,
              skipped_nets: list[str] | None = None) -> dict:
    """The range's verdict over its zones (ADR 0013 §4)."""
    notes = list(notes or []) + [f"not probed: {n}" for n in skipped_nets or []]
    failures = [f"zone {z['zone']}: {'; '.join(z['reasons'])}" for z in zone_rows if z["verdict"] == NOT_ISOLATED]
    gaps = [f"zone {z['zone']}: {'; '.join(z['reasons'])}" for z in zone_rows if z["verdict"] == INCOMPLETE]
    if not zone_rows:
        gaps.append("no range networks recorded")
    if cfg is not None and not cfg.targets:
        gaps.append("no site probe targets configured (tn_range_isolation_probe_targets)")
    verdict = NOT_ISOLATED if failures else INCOMPLETE if gaps else ISOLATED
    probed = sum(1 for z in zone_rows if z.get("status") == "probed")
    if verdict == ISOLATED:
        summary = f"isolated: {len(zone_rows)} zone(s) probed, every check that must fail proven unreachable"
    elif verdict == NOT_ISOLATED:
        summary = "not isolated: " + "; ".join(failures)
    else:
        summary = f"incomplete ({probed} of {len(zone_rows)} zone(s) probed): " + "; ".join(gaps)
    summary += "".join(f" ({part})" for part in (measured_from(zone_rows), "; ".join(
        f"not probed: {n}" for n in skipped_nets or [])) if part)
    return {"verdict": verdict, "summary": summary[:900], "failures": failures, "missing": gaps, "notes": notes,
            "excluded": list(skipped_nets or [])}


def record(zone_rows: list[dict], cfg: Settings | None, current_mode: str, *, notes: list[str] | None = None,
           started: float | None = None, source: str = "build", skipped_nets: list[str] | None = None) -> dict:
    """What provisioner_output's ``isolation`` holds. ``started``: the run's monotonic start
    (``started_at`` orders re-probes, db_ops.store_isolation)."""
    now = datetime.now(UTC)
    elapsed = max(time.monotonic() - started, 0.0) if started is not None else 0.0
    return {"version": VERSION, "mode": current_mode, "started_at": (now - timedelta(seconds=elapsed)).isoformat(),
            "ran_at": now.isoformat(), "seconds": round(elapsed, 1), "settings": cfg.public() if cfg else None,
            "source": source, "zones": zone_rows, **aggregate(zone_rows, cfg, notes=notes, skipped_nets=skipped_nets)}


def blocking(rec: dict | None) -> str | None:
    """The build error enforce mode records for ``rec``, or None (warn mode, or isolated)."""
    if not isinstance(rec, dict) or rec.get("mode") != ENFORCE or rec.get("verdict") == ISOLATED:
        return None
    return f"isolation not proven (RANGE_ISOLATION_CHECK=enforce): {rec.get('summary') or rec.get('verdict')}"


# --------------------------------------------------------------------------- #
# Running the probes
# --------------------------------------------------------------------------- #

def attempts(candidates: list[dict], accounts: dict, range_id: str) -> list[tuple[dict, GuestLogin, bool, str]]:
    """(VM, login, is admin) to try, at most ATTEMPTS_PER_ZONE: each candidate VM with its
    least-privileged login first (a VM whose Tools are down fails whatever the login), then
    the VMs' other logins. Each failed attempt can cost a zone's whole timeout."""
    per_vm = [[(vm, *login) for login in logins(vm, accounts, range_id)] for vm in candidates]
    ordered = [row[0] for row in per_vm if row] + [a for row in per_vm for a in row[1:]]
    return ordered[:ATTEMPTS_PER_ZONE]


async def _probe_zone(provisioner, range_id: str, z: dict, output: dict, template: dict, accounts: dict,
                      cfg: Settings, flagged: set[str], all_keys: set[str], deadline: float) -> dict:
    dns = range_dns(output, template, range_id)
    tried: list[str] = []
    last_error = ""
    for vm, login, admin, cls in attempts(probe_candidates(output, z, flagged, range_id, all_keys), accounts, range_id):
        left = deadline - time.monotonic()
        if left < MIN_ZONE_SECONDS:
            return unprobed(z, "budget_exhausted", "the isolation time budget ran out before this zone")
        planned = plan_checks(z, vm, output, cfg, dns)
        ctx = zone_context(vm, z, output, cfg)
        steps = scripts.steps(_family(vm), [c.probe for c in planned], cfg.probe_timeout, ctx)
        tried.append(f"{vm.get('name')} as {login.username}")
        is_flagged = node_of(vm, range_id) in flagged
        probe = {"vm": vm.get("name"), "family": _family(vm), "account": login.username, "admin": admin,
                 "account_class": cls, "flagged": is_flagged, "multi_homed": bool(ctx.src), "source_ip": ctx.src,
                 # a re-probe from here may raise the verdict (app/range_certification/evidence.py)
                 "trusted": is_flagged and cls == STAFF}
        try:
            ran = await provisioner.run_in_guest(
                vm["vm_id"], login, steps, min(cfg.zone_timeout, left), range_id=range_id,
                vm_name=str(vm.get("name") or ""),
                **({"identity": vm["identity"]} if isinstance(vm.get("identity"), dict) else {}))
        except Exception as exc:  # noqa: BLE001 — the provider scrubs the password; recorded, next candidate
            logger.warning("range %s zone %s: guest operations on %s failed: %s", range_id, z["zone"],
                           vm.get("name"), str(exc)[:300])
            last_error = str(exc)[:300]
            continue
        codes = _codes(planned, steps, ran)
        graded = grade_zone(z, planned, codes, cfg)
        failed_steps = [r.get("label") for r in ran if r.get("status") != "ok"]
        return {**graded, "status": "probed", "probe": probe, **({"step_errors": failed_steps} if failed_steps else {})}
    if not tried:
        return unprobed(z, "no_probe", "no VM with VMware Tools and a declared account on this zone to probe from")
    return unprobed(z, "guest_op_failed", f"guest operations failed on {', '.join(tried)}: {last_error}"[:400])


def _codes(planned: list[Check], steps: list, ran: list[dict]) -> list[int | None]:
    by_label = {r.get("label"): r for r in ran if isinstance(r, dict)}
    codes: list[int | None] = []
    for step, group in zip(steps, scripts.chunks(planned), strict=True):
        r = by_label.get(step.label) or {}
        codes += scripts.decode(r.get("exit_code") if r.get("status") == "ok" else None, len(group))
    return codes


async def run(provisioner, range_id: str, output: dict, template: dict, accounts: dict, *,
              finish_by: float | None = None, source: str = "build") -> dict:
    """Probe every zone of a built range; returns the ``isolation`` record. Never raises:
    whatever goes wrong is recorded as inconclusive evidence. Zones run one after another
    (one guest-operations session at a time), within ``TN_RANGE_ISOLATION_BUDGET`` and
    before ``finish_by`` (the build's monotonic stop-by, fencing.provision_finish_by)."""
    started = time.monotonic()
    current, _ = mode()
    try:
        cfg = settings()
    except SettingsError as exc:
        logger.error("range %s: isolation probes not run: %s", range_id, exc)
        rows = [unprobed(z, "not_run", f"setting unusable: {exc}") for z in zones(output)]
        return record(rows, None, current, notes=[str(exc)], started=started, source=source,
                      skipped_nets=excluded(output))
    notes = list(cfg.notes)
    deadline = started + cfg.budget
    if finish_by is not None:
        deadline = min(deadline, finish_by - 30.0)  # leave the build time to store its result
    found = zones(output)
    if not getattr(provisioner, "supports_guest_commands", False):
        reason = f"the {output.get('provider') or type(provisioner).__name__} backend cannot run commands in a guest"
        return record([unprobed(z, "no_probe", reason) for z in found], cfg, cfg.mode, notes=notes, started=started,
                      source=source, skipped_nets=excluded(output))
    flagged = flagged_probes(template)
    all_keys = {k for z in found for k in _zone_keys(z)}
    rows = []
    for z in found:
        try:
            rows.append(await _probe_zone(provisioner, range_id, z, output, template, accounts, cfg, flagged,
                                          all_keys, deadline))
        except Exception as exc:  # noqa: BLE001 — never a crashed build over evidence
            logger.exception("range %s zone %s: isolation probe error", range_id, z["zone"])
            rows.append(unprobed(z, "guest_op_failed", f"probe error: {type(exc).__name__}"))
    return record(rows, cfg, cfg.mode, notes=notes, started=started, source=source, skipped_nets=excluded(output))


def skipped(output: dict, reason: str) -> dict:
    """The record of a build whose probes were not run (not ready, failed)."""
    current, _ = mode()
    return record([unprobed(z, "not_run", reason) for z in zones(output)], None, current, notes=[reason],
                  skipped_nets=excluded(output))


async def after_build(provisioner, range_id: str, template: dict, result, allocations: dict, session) -> None:
    """range_accounts.provision, once the build returned: probe a ready build and attach
    ``result.isolation`` (stored with the output; enforce fails the build in
    range_alloc.build_outcome). A failed or not-ready build is not probed. In enforce mode a
    range not proven isolated also has its VMs powered off here (``enforced``), so it is
    not left running on a path out while it waits to be destroyed."""
    if getattr(result, "status", "failed") == "failed":
        return
    output = {"provider": type(provisioner).__name__, "vms": list(result.vms or []),
              "networks": list(result.networks or []), "uplink": getattr(result, "uplink", None)}
    if getattr(result, "not_ready", None):
        result.isolation = skipped(output, "the range is not ready (guest network check failed); probes not run")
        return
    try:
        accounts = load_accounts(session, range_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("range %s: accounts for the isolation probes not read (%s)", range_id, type(exc).__name__)
        accounts = {}
    try:
        result.isolation = await run(provisioner, range_id, output, template if isinstance(template, dict) else {},
                                     accounts, finish_by=allocations.get("finish_by"))
    except Exception as exc:  # noqa: BLE001 — evidence never crashes a build; recorded as not run
        logger.exception("range %s: isolation probes failed", range_id)
        result.isolation = skipped(output, f"isolation probes failed: {type(exc).__name__}")
    logger.info("range %s: isolation %s (%s mode)", range_id, result.isolation["verdict"], result.isolation["mode"])
    if blocking(result.isolation):
        result.isolation["enforced"] = await power_off(provisioner, range_id, output)


async def power_off(provisioner, range_id: str, output: dict) -> dict:
    """Enforce mode: power off every VM of a range that failed isolation (its edge
    firewall's uplink with it). Never raises; what happened is recorded."""
    stop_output = dict(output)
    record_of = getattr(provisioner, "connection_record", None)
    try:
        if callable(record_of) and isinstance(rec := record_of(), dict) and rec:
            stop_output["connection"] = rec
        stopped = await provisioner.stop(range_id, stop_output)
        logger.warning("range %s: not proven isolated in enforce mode; %s VM(s) powered off", range_id,
                       getattr(stopped, "vms_stopped", "?"))
        return {"action": "powered_off", "status": getattr(stopped, "status", "unknown"),
                "vms_stopped": getattr(stopped, "vms_stopped", None), "errors": list(getattr(stopped, "errors", []))[:5]}
    except Exception as exc:  # noqa: BLE001 — the build fails anyway; say the VMs may still run
        logger.error("range %s: VMs not powered off after failed isolation (%s)", range_id, type(exc).__name__)
        return {"action": "power_off_failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

