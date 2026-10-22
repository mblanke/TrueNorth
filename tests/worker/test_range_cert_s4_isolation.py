"""Range certification S4 (ADR 0013 §4, issue #242): isolation evidence per security zone.

* result classes: a refused connect is ``connect_failed_only`` (never proof), a timeout is
  ``proven_unreachable`` only with a passing self-test, a failed self-test or guest
  operation makes everything ``inconclusive``;
* per-zone aggregation: a zone without a probe keeps the verdict from Isolated and is named;
* exceptions: the depot's 8081 and 3142 must connect, every other depot port must not;
* IPv6 present / absent (a ULA address or route is present); multi-homed probe VMs
  (ranked after single-homed ones; a target routed out of another NIC is not measured);
  the Windows (PowerShell) probe path; staff-only accounts first; budget;
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
import os
import re
import shlex
import stat
import subprocess
import time
import uuid

import pytest
import sqlalchemy as sa
from worker import isolation_scripts as scripts
from worker import isolation_task, range_alloc, range_isolation
from worker.provisioners.results import ProvisionResult
from worker.provisioners.vsphere_guest import Secret
from worker.range_isolation import ProbeAccount

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


def staff(name, pw, admin=True):
    return ProbeAccount(name, Secret(pw), admin, student_visible=False)


def student(name, pw):
    return ProbeAccount(name, Secret(pw), False, student_visible=True)


ACCOUNTS = {
    "web01": [staff("soadmin", PASSWORDS[1]), student("student", PASSWORDS[0])],
    "win01": [staff("Administrator", PASSWORDS[2])],
}
PS_KINDS = {v: k for k, v in scripts._PS_CALL.items()}


def probes_of(step) -> list[tuple[str, tuple[str, ...]]]:
    """The probes a generated step runs, read back from its script."""
    if step.program == scripts.BASH:
        wrapper = shlex.split(step.arguments)[1]
        script = base64.b64decode(re.search(r"printf %s (\S+) \| base64 -d", wrapper).group(1)).decode()
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
    return {"icmp": 1, "selftest": 0, "dns_int": 0, "dns_ext": 1, "route": 0, "v6route": 1, "v6addr": 1}[kind]


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
            out.append({"label": st.label, "status": "ok", "exit_code": scripts.pack(codes)})
        return out

    async def stop(self, range_id, output):
        from worker.provisioners.results import StopResult

        self.stopped = [v["vm_id"] for v in output["vms"]]
        return StopResult(status="ok", vms_stopped=len(self.stopped))


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
    (5, True, "inconclusive"),  # multi-homed: the route to it leaves by another NIC
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
    assert [range_isolation.classify(icmp, c, True) for c in (0, 1, 2, 3, 4, 5)] == [
        "reachable", "proven_unreachable", "inconclusive",
        "connect_failed_only",  # an "unreachable" from the range's own edge: rejected there
        "reachable",  # an "unreachable" from a router outside the range: the packet got out
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


@pytest.mark.parametrize("exit_code", [None, 0, 1, 2, 63, 126, 127, 128, 137, 255, 64 + 7, 64 + 7 * 8])
def test_an_exit_code_outside_the_packed_range_carries_no_outcome(exit_code):
    """Including bash's own 126/127 and a killed shell's 128+: a digit of 7 is never a code."""
    assert scripts.decode(exit_code, 2) == [None, None]


def test_packing_round_trips():
    assert scripts.decode(scripts.pack([3, 5]), 2) == [3, 5]
    assert scripts.decode(scripts.pack([6]), 1) == [6]
    assert scripts.decode(scripts.pack([1, 2]), 1) == [None], "a digit beyond the step's probes is no outcome"


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
    accounts = {**ACCOUNTS, "web02": [student("student", "x-pw-web02")]}
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
    assert lan["ipv6"] == {"route": "absent", "address": "absent"}
    assert _check(lan, "ipv6_egress", "IPv6")["result"] == "proven_unreachable"


def test_ipv6_present_without_site_v6_targets_is_inconclusive():
    def v6(vm, kind, args):
        return 0 if kind in ("v6route", "v6addr") else isolated_world(vm, kind, args)

    rec = _run(FakeGuest(v6))
    lan = _zone(rec, "lan")
    assert lan["ipv6"] == {"route": "present", "address": "present"}
    egress = _check(lan, "ipv6_egress", "IPv6")
    assert egress["result"] == "inconclusive" and "#238" in egress["note"]
    assert rec["verdict"] == "incomplete"


def test_site_ipv6_targets_decide_when_ipv6_is_present(monkeypatch):
    monkeypatch.setenv("TN_RANGE_ISOLATION_PROBE_TARGETS", SITE + ",[fd00:30::20]:443")

    def v6(vm, kind, args):
        return 0 if kind in ("v6route", "v6addr") else isolated_world(vm, kind, args)

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
    assert (row["result"], row["note"]) == ("proven_unreachable", "no IPv6 route and no IPv6 address")


def test_an_ipv6_address_alone_ula_included_means_ipv6_is_present():
    """Review of #267: a ULA (fd00::/8) address with no default route is still IPv6."""
    def ula(vm, kind, args):
        return 0 if kind == "v6addr" else isolated_world(vm, kind, args)

    lan = _zone(_run(FakeGuest(ula)), "lan")
    assert lan["ipv6"] == {"route": "absent", "address": "present"}
    assert _check(lan, "ipv6_egress", "IPv6")["result"] == "inconclusive"
    assert lan["verdict"] == "incomplete"


