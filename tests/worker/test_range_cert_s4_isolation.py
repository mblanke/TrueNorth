"""Range certification S4 (ADR 0013 §4, issue #242): isolation evidence per security zone.

* result classes: a refused connect is ``connect_failed_only`` (never proof), a timeout is
  ``proven_unreachable`` only with a passing self-test, a failed self-test or guest
  operation makes everything ``inconclusive``;
* per-zone aggregation: a zone without a probe keeps the verdict from Isolated and is named;
* exceptions: the depot's 8081 and 3142 must connect, every other depot port must not;
* IPv6 present / absent; the Windows (PowerShell) probe path; least privilege; budget;
* warn (the default) records, enforce fails the build; the re-probe task's store is
  conditional (still ready, same VMs, newest run).

The guest channel is a fake that reads the steps the real script generator wrote (the
bash call lines, the PowerShell decoded from -EncodedCommand) and answers per probe, so
the packing and decoding of exit codes is exercised end to end. The scripts themselves
were run in Ubuntu 24.04 and pwsh containers by hand (see the PR); not in CI.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shlex
import time
import uuid

import pytest
import sqlalchemy as sa
from worker import isolation_scripts as scripts
from worker import isolation_task, range_alloc, range_isolation
from worker.provisioners.results import ProvisionResult
from worker.provisioners.vsphere_guest import RangeAccount, Secret

RID = "a1b2c3d4-0000-4000-8000-000000000001"
SITE = "10.30.30.1:443,10.30.30.10:53,192.168.1.10:443,10.30.30.30:22,10.30.30.20:443"
DEPOT = "10.30.32.10"
PASSWORDS = ("Student-pw-1x", "Admin-pw-2y", "Win-admin-3z")


@pytest.fixture(autouse=True)
def site(monkeypatch):
    for name in ("RANGE_ISOLATION_CHECK", "TN_RANGE_ISOLATION_EXCEPTIONS", "TN_RANGE_ISOLATION_CLOSED_PORTS",
                 "TN_RANGE_ISOLATION_EXTERNAL_NAME", "TN_RANGE_ISOLATION_BUDGET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TN_RANGE_ISOLATION_PROBE_TARGETS", SITE)


def _output(**over) -> dict:
    out = {
        "provider": "vsphere_api",
        "networks": [{"name": "lan", "portgroup": "tn-a1b2c3d4-101", "cidr": "10.80.0.0/24", "gateway": "10.80.0.1"},
                     {"name": "dmz", "portgroup": "tn-a1b2c3d4-102", "cidr": "10.80.1.0/24", "gateway": "10.80.1.1"}],
        "vms": [
            {"vm_id": "vm-fw", "name": "a1b2c3d4-fw", "node_id": "fw", "family": "appliance", "pfsense": {},
             "nics": [{"network": "tn-a1b2c3d4-101", "ip": "10.80.0.1"}, {"network": "tn-a1b2c3d4-102", "ip": "10.80.1.1"}]},
            {"vm_id": "vm-web", "name": "a1b2c3d4-web01", "node_id": "web01", "family": "linux", "tools_ready": True,
             "ip_verified": True, "accounts_applied": ["student", "soadmin"],
             "nics": [{"network": "tn-a1b2c3d4-101", "ip": "10.80.0.20"}]},
            {"vm_id": "vm-win", "name": "a1b2c3d4-win01", "node_id": "win01", "family": "windows", "tools_ready": True,
             "ip_verified": True, "accounts_applied": ["Administrator"],
             "nics": [{"network": "tn-a1b2c3d4-102", "ip": "10.80.1.30"}]},
        ],
        "uplink": {"ip": "10.30.32.101"},
    }
    out.update(over)
    return out


ACCOUNTS = {
    "web01": [RangeAccount("soadmin", Secret(PASSWORDS[1]), True), RangeAccount("student", Secret(PASSWORDS[0]), False)],
    "win01": [RangeAccount("Administrator", Secret(PASSWORDS[2]), True)],
}
PS_KINDS = {v: k for k, v in scripts._PS_CALL.items()}


def probes_of(step) -> list[tuple[str, tuple[str, ...]]]:
    """The probes a generated step runs, read back from its script."""
    if step.program == scripts.BASH:
        script = shlex.split(step.arguments)[1]
        calls = [m.group(1) for m in map(re.compile(r"^(.*) & p\d+=\$!$").match, script.splitlines()) if m]
        return [(parts[0], tuple(parts[1:])) for parts in map(shlex.split, calls)]
    assert step.program == scripts.POWERSHELL
    script = base64.b64decode(step.arguments.split()[-1]).decode("utf-16-le")
    out = []
    for call in re.findall(r"^\$c\d+ = Code \((.*)\)$", script, re.M):
        name = call.split()[0]
        args = re.findall(r"'((?:[^']|'')*)'", call)
        if name == "Tcp":
            args.append(call.split()[-1])
        out.append((PS_KINDS[name], tuple(args)))
    return out


def isolated_world(vm: str, kind: str, args: tuple[str, ...]) -> int | None:
    """A range whose edge drops everything but the depot's two ports."""
    if kind == "tcp":
        return 0 if args[0] == DEPOT and args[1] in ("8081", "3142") else 3
    return {"icmp": 1, "selftest": 0, "dns_int": 0, "dns_ext": 1, "route": 0, "v6route": 1, "v6global": 1}[kind]


