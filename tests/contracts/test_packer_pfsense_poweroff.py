"""The pfSense keystroke build must end with the guest powering itself off.

With ``communicator = "none"`` the vsphere-iso builder neither runs ``shutdown_command`` nor
powers the VM off: it waits ``shutdown_timeout`` for the guest to do it. The 2026-10-21 work
vCenter run ended its boot_command on the installer's "Reboot" and failed after 19m50s
(40s boot_wait + ~4m keystrokes + 15m shutdown_timeout) with "timeout while waiting for
machine to shutdown". These tests keep the fix: leave the installer through its Shell button
and run ``poweroff``, with a shutdown_timeout that is margin, not install time.
Static only; nothing here proves a build (that needs the work vCenter).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPLIANCES = ROOT / "infra/vsphere/packer/appliances.pkr.hcl"


def _source(name: str) -> str:
    text = APPLIANCES.read_text(encoding="utf-8")
    m = re.search(rf'^source "vsphere-iso" "{name}" \{{', text, re.M)
    assert m, f"no vsphere-iso source {name}"
    depth = 0
    for j in range(m.end() - 1, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[m.end() : j]
    raise AssertionError("unterminated source block")


def _boot_command(body: str) -> list[str]:
    m = re.search(r"boot_command\s*=\s*\[(.*?)\n\s*\]", body, re.S)
    assert m, "no boot_command"
    return re.findall(r'^\s*"((?:[^"\\]|\\.)*)"', m.group(1), re.M)


def _minutes(body: str, key: str) -> float:
    m = re.search(rf'^\s*{key}\s*=\s*"(\d+)([smh])"', body, re.M)
    assert m, f"no {key}"
    return int(m.group(1)) * {"s": 1 / 60, "m": 1, "h": 60}[m.group(2)]


def test_pfsense_has_no_communicator_so_the_guest_must_power_itself_off():
    body = _source("pfsense")
    assert re.search(r'^\s*communicator\s*=\s*"none"', body, re.M)
    # shutdown_command would be silently ignored with no communicator: do not rely on it.
    assert "shutdown_command" not in body


def test_pfsense_boot_command_ends_in_poweroff_not_reboot():
    keys = _boot_command(_source("pfsense"))
    assert keys[-1] in ("poweroff<enter>", "shutdown -p now<enter>", "halt -p<enter>"), keys[-1]
    # The step before it leaves the final installer dialog through Shell, not Reboot (the
    # dialog's default button): a bare "<enter>" there reboots into pfSense, which never halts.
    leave = keys[-2]
    assert leave.startswith(("<tab>", "<right>")) and "<enter>" in leave, leave
    # The install itself gets a long wait before that dialog is answered.
    install = next(k for k in keys if "<left><enter>" in k)
    wait = re.search(r"<wait(\d+)>", install)
    assert wait and int(wait.group(1)) >= 180, install


def test_pfsense_shutdown_timeout_is_margin_not_install_time():
    # The guest halts seconds after the last keystroke; a long timeout only delays the failure.
    assert 3 <= _minutes(_source("pfsense"), "shutdown_timeout") <= 10


def test_header_does_not_claim_packer_powers_the_appliances_off():
    head = APPLIANCES.read_text(encoding="utf-8").split("source ", 1)[0]
    assert "Packer powers the VM off" not in head
    assert "does NOT power the VM off" in head