# ── Probe VMs: Windows, the probe flag, least privilege ──────────────────────────
def test_a_zone_with_only_a_windows_vm_is_probed_with_powershell():
    prov = FakeGuest()
    rec = _run(prov)
    dmz = _zone(rec, "dmz")
    assert dmz["probe"] == {"vm": "a1b2c3d4-win01", "family": "windows", "account": "Administrator", "admin": True,
                            "account_class": "staff", "flagged": False, "multi_homed": False, "source_ip": "",
                            "trusted": False}
    (call,) = [c for c in prov.calls if c["vm"] == "a1b2c3d4-win01"]
    assert {s.program for s in call["steps"]} == {scripts.POWERSHELL}
    assert all("-EncodedCommand" in s.arguments and s.ok_codes == scripts.EVERY_CODE for s in call["steps"])
    assert dmz["verdict"] == "isolated"


def test_a_staff_only_account_is_preferred_over_a_student_visible_one():
    """Review of #267: Students control the accounts they can reveal."""
    prov = FakeGuest()
    rec = _run(prov)
    probe = _zone(rec, "lan")["probe"]
    assert (probe["account"], probe["account_class"]) == ("soadmin", "staff")
    assert [c["user"] for c in prov.calls if c["vm"].endswith("web01")] == ["soadmin"]
    assert "lan: a1b2c3d4-web01 using a staff account" in rec["summary"]


def test_among_staff_accounts_the_least_privileged_is_used():
    accounts = {**ACCOUNTS, "web01": [staff("soadmin", PASSWORDS[1]), staff("tnprobe", "probe-pw", admin=False),
                                      student("student", PASSWORDS[0])]}
    out = _output()
    out["vms"][1]["accounts_applied"].append("tnprobe")  # verified by the build, like soadmin
    probe = _zone(_run(FakeGuest(), out, accounts=accounts), "lan")["probe"]
    assert (probe["account"], probe["admin"]) == ("tnprobe", False)


def test_a_student_visible_account_is_used_only_when_there_is_no_other():
    accounts = {**ACCOUNTS, "web01": [student("student", PASSWORDS[0])]}
    probe = _zone(_run(FakeGuest(), accounts=accounts), "lan")["probe"]
    assert (probe["account"], probe["account_class"], probe["trusted"]) == ("student", "student_visible", False)


# ── Multi-homed probe VMs (review of #267) ───────────────────────────────────────
def _multi(out):
    """web01 also on dmz: multi-homed; and a single-homed Windows VM on lan."""
    web = next(v for v in out["vms"] if v["node_id"] == "web01")
    web["nics"].append({"network": "tn-a1b2c3d4-102", "ip": "10.80.1.20"})
    out["vms"].append({"vm_id": "vm-w2", "name": "a1b2c3d4-win02", "node_id": "win02", "family": "windows",
                       "tools_ready": True, "nics": [{"network": "tn-a1b2c3d4-101", "ip": "10.80.0.40"}]})
    return out


def test_a_single_homed_vm_is_ranked_above_a_multi_homed_one_of_a_better_family():
    accounts = {**ACCOUNTS, "win02": [staff("Administrator", "w2-pw")]}
    lan = _zone(_run(FakeGuest(), _multi(_output()), accounts=accounts), "lan")
    assert lan["probe"]["vm"] == "a1b2c3d4-win02" and lan["probe"]["multi_homed"] is False


def test_a_multi_homed_probe_is_pinned_to_its_zone_address():
    prov = FakeGuest()
    out = _multi(_output())
    out["vms"] = [v for v in out["vms"] if v["node_id"] != "win02"]
    lan = _zone(_run(prov, out), "lan")
    assert (lan["probe"]["multi_homed"], lan["probe"]["source_ip"]) == (True, "10.80.0.20")
    (call,) = [c for c in prov.calls if c["vm"].endswith("web01") and c is prov.calls[0]]
    script = base64.b64decode(re.search(r"printf %s (\S+) \|", shlex.split(call["steps"][0].arguments)[1])
                              .group(1)).decode()
    assert "SRC=10.80.0.20" in script