class FakeGuest:
    """A backend with guest operations whose guests answer ``respond(vm, kind, args)``
    (None: that step times out). ``fail``: VMs whose guest operations fail."""

    supports_guest_commands = True

    def __init__(self, respond=isolated_world, fail=()):
        self.respond, self.fail, self.calls = respond, set(fail), []

    async def run_in_guest(self, vm_id, login, steps, timeout, *, range_id=None, vm_name="", identity=None):
        self.calls.append({"vm": vm_name, "user": login.username, "family": login.family, "timeout": timeout,
                           "steps": steps, "range_id": range_id, "password": login.password})
        if vm_name in self.fail:
            raise RuntimeError("guest operations not ready before the deadline: InvalidGuestLogin")
        out, stopped = [], False
        for st in steps:
            if stopped:
                out.append({"label": st.label, "status": "not_run", "exit_code": None})
                continue
            codes = [self.respond(vm_name, kind, args) for kind, args in probes_of(st)]
            if None in codes:
                out.append({"label": st.label, "status": "timeout", "exit_code": None})
                stopped = True
                continue
            out.append({"label": st.label, "status": "ok",
                        "exit_code": scripts.MARK + sum(c * 4 ** i for i, c in enumerate(codes))})
        return out


def _run(prov, output=None, template=None, accounts=ACCOUNTS, finish_by=None) -> dict:
    return asyncio.run(range_isolation.run(prov, RID, output or _output(), template or {}, accounts,
                                           finish_by=finish_by))


def _zone(rec: dict, name: str) -> dict:
    return next(z for z in rec["zones"] if z["zone"] == name)


def _check(zone: dict, check: str, target: str) -> dict:
    return next(c for c in zone["checks"] if c["check"] == check and c["target"] == target)


# ── Result classes ───────────────────────────────────────────────────────────────
def _c(kind, check="tcp", expect=range_isolation.MUST_FAIL, args=("10.30.30.1", "443")):
    return range_isolation.Check(check, "t", expect, scripts.Probe(kind, args if kind in ("tcp",) else ()))


@pytest.mark.parametrize("code,selftest,expected", [
    (0, True, "reachable"),
    (1, True, "connect_failed_only"),  # refused: the target answered, but it is never proof
    (2, True, "connect_failed_only"),  # no route / ICMP unreachable: a failed connect, never proof
    (3, True, "proven_unreachable"),  # timed out, and the probe itself works
    (3, False, "inconclusive"),  # timed out, but so might everything: the self-test failed
    (None, True, "inconclusive"),  # the guest operation failed or timed out
])
def test_tcp_result_classes(code, selftest, expected):
    assert range_isolation.classify(_c("tcp"), code, selftest) == expected


