"""Evidence the other slices record, as the certification record reads it (ADR 0013 §6).

* S1 (#239) network allocation: the range's VLANs against VLANs used outside TrueNorth on
  the same switch (``external`` reservations), on a backend with the live checks;
* S2 (#240) template provenance: ``vms[].template_build`` against the registered builds;
* S3 (#241) credential state: ``vms[].template_login`` and the range's account rows.

Pure functions (app/range_certification/evidence.py); the API-level wiring is in
test_range_certification.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.range_certification import evidence

DOMAIN = "vsphere:vc01:dvs-range:vlans"


def _res(kind: str, value: str, holder: str = "lan", domain: str = DOMAIN, **extra) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, value=value, holder=holder, domain=domain, **extra)


def _ext(value: str, holder: str = "external:vc01/dvs-range:corp-pg") -> SimpleNamespace:
    return _res("vlan", value, holder, updated_at=datetime(2026, 10, 21, 12, tzinfo=UTC))


VSPHERE = {"provider": "vsphere_api", "vms": [{"name": "web01"}], "networks": [{"name": "lan", "physical_vlan": 3101}]}


# ── S1: network allocation ─────────────────────────────────────────────────────────
class TestNetworkAllocation:
    def test_a_finished_vsphere_build_with_no_collision_passes(self):
        status, summary, detail = evidence.network_allocation(VSPHERE, [_res("vlan", "3101")], [_ext("200")])
        assert status == "pass" and "no collision" in summary
        check = detail["external_collision_check"]
        assert check["overlap"] == [] and check["external_vlans_in_domain"] == [f"{DOMAIN}:200"]
        assert check["last_live_read"] == "2026-10-21T12:00:00+00:00"

    def test_a_vlan_also_used_outside_truenorth_is_a_failure(self):
        status, summary, _ = evidence.network_allocation(VSPHERE, [_res("vlan", "3101")], [_ext("3101")])
        assert status == "fail" and summary == (
            "VLAN collision with use outside TrueNorth: VLAN 3101 (external:vc01/dvs-range:corp-pg)")

    def test_external_use_on_another_switch_is_not_a_collision(self):
        other = _res("vlan", "3101", "external:vc02/dvs:x", domain="vsphere:vc02:dvs:vlans", updated_at=None)
        assert evidence.network_allocation(VSPHERE, [_res("vlan", "3101")], [other])[0] == "pass"

    @pytest.mark.parametrize("output,error,held,expected", [
        ({**VSPHERE, "provider": "mock"}, None, [_res("vlan", "3101")], "the mock backend makes no live collision check"),
        (VSPHERE, "clone failed", [_res("vlan", "3101")], "the build did not finish"),
        (VSPHERE, None, [_res("mac", "00:50:56:00:00:01")], "the range holds no VLAN reservation"),
    ])
    def test_otherwise_it_is_incomplete(self, output, error, held, expected):
        status, summary, _ = evidence.network_allocation(output, held, [], error)
        assert status == "incomplete" and expected in summary


# ── S2: template provenance ────────────────────────────────────────────────────────
def _vm(name: str, catalogue: str = "srv2022", build: str = "20261021-1", checksum: str = "a" * 64,
        promoted: bool = True) -> dict:
    return {"name": name, "template": f"{catalogue}@esx03-20261021",
            "template_build": {"promoted": promoted, "catalogue_id": catalogue, "build_id": build, "checksum": checksum}}


BUILDS = {"srv2022": {"20261021-1": {"checksum": "a" * 64, "promoted": True},
                      "20260901-1": {"checksum": "b" * 64, "promoted": False}}}


class TestTemplateProvenance:
    def test_every_vm_on_a_registered_promoted_build_passes(self):
        status, summary, detail = evidence.template_provenance({"vms": [_vm("dc01"), _vm("dc02")]}, BUILDS)
        assert (status, summary) == ("pass", "2 VM(s) on registered promoted builds: srv2022 20261021-1")
        assert {(r["registered"], r["promoted_now"]) for r in detail["vms"]} == {(True, True)}

    def test_a_build_promoted_before_a_later_one_still_passes_and_says_so(self):
        status, _, detail = evidence.template_provenance({"vms": [_vm("dc01", build="20260901-1",
                                                                           checksum="b" * 64)]}, BUILDS)
        assert status == "pass" and detail["vms"][0]["promoted_now"] is False

    @pytest.mark.parametrize("vms,expected", [
        ([_vm("dc01", checksum="c" * 64)], "dc01: checksum differs from registered build 20261021-1"),
        ([_vm("dc01", build="20250101-9")], "dc01: build 20250101-9 of srv2022 is not registered"),
        ([_vm("dc01"), _vm("dc02", build="20260901-1", checksum="b" * 64)],
         "srv2022: VMs run different builds (20260901-1, 20261021-1)"),
    ])
    def test_a_mismatch_is_a_failure(self, vms, expected):
        status, summary, _ = evidence.template_provenance({"vms": vms}, BUILDS)
        assert status == "fail" and summary == f"pinned build mismatch: {expected}"

    def test_an_unpromoted_image_or_a_vm_without_a_build_record_is_incomplete(self):
        unpromoted = {"name": "u1", "template_build": {"promoted": False, "catalogue_id": "ubuntu-lts",
                                                       "allowed_by": "TN_TEMPLATE_ALLOW_UNPROMOTED"}}
        status, summary, _ = evidence.template_provenance({"vms": [unpromoted, {"name": "fw01"}]}, BUILDS)
        assert status == "incomplete"
        assert summary == "u1: unpromoted image allowed by TN_TEMPLATE_ALLOW_UNPROMOTED; fw01: no build record"


# ── S3: credential state ───────────────────────────────────────────────────────────
def _login(name: str, status: str, method: str | None = None) -> dict:
    rec = {"status": status, "username": "Administrator", "detail": "..."}
    return {"name": name, "template_login": {**rec, "method": method} if method else rec}


def _account(**kw) -> SimpleNamespace:
    base = {"machine": "web01", "username": "analyst", "student_visible": True, "generated": True,
            "scenario": False, "applied": True, "sealed_previous": None, "pending_operation": None,
            "rotated_at": datetime(2026, 10, 21, 12, tzinfo=UTC)}
    return SimpleNamespace(**{**base, **kw})


class TestCredentials:
    def test_template_password_refused_and_student_accounts_set_passes(self):
        out = {"vms": [_login("web01", "rejected"), _login("fw01", "rejected_self_reported", "guestinfo_ack"),
                       _login("rtr01", "rejected", "ssh")]}
        status, summary, detail = evidence.credentials(out, [_account(), _account(username="lab", scenario=True,
                                                                                 applied=False)])
        # A self-reported replacement passes, but is never counted or worded as a refusal (issue #248).
        assert (status, summary) == ("pass", "template password refused on 2 VM(s); template password replaced, "
                                             "reported by the appliance, on 1 VM(s) (no login attempt); "
                                             "1 Student account(s) set with this build's passwords")
        assert detail["accounts"][0]["rotated_at"] == "2026-10-21T12:00:00+00:00"
        # Which method proved each one is part of the record.
        assert [r["method"] for r in detail["template_login"]] == [None, "guestinfo_ack", "ssh"]
        assert [r["status"] for r in detail["template_login"]] == ["rejected", "rejected_self_reported", "rejected"]

    def test_only_self_reported_appliances_never_read_as_refused(self):
        status, summary, _ = evidence.credentials(
            {"vms": [_login("fw01", "rejected_self_reported", "guestinfo_ack")]}, [])
        assert status == "pass" and "refused" not in summary
        assert summary.startswith("template password replaced, reported by the appliance, on 1 VM(s)")

    def test_an_appliance_proven_over_ssh_says_nothing_about_acknowledgements(self):
        status, summary, _ = evidence.credentials({"vms": [_login("fw01", "rejected", "ssh")]}, [])
        assert (status, summary) == ("pass", "template password refused on 1 VM(s); 0 Student account(s) set "
                                             "with this build's passwords")

    def test_a_template_password_that_still_works_is_not_safe(self):
        status, summary, _ = evidence.credentials({"vms": [_login("web01", "accepted")]}, [])
        assert (status, summary) == ("fail", "the golden template's password still logs in on web01")

    @pytest.mark.parametrize("vms,accounts,expected", [
        ([_login("web01", "inconclusive")], [], "template-password check not proven on web01"),
        ([{"name": "web01"}], [], "template-password check not proven on web01"),  # no check recorded
        # An appliance recorded as not checked (a build before issue #248) is no longer a pass.
        ([_login("web01", "rejected"), _login("fw01", "not_checked")], [], "template-password check not proven on fw01"),
        ([_login("fw01", "inconclusive", "none")], [], "template-password check not proven on fw01"),
        # A template built before #248 cannot acknowledge: incomplete, not waited for.
        ([_login("fw01", "unsupported_template", "none")], [], "template-password check not proven on fw01"),
        ([_login("web01", "rejected")], [_account(sealed_previous="tnsec:rk1:x")],
         "a credential change is still pending on web01/analyst"),
        ([_login("web01", "rejected")], [_account(applied=False)],
         "Student account(s) not set by this build yet: web01/analyst"),
    ])
    def test_anything_unproven_is_incomplete(self, vms, accounts, expected):
        status, summary, _ = evidence.credentials({"vms": vms}, accounts)
        assert status == "incomplete" and summary.startswith(expected)