def test_targets_a_multi_homed_probe_reaches_by_another_nic_are_not_measured():
    """The guest reports 5 for every probe whose route leaves by the other NIC."""
    out = _multi(_output())
    out["vms"] = [v for v in out["vms"] if v["node_id"] != "win02"]

    def other_nic(vm, kind, args):
        return 5 if vm.endswith("web01") and kind in ("tcp", "icmp", "route", "dns_ext") else isolated_world(vm, kind, args)

    lan = _zone(_run(FakeGuest(other_nic), out), "lan")
    row = _check(lan, "tcp", "10.30.30.1:443")
    assert row["result"] == "inconclusive" and "another NIC" in row["note"]
    assert lan["verdict"] == "incomplete"


# ── The real bash, against a stubbed ``ip`` (needs bash and coreutils timeout) ────
STUB_IP = r"""#!/bin/sh
case "$*" in
  "-o addr show to 10.80.0.20") echo "2: eth0    inet 10.80.0.20/24 brd 10.80.0.255 scope global eth0";;
  "route get 10.30.30.1") echo "10.30.30.1 via 10.80.1.1 dev eth1 src 10.80.1.20 uid 1000";;
  "-6 route show table all") printf '%s
' "$IP6ROUTES";;
  "-6 addr show") printf '%s
' "$IP6ADDRS";;
esac
"""


def _bash(tmp_path, probes, ctx=scripts.Context(), **env):
    stub = tmp_path / "ip"
    stub.write_text(STUB_IP)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    script = scripts.linux_script(probes, 1, ctx)
    run = subprocess.run(["bash", "-c", script], env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", **env},
                         capture_output=True, timeout=30)
    return scripts.decode(run.returncode, len(probes))


@pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="needs bash")
def test_bash_pin_refuses_a_target_routed_out_of_another_nic(tmp_path):
    ctx = scripts.Context(src="10.80.0.20", range_addresses=("10.80.0.1",))
    assert _bash(tmp_path, [scripts.Probe("tcp", ("10.30.30.1", "443")), scripts.Probe("icmp", ("10.30.30.1",))],
                 ctx) == [5, 5]


@pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="needs bash")
@pytest.mark.parametrize("routes,addrs,expected", [
    ("fe80::/64 dev eth0 proto kernel metric 256\nlocal ::1 dev lo table local\nmulticast ff00::/8 dev eth0 table local",
     "    inet6 ::1/128 scope host\n    inet6 fe80::1/64 scope link", [1, 1]),  # link-local only: absent
    ("fe80::/64 dev eth0 proto kernel", "    inet6 fd00:80::20/64 scope global\n    inet6 fe80::1/64 scope link",
     [1, 0]),  # a ULA address: present
    ("fd00:80::/64 dev eth0 proto kernel\nfe80::/64 dev eth0", "    inet6 fe80::1/64 scope link",
     [0, 1]),  # a ULA route: present
    ("default via fe80::1 dev eth0 proto ra", "    inet6 2001:db8::5/64 scope global", [0, 0]),
])
def test_bash_ipv6_presence_counts_ula(tmp_path, routes, addrs, expected):
    got = _bash(tmp_path, [scripts.Probe("v6route"), scripts.Probe("v6addr")],
                IP6ROUTES=routes, IP6ADDRS=addrs)
    assert got == expected