def test_a_refused_connect_is_never_proof_even_with_a_passing_selftest():
    for code in (1, 2):
        assert range_isolation.classify(_c("tcp"), code, True) != range_isolation.PROVEN


def test_icmp_route_and_dns_classes():
    icmp = range_isolation.Check("icmp", "h", "unreachable", scripts.Probe("icmp", ("10.30.30.1",)))
    assert [range_isolation.classify(icmp, c, True) for c in (0, 1, 2)] == ["reachable", "proven_unreachable",
                                                                           "inconclusive"]
    assert range_isolation.classify(icmp, 1, False) == "inconclusive"
    route = range_isolation.Check("route", "h", "unreachable", scripts.Probe("route", ("10.30.30.1",)))
    assert [range_isolation.classify(route, c, True) for c in (0, 1, 3)] == ["proven_unreachable", "reachable",
                                                                            "inconclusive"]
    ext = range_isolation.Check("dns_external", "example.com", "unreachable", scripts.Probe("dns_ext", ("example.com",)))
    assert range_isolation.classify(ext, 0, True) == "reachable"
    assert range_isolation.classify(ext, 2, True) == "proven_unreachable"  # resolved inside the range (Greyspace)
    assert range_isolation.classify(ext, 1, True, dns_ok=False) == "inconclusive", "the range DNS itself failed"
    assert range_isolation.classify(ext, 3, True) == "inconclusive"  # no resolver tool


@pytest.mark.parametrize("exit_code", [None, 0, 1, 2, 63, 128, 255])
def test_an_exit_code_outside_the_packed_range_carries_no_outcome(exit_code):
    assert scripts.decode(exit_code, 3) == [None, None, None]


def test_packing_round_trips():
    assert scripts.decode(64 + 3 + 4 * 1 + 16 * 2, 3) == [3, 1, 2]


# ── A whole range ────────────────────────────────────────────────────────────────
def test_an_isolated_range_probes_every_zone_and_is_isolated():
    prov = FakeGuest()
    rec = _run(prov)
    assert rec["verdict"] == "isolated", rec["missing"]
    assert rec["mode"] == "warn"
    assert {z["zone"]: z["status"] for z in rec["zones"]} == {"lan": "probed", "dmz": "probed"}
    for z in rec["zones"]:
        assert z["verdict"] == "isolated"
        assert all(c["result"] == "proven_unreachable" for c in z["checks"] if c["expect"] == "unreachable")
    assert {c["vm"] for c in prov.calls} == {"a1b2c3d4-web01", "a1b2c3d4-win01"}
    assert all(c["range_id"] == RID for c in prov.calls)


def test_a_zone_with_no_probe_keeps_the_verdict_from_isolated_and_names_the_zone():
    out = _output()
    out["vms"] = [v for v in out["vms"] if v["node_id"] != "win01"]  # nothing on dmz but the firewall
    rec = _run(FakeGuest(), out)
    assert rec["verdict"] == "incomplete"
    dmz = _zone(rec, "dmz")
    assert (dmz["status"], dmz["verdict"]) == ("no_probe", "incomplete")
    assert any(m.startswith("zone dmz:") for m in rec["missing"])
    assert "zone dmz" in rec["summary"]


def test_a_reachable_site_target_is_not_isolated():
    def leaky(vm, kind, args):
        return 0 if (vm.endswith("web01") and kind == "tcp" and args == ("10.30.30.20", "443")) else \
            isolated_world(vm, kind, args)

    rec = _run(FakeGuest(leaky))
    assert rec["verdict"] == "not_isolated"
    assert _check(_zone(rec, "lan"), "tcp", "10.30.30.20:443")["result"] == "reachable"
    assert rec["failures"] == ["zone lan: reachable outside its exceptions: tcp 10.30.30.20:443"]


def test_a_failed_selftest_makes_silence_inconclusive():
    def blind(vm, kind, args):
        return 1 if kind == "selftest" else isolated_world(vm, kind, args)

    rec = _run(FakeGuest(blind))
    lan = _zone(rec, "lan")
    assert lan["selftest"]["passed"] is False
    assert all(c["result"] == "inconclusive" for c in lan["checks"] if c["check"] in ("tcp", "icmp", "closed_port"))
    assert lan["verdict"] == "incomplete" and rec["verdict"] == "incomplete"
    assert any("self-test failed" in r for r in lan["reasons"])


def test_refused_connects_are_never_isolated():
    def rejecting(vm, kind, args):
        return 1 if kind == "tcp" and args[0] != DEPOT else isolated_world(vm, kind, args)

    rec = _run(FakeGuest(rejecting))
    assert rec["verdict"] == "incomplete"
    assert _check(_zone(rec, "lan"), "tcp", "10.30.30.1:443")["result"] == "connect_failed_only"


def test_a_guest_operation_failure_tries_the_next_candidate_then_is_inconclusive():
    out = _output()
    out["vms"].append({"vm_id": "vm-web2", "name": "a1b2c3d4-web02", "node_id": "web02", "family": "linux",
                       "tools_ready": True, "nics": [{"network": "tn-a1b2c3d4-101", "ip": "10.80.0.21"}]})
    accounts = {**ACCOUNTS, "web02": [RangeAccount("student", Secret("x-pw-web02"), False)]}
    prov = FakeGuest(fail={"a1b2c3d4-web01"})
    rec = _run(prov, out, accounts=accounts)
    assert _zone(rec, "lan")["probe"]["vm"] == "a1b2c3d4-web02"
    prov = FakeGuest(fail={"a1b2c3d4-web01", "a1b2c3d4-web02"})
    rec = _run(prov, out, accounts=accounts)
    lan = _zone(rec, "lan")
    assert (lan["status"], lan["verdict"]) == ("guest_op_failed", "incomplete")
    assert len([c for c in prov.calls if c["vm"].endswith(("web01", "web02"))]) == range_isolation.ATTEMPTS_PER_ZONE


def test_a_step_that_times_out_leaves_its_checks_and_the_rest_inconclusive():
    def slow(vm, kind, args):
        return None if kind == "dns_ext" else isolated_world(vm, kind, args)

    rec = _run(FakeGuest(slow))
    lan = _zone(rec, "lan")
    assert _check(lan, "dns_external", "example.com")["result"] == "inconclusive"
    assert lan["verdict"] == "incomplete" and lan["step_errors"]


def test_no_site_targets_is_never_isolated(monkeypatch):
    monkeypatch.setenv("TN_RANGE_ISOLATION_PROBE_TARGETS", "")
    rec = _run(FakeGuest())
    assert rec["verdict"] == "incomplete"
    assert "no site probe targets configured (tn_range_isolation_probe_targets)" in rec["missing"]


def test_a_backend_without_guest_operations_records_every_zone_without_a_probe():
    class Mock:
        supports_guest_commands = False

    rec = _run(Mock())
    assert {z["status"] for z in rec["zones"]} == {"no_probe"} and rec["verdict"] == "incomplete"


def test_a_malformed_setting_is_recorded_not_raised(monkeypatch):
    monkeypatch.setenv("TN_RANGE_ISOLATION_PROBE_TARGETS", "10.30.30.1:443;rm -rf /")
    rec = _run(FakeGuest())
    assert rec["verdict"] == "incomplete" and rec["settings"] is None
    assert {z["status"] for z in rec["zones"]} == {"not_run"}


# ── Exceptions ───────────────────────────────────────────────────────────────────
def test_the_depot_exceptions_must_connect_and_every_other_depot_port_must_not():
    rec = _run(FakeGuest())
    lan = _zone(rec, "lan")
    exc = {c["target"]: c for c in lan["checks"] if c["check"] == "exception"}
    assert set(exc) == {f"{DEPOT}:8081", f"{DEPOT}:3142"}
    assert all(c["expect"] == "reachable" and c["result"] == "reachable" and c["passed"] for c in exc.values())
    closed = {c["target"] for c in lan["checks"] if c["check"] == "closed_port"}
    assert closed == {f"{DEPOT}:{p}" for p in (22, 80, 443, 8080)}
    assert _check(lan, "icmp", DEPOT)["expect"] == "unreachable"