def test_a_template_probe_node_is_preferred():
    out = _output()
    out["vms"].append({"vm_id": "vm-p", "name": "a1b2c3d4-probe", "node_id": "probe", "family": "windows",
                       "tools_ready": True, "nics": [{"network": "tn-a1b2c3d4-101", "ip": "10.80.0.99"}]})
    accounts = {**ACCOUNTS, "probe": [student("tnprobe", "probe-pw-9")]}
    rec = _run(FakeGuest(), out, template={"nodes": [{"id": "probe", "probe": True}]}, accounts=accounts)
    assert _zone(rec, "lan")["probe"]["vm"] == "a1b2c3d4-probe"
    assert _zone(rec, "lan")["probe"]["flagged"] is True
    assert _zone(rec, "lan")["probe"]["trusted"] is False, "a Student-visible account is never trusted"


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
    probes = [scripts.Probe("tcp", ("10.30.30.1", "443")), scripts.Probe("route", ("10.30.30.1",))]
    ctx = scripts.Context("10.80.0.20", ("10.80.0.1",), ("10.80.0.0/24",))
    bash = scripts.linux_script(probes, 2, ctx)
    # The only redirections: to /dev/null, fd duplication, and the probe's own TCP socket.
    rest = bash.replace(">/dev/null", "").replace("2>&1", "").replace("3<>/dev/tcp/", "")
    assert ">" not in rest, "a redirection to a file"
    assert "<<" not in rest.replace("<<(32-b)", "").replace("<<24", "").replace("<<16", "").replace("<<8", "")
    assert "mktemp" not in bash and "/tmp" not in bash and "tee " not in bash
    ps = scripts.windows_script(probes, 2, ctx)
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
    monkeypatch.setattr(range_isolation, "load_accounts", lambda session, rid: ACCOUNTS)

    class Built(FakeGuest):
        async def provision(self, range_id, template, allocations):
            return ProvisionResult(status="ok", vms=_output()["vms"], networks=_output()["networks"])

    leak = Built(lambda vm, kind, args: 1 if kind == "route" else isolated_world(vm, kind, args))  # crosses the edge
    monkeypatch.setattr(tasks, "_get_backend", lambda b, range_id=None, **kw: leak)
    assert tasks.provision_range.run(RID)["status"] == "failed"
    assert "ready" not in [s for s, _ in states]
    kw = next(kw for s, kw in states if s == "failed" and kw.get("output"))  # recorded with its VMs (destroy needs them)
    assert "RANGE_ISOLATION_CHECK=enforce" in kw["error"]
    stored = json.loads(kw["output"])["isolation"]
    assert stored["verdict"] == "not_isolated"
    # Enforce powers the range off rather than leave it running on a path out.
    assert stored["enforced"] == {"action": "powered_off", "status": "ok", "vms_stopped": 3, "errors": []}
    assert sorted(leak.stopped) == ["vm-fw", "vm-web", "vm-win"]


def test_warn_never_powers_anything_off(monkeypatch):
    prov = FakeGuest(lambda vm, kind, args: 1 if kind == "route" else isolated_world(vm, kind, args))
    result = ProvisionResult(status="ok", vms=_output()["vms"], networks=_output()["networks"])
    monkeypatch.setattr(range_isolation, "load_accounts", lambda session, rid: ACCOUNTS)
    asyncio.run(range_isolation.after_build(prov, RID, {}, result, {}, None))
    assert result.isolation["verdict"] == "not_isolated" and "enforced" not in result.isolation
    assert not hasattr(prov, "stopped")


def test_the_noise_channel_exclusion_is_stated_with_the_verdict():
    out = _output()
    out["networks"].append({"name": "noise_mgmt", "cidr": "172.31.0.0/24", "gateway": ""})
    rec = _run(FakeGuest(), out)
    assert [z["zone"] for z in rec["zones"]] == ["lan", "dmz"]
    assert rec["excluded"] and rec["excluded"][0].startswith("noise_mgmt (out-of-band")
    assert "not probed: noise_mgmt" in rec["summary"]


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

    rid = str(uuid.uuid4())
    with db.begin() as conn:
        conn.execute(sa.text("INSERT INTO templates (id, yaml) VALUES ('t1', 'nodes: [{id: web01, probe: true}]')"))
        conn.execute(sa.text("INSERT INTO ranges (id, name, template_id, state, provisioner_backend, provisioner_output) "
                             "VALUES (:id, 'r', 't1', 'ready', 'vsphere_api', :o)"),
                     {"id": rid, "o": json.dumps(_output())})
    monkeypatch.setattr(range_isolation, "load_accounts", lambda session, r: ACCOUNTS)
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
    stored = _stored(db, ready_range)
    assert stored["isolation_reprobe"]["verdict"] == "not_isolated"
    assert stored["isolation_reprobe"]["source"] == "reprobe"
    assert "isolation" not in stored, "the build's own evidence is never overwritten"
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
    assert "isolation_reprobe" not in _stored(db, ready_range)


def test_an_older_probe_never_overwrites_a_newer_one(db, ready_range):
    from worker import db_ops, tasks

    vm_ids = {"vm-fw", "vm-web", "vm-win"}
    with tasks._db_session() as s:
        assert db_ops.store_isolation(s, ready_range, {"started_at": "2026-10-22T10:00:00+00:00", "n": 2},
                                      vm_ids) == "stored"
    with tasks._db_session() as s:
        assert db_ops.store_isolation(s, ready_range, {"started_at": "2026-10-22T09:00:00+00:00", "n": 1},
                                      vm_ids) == "a later probe has stored its evidence"
    assert _stored(db, ready_range)["isolation_reprobe"]["n"] == 2