def test_an_exception_that_does_not_connect_is_not_isolated():
    def no_depot(vm, kind, args):
        return 3 if kind == "tcp" and args == (DEPOT, "3142") else isolated_world(vm, kind, args)

    rec = _run(FakeGuest(no_depot))
    assert rec["verdict"] == "incomplete"
    assert any(f"permitted exception did not connect: {DEPOT}:3142" in r for r in _zone(rec, "lan")["reasons"])


def test_another_depot_port_answering_is_not_isolated():
    def open_ssh(vm, kind, args):
        return 0 if kind == "tcp" and args == (DEPOT, "22") else isolated_world(vm, kind, args)

    rec = _run(FakeGuest(open_ssh))
    assert rec["verdict"] == "not_isolated"
    assert _check(_zone(rec, "lan"), "closed_port", f"{DEPOT}:22")["result"] == "reachable"


def test_exceptions_come_from_the_setting(monkeypatch):
    monkeypatch.setenv("TN_RANGE_ISOLATION_EXCEPTIONS", "10.30.32.11:9000")
    rec = _run(FakeGuest(lambda vm, kind, args: 0 if args[:2] == ("10.30.32.11", "9000")
                         else isolated_world(vm, kind, args) if args[:1] != ("10.30.32.11",) else 3))
    lan = _zone(rec, "lan")
    assert [c["target"] for c in lan["checks"] if c["check"] == "exception"] == ["10.30.32.11:9000"]


# ── IPv6 ─────────────────────────────────────────────────────────────────────────
def test_no_ipv6_route_or_address_proves_there_is_no_ipv6_path():
    lan = _zone(_run(FakeGuest()), "lan")
    assert lan["ipv6"] == {"route": "absent", "global": "absent"}
    assert _check(lan, "ipv6_egress", "IPv6")["result"] == "proven_unreachable"


def test_ipv6_present_without_site_v6_targets_is_inconclusive():
    def v6(vm, kind, args):
        return 0 if kind in ("v6route", "v6global") else isolated_world(vm, kind, args)

    rec = _run(FakeGuest(v6))
    lan = _zone(rec, "lan")
    assert lan["ipv6"] == {"route": "present", "global": "present"}
    egress = _check(lan, "ipv6_egress", "IPv6")
    assert egress["result"] == "inconclusive" and "#238" in egress["note"]
    assert rec["verdict"] == "incomplete"


def test_site_ipv6_targets_decide_when_ipv6_is_present(monkeypatch):
    monkeypatch.setenv("TN_RANGE_ISOLATION_PROBE_TARGETS", SITE + ",[fd00:30::20]:443")

    def v6(vm, kind, args):
        return 0 if kind in ("v6route", "v6global") else isolated_world(vm, kind, args)

    lan = _zone(_run(FakeGuest(v6)), "lan")
    assert _check(lan, "tcp", "[fd00:30::20]:443")["result"] == "proven_unreachable"
    assert not [c for c in lan["checks"] if c["check"] == "ipv6_egress"]
    assert lan["verdict"] == "isolated"


def test_a_failed_v6_connect_counts_as_proof_only_when_the_guest_has_no_ipv6(monkeypatch):
    monkeypatch.setenv("TN_RANGE_ISOLATION_PROBE_TARGETS", SITE + ",[fd00:30::20]:443")

    def unreachable_v6(vm, kind, args):
        return 2 if kind == "tcp" and args[0].startswith("fd00") else isolated_world(vm, kind, args)

    lan = _zone(_run(FakeGuest(unreachable_v6)), "lan")
    row = _check(lan, "tcp", "[fd00:30::20]:443")
    assert (row["result"], row["note"]) == ("proven_unreachable", "no IPv6 route or global address")


# ── Probe VMs: Windows, the probe flag, least privilege ──────────────────────────
def test_a_zone_with_only_a_windows_vm_is_probed_with_powershell():
    prov = FakeGuest()
    rec = _run(prov)
    dmz = _zone(rec, "dmz")
    assert dmz["probe"] == {"vm": "a1b2c3d4-win01", "family": "windows", "account": "Administrator", "admin": True,
                            "flagged": False}
    (call,) = [c for c in prov.calls if c["vm"] == "a1b2c3d4-win01"]
    assert {s.program for s in call["steps"]} == {scripts.POWERSHELL}
    assert all("-EncodedCommand" in s.arguments and s.ok_codes == scripts.EVERY_CODE for s in call["steps"])
    assert dmz["verdict"] == "isolated"


def test_the_least_privileged_declared_account_is_used():
    prov = FakeGuest()
    rec = _run(prov)
    assert _zone(rec, "lan")["probe"]["account"] == "student"
    assert _zone(rec, "lan")["probe"]["admin"] is False
    assert [c["user"] for c in prov.calls if c["vm"].endswith("web01")] == ["student"]


def test_a_template_probe_node_is_preferred():
    out = _output()
    out["vms"].append({"vm_id": "vm-p", "name": "a1b2c3d4-probe", "node_id": "probe", "family": "windows",
                       "tools_ready": True, "nics": [{"network": "tn-a1b2c3d4-101", "ip": "10.80.0.99"}]})
    accounts = {**ACCOUNTS, "probe": [RangeAccount("tnprobe", Secret("probe-pw-9"), False)]}
    rec = _run(FakeGuest(), out, template={"nodes": [{"id": "probe", "probe": True}]}, accounts=accounts)
    assert _zone(rec, "lan")["probe"]["vm"] == "a1b2c3d4-probe"
    assert _zone(rec, "lan")["probe"]["flagged"] is True


def test_no_password_ever_reaches_the_record_or_the_log(caplog):
    caplog.set_level("DEBUG")
    prov = FakeGuest(fail={"a1b2c3d4-win01"})
    rec = _run(prov)
    blob = json.dumps(rec) + caplog.text + "".join(s.arguments for c in prov.calls for s in c["steps"])
    assert not any(p in blob for p in PASSWORDS)
    assert {c["password"] for c in prov.calls} <= set(PASSWORDS)  # handed to the guest channel only


# ── Bounded, no artifacts, no injection ──────────────────────────────────────────
def test_the_budget_bounds_every_guest_operation(monkeypatch):
    monkeypatch.setenv("TN_RANGE_ISOLATION_ZONE_TIMEOUT", "90")
    prov = FakeGuest()
    _run(prov, finish_by=time.monotonic() + 30 + 60)  # 60 s left once the build's own 30 s are kept back
    assert prov.calls and all(c["timeout"] <= 60 for c in prov.calls)


def test_a_spent_budget_leaves_zones_unprobed(monkeypatch):
    prov = FakeGuest()
    rec = _run(prov, finish_by=time.monotonic() + 35)  # less than a zone's minimum after the 30 s reserve
    assert not prov.calls
    assert {z["status"] for z in rec["zones"]} == {"budget_exhausted"} and rec["verdict"] == "incomplete"


def test_scripts_write_no_files():
    probes = [scripts.Probe("tcp", ("10.30.30.1", "443")), scripts.Probe("dns_ext", ("example.com", "10.0.0.0/8")),
              scripts.Probe("route", ("10.30.30.1", "10.80.0.1"))]
    bash = scripts.linux_script(probes, 2)
    # The only redirections: to /dev/null, fd duplication, and the probe's own TCP socket.
    rest = bash.replace(">/dev/null", "").replace("2>&1", "").replace("3<>/dev/tcp/", "")
    assert ">" not in rest, "a redirection to a file"
    assert "<<" not in rest.replace("<<(32-b)", "").replace("<<24", "").replace("<<16", "").replace("<<8", "")
    assert "mktemp" not in bash and "/tmp" not in bash and "tee " not in bash
    ps = scripts.windows_script(probes, 2)
    for cmdlet in ("Out-File", "Set-Content", "Add-Content", "New-Item", "Start-Transcript", "Export-"):
        assert cmdlet not in ps


@pytest.mark.parametrize("bad", ["10.30.30.1;reboot", "$(id)", "a b", "x'y", "10.30.30.1/24/1"])
def test_probe_arguments_are_validated(bad):
    with pytest.raises(ValueError):
        scripts.Probe("icmp", (bad,))
    with pytest.raises(range_isolation.SettingsError):
        range_isolation.parse_target(bad + ":443")


# ── warn vs enforce ──────────────────────────────────────────────────────────────
def test_the_default_mode_is_warn(monkeypatch):
    monkeypatch.delenv("RANGE_ISOLATION_CHECK", raising=False)
    assert range_isolation.mode() == ("warn", "")
    monkeypatch.setenv("RANGE_ISOLATION_CHECK", "strict")
    assert range_isolation.mode()[0] == "warn"


def _result(isolation: dict) -> ProvisionResult:
    return ProvisionResult(status="ok", vms=_output()["vms"], networks=_output()["networks"], isolation=isolation)


def test_warn_records_a_weak_result_and_the_range_is_ready():
    rec = _run(FakeGuest(lambda vm, kind, args: 0 if kind == "tcp" else isolated_world(vm, kind, args)))
    assert rec["verdict"] == "not_isolated" and rec["mode"] == "warn"
    assert range_alloc.build_outcome(_result(rec)) == ("ready", None)


def test_enforce_fails_a_range_that_is_not_proven_isolated(monkeypatch):
    monkeypatch.setenv("RANGE_ISOLATION_CHECK", "enforce")
    out = _output()
    out["vms"] = [v for v in out["vms"] if v["node_id"] != "win01"]
    rec = _run(FakeGuest(), out)
    state, error = range_alloc.build_outcome(_result(rec))
    assert state == "failed"
    assert error.startswith("isolation not proven (RANGE_ISOLATION_CHECK=enforce): incomplete") and "zone dmz" in error
    isolated = _run(FakeGuest())
    assert range_alloc.build_outcome(_result(isolated)) == ("ready", None)


def test_enforce_fails_the_build_end_to_end(monkeypatch, lease_always_free):
    """provision_range with a backend that builds the range and probes it: enforce + a leak -> failed."""
    from worker import range_accounts, tasks

    monkeypatch.setenv("RANGE_ISOLATION_CHECK", "enforce")
    states = []
    monkeypatch.setattr(tasks, "_update_range_state", lambda rid, s, **kw: states.append((s, kw)) or 1)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_db_session", _NoDb)
    monkeypatch.setattr(tasks.db_ops, "range_template_and_backend", lambda db, rid: ("{}", "vsphere_api"))
    monkeypatch.setattr(tasks.range_alloc, "reserve_for_build", lambda *a: {})
    monkeypatch.setattr(range_accounts, "attach", lambda template, session, rid: template)
    monkeypatch.setattr(range_accounts, "load", lambda session, rid: ACCOUNTS)

    class Built(FakeGuest):
        async def provision(self, range_id, template, allocations):
            return ProvisionResult(status="ok", vms=_output()["vms"], networks=_output()["networks"])

    leak = Built(lambda vm, kind, args: 1 if kind == "route" else isolated_world(vm, kind, args))  # crosses the edge
    monkeypatch.setattr(tasks, "_get_backend", lambda b, range_id=None, **kw: leak)
    assert tasks.provision_range.run(RID)["status"] == "failed"
    assert "ready" not in [s for s, _ in states]
    kw = next(kw for s, kw in states if s == "failed" and kw.get("output"))  # recorded with its VMs (destroy needs them)
    assert "RANGE_ISOLATION_CHECK=enforce" in kw["error"]
    assert json.loads(kw["output"])["isolation"]["verdict"] == "not_isolated"


class _NoDb:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_not_ready_build_is_not_probed():
    prov = FakeGuest()
    result = ProvisionResult(status="partial", vms=_output()["vms"], networks=_output()["networks"],
                             not_ready=["a1b2c3d4-web01"])
    asyncio.run(range_isolation.after_build(prov, RID, {}, result, {}, None))
    assert not prov.calls and {z["status"] for z in result.isolation["zones"]} == {"not_run"}


# ── The re-probe task ────────────────────────────────────────────────────────────
@pytest.fixture
def ready_range(db, monkeypatch):
    from worker import range_accounts

    rid = str(uuid.uuid4())
    with db.begin() as conn:
        conn.execute(sa.text("INSERT INTO templates (id, yaml) VALUES ('t1', 'nodes: [{id: web01, probe: true}]')"))
        conn.execute(sa.text("INSERT INTO ranges (id, name, template_id, state, provisioner_backend, provisioner_output) "
                             "VALUES (:id, 'r', 't1', 'ready', 'vsphere_api', :o)"),
                     {"id": rid, "o": json.dumps(_output())})
    monkeypatch.setattr(range_accounts, "load", lambda session, r: ACCOUNTS)
    return rid


def _stored(db, rid):
    with db.connect() as conn:
        return json.loads(conn.execute(sa.text("SELECT provisioner_output FROM ranges WHERE id = :id"),
                                       {"id": rid}).scalar())


def _lock_factory(fake_redis):
    from worker.redis_lock import RedisLock

    return lambda rid: RedisLock(fake_redis, isolation_task.LOCK_KEY.format(rid), 600_000)


def test_the_reprobe_stores_new_evidence_and_never_changes_the_state(db, ready_range, monkeypatch, fake_redis):
    monkeypatch.setenv("RANGE_ISOLATION_CHECK", "enforce")
    prov = FakeGuest(lambda vm, kind, args: 0 if kind == "icmp" else isolated_world(vm, kind, args))
    monkeypatch.setattr(isolation_task, "_get_backend", lambda b, rid, **kw: prov)
    got = isolation_task.check(ready_range, _lock_factory(fake_redis))
    assert (got["status"], got["verdict"]) == ("stored", "not_isolated")
    assert _stored(db, ready_range)["isolation"]["verdict"] == "not_isolated"
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT state FROM ranges WHERE id = :id"), {"id": ready_range}).scalar() == "ready"


def test_the_reprobe_skips_a_range_that_is_not_ready(db, ready_range, fake_redis):
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE ranges SET state = 'destroying' WHERE id = :id"), {"id": ready_range})
    assert isolation_task.check(ready_range, _lock_factory(fake_redis))["status"] == "skipped"


def test_one_reprobe_per_range_at_a_time(db, ready_range, fake_redis):
    fake_redis.set(isolation_task.LOCK_KEY.format(ready_range), "someone-else", nx=True, px=600_000)
    got = isolation_task.check(ready_range, _lock_factory(fake_redis))
    assert got["status"] == "skipped" and "another isolation probe" in got["reason"]


def test_a_reprobe_finishing_after_a_rebuild_is_discarded(db, ready_range, monkeypatch, fake_redis):
    class Rebuilt(FakeGuest):
        async def run_in_guest(self, *a, **k):
            with db.begin() as conn:  # the range is rebuilt while it is being probed: other VMs
                conn.execute(sa.text("UPDATE ranges SET provisioner_output = :o WHERE id = :id"),
                             {"o": json.dumps({**_output(), "vms": [{"vm_id": "vm-new", "name": "n"}]}),
                              "id": ready_range})
            return await super().run_in_guest(*a, **k)

    monkeypatch.setattr(isolation_task, "_get_backend", lambda b, rid, **kw: Rebuilt())
    got = isolation_task.check(ready_range, _lock_factory(fake_redis))
    assert got == {"status": "discarded", "reason": "the range was rebuilt while it was probed",
                   "verdict": "isolated"}
    assert "isolation" not in _stored(db, ready_range)


def test_an_older_probe_never_overwrites_a_newer_one(db, ready_range):
    from worker import db_ops, tasks

    vm_ids = {"vm-fw", "vm-web", "vm-win"}
    with tasks._db_session() as s:
        assert db_ops.store_isolation(s, ready_range, {"started_at": "2026-10-22T10:00:00+00:00", "n": 2},
                                      vm_ids) == "stored"
    with tasks._db_session() as s:
        assert db_ops.store_isolation(s, ready_range, {"started_at": "2026-10-22T09:00:00+00:00", "n": 1},
                                      vm_ids) == "a later probe has stored its evidence"
    assert _stored(db, ready_range)["isolation"]["n"] == 2
