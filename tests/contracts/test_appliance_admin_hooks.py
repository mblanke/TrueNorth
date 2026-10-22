"""The appliance boot hooks that put a range's admin password in force (issue #248).

* pfSense / OPNsense: ``tn-pfsense-config boot`` (PHP) reads the bcrypt hash the worker
  put in ``guestinfo.tn.<product>.admin``, writes it into the admin user (pfSense ``admin``
  ``bcrypt-hash``, OPNsense ``root`` ``password``), reboots once into it, and on the boot
  that finds it in force writes ``guestinfo.tn.<product>.admin_ack`` (the fingerprint the
  worker expects) and clears the key. Run here against a fake firewall directory
  (``TN_PFSENSE_TEST_ROOT``): needs ``php`` on PATH, or ``TN_PHP_DOCKER=1`` with docker.
* VyOS: ``tn-admin-ack.sh`` (installed by tn-vyos-install.py) reports the fingerprint of
  the vyos login's shadow hash after cloud-init. Run here under /bin/sh with a stub
  ``vmware-rpctool`` and a fake shadow file.

Nothing here proves a real appliance: that needs the work vCenter and a rebuilt template.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import bcrypt
import pytest

ROOT = Path(__file__).resolve().parents[2]
PF = ROOT / "infra/vsphere/packer/files/pfsense"
VY = ROOT / "infra/vsphere/packer/files/vyos"
TEMPLATE_CONFIG = ROOT / "infra/vsphere/packer/http/pfsense/config.xml"
sys.path.insert(0, str(ROOT / "control-plane/worker"))

from worker import appliance_admin as aa  # noqa: E402
from worker import pfsense_config as pf  # noqa: E402

RANGE_PW = "Fw-Range-Pass-1!x"


def _php():
    script = PF / "tn-pfsense-config"
    if shutil.which("php"):
        return lambda root, env, *a: subprocess.run(
            ["php", str(script), *a], capture_output=True, text=True, timeout=60,
            env={**os.environ, **env, "TN_PFSENSE_TEST_ROOT": str(root)})
    if os.getenv("TN_PHP_DOCKER") == "1" and shutil.which("docker"):
        def run(root, env, *a):
            flags = [x for k, v in {**env, "TN_PFSENSE_TEST_ROOT": str(root)}.items() for x in ("-e", f"{k}={v}")]
            return subprocess.run(["docker", "run", "--rm", *flags, "-v", f"{root}:{root}", "-v", f"{PF}:/s:ro",
                                   "php:8.2-cli", "php", "/s/tn-pfsense-config", *a],
                                  capture_output=True, text=True, timeout=300)
        return run
    return None


PHP = _php()
needs_php = pytest.mark.skipif(PHP is None, reason="no php CLI (install php, or TN_PHP_DOCKER=1 with docker)")


def _hash() -> str:
    return aa.hash_password("pfsense", RANGE_PW)


def _firewall(tmp_path: Path, product: str = "pfsense") -> Path:
    root = tmp_path / "fw"
    (root / "conf").mkdir(parents=True)
    (root / "guestinfo").mkdir()
    doc = ET.parse(TEMPLATE_CONFIG).getroot()
    if product == "opnsense":  # OPNsense: root user, its hash in <password>
        doc.tag = "opnsense"
        user = doc.find("system/user")
        user.find("name").text = "root"
        old = user.find("bcrypt-hash")
        user.remove(old)
        ET.SubElement(user, "password").text = old.text
    ET.ElementTree(doc).write(root / "conf/config.xml")
    return root


def _gi(root: Path, key: str) -> str | None:
    f = root / "guestinfo" / key
    return f.read_text().strip() if f.exists() else None


def _admin_hash(root: Path, product: str = "pfsense") -> str:
    doc = ET.parse(root / "conf/config.xml").getroot()
    user = next(u for u in doc.findall("system/user") if u.findtext("name") == aa.ADMIN_USERS[product])
    return user.findtext("password" if product == "opnsense" else "bcrypt-hash")


@needs_php
class TestFirewallBootHook:
    def test_first_boot_writes_the_hash_and_reboots_without_acknowledging(self, tmp_path):
        root = _firewall(tmp_path)
        hashed = _hash()
        (root / "guestinfo/guestinfo.tn.pfsense.admin").write_text(hashed)
        r = PHP(root, {}, "boot")
        assert r.returncode == 0, r.stdout + r.stderr
        assert _admin_hash(root) == hashed
        user = ET.parse(root / "conf/config.xml").getroot().find("system/user")
        assert [c.tag for c in user].count("bcrypt-hash") == 1 and user.find("sha512-hash") is None
        assert r.stdout.count("would reboot") == 1
        assert _gi(root, "guestinfo.tn.pfsense.admin_ack") is None  # not before the boot that loads it
        assert _gi(root, "guestinfo.tn.pfsense.admin") == hashed  # still there for that boot
        log = (root / "conf/tn-pfsense-config.log").read_text()
        for text in (r.stdout, r.stderr, log):
            assert hashed not in text and RANGE_PW not in text

    def test_next_boot_acknowledges_and_clears_the_key(self, tmp_path):
        root = _firewall(tmp_path)
        hashed = _hash()
        (root / "guestinfo/guestinfo.tn.pfsense.admin").write_text(hashed)
        assert PHP(root, {}, "boot").returncode == 0
        r = PHP(root, {}, "boot")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "would reboot" not in r.stdout  # no reboot loop
        assert _gi(root, "guestinfo.tn.pfsense.admin_ack") == aa.fingerprint(hashed)
        assert _gi(root, "guestinfo.tn.pfsense.admin") == ""  # cleared
        # The hash the worker made logs in as the range's password, the template's does not.
        stored = _admin_hash(root)
        assert bcrypt.checkpw(RANGE_PW.encode(), ("$2b$" + stored[4:]).encode())
        # A third boot with the key gone changes nothing.
        before = (root / "conf/config.xml").read_text()
        assert PHP(root, {}, "boot").returncode == 0
        assert (root / "conf/config.xml").read_text() == before

    def test_with_a_new_config_one_reboot_covers_both_and_the_users_carry_the_new_hash(self, tmp_path):
        root = _firewall(tmp_path)
        hashed = _hash()
        vm = {"name": "fw", "nics": [{"network": "lan", "ip": "10.60.200.1", "prefix": 24}]}
        cfg = pf.build_config(vm, networks=[{"name": "lan", "cidr": "10.60.200.0/24"}], hostname="fw01")
        (root / "ifconfig.txt").write_text("vmx0: flags=8843<UP> metric 0 mtu 1500\n\tether 00:50:56:aa:00:00\n")
        for key, value in pf.guestinfo(cfg, ["00:50:56:aa:00:00"], hashed).items():
            (root / "guestinfo" / key).write_text(value)
        r = PHP(root, {}, "boot")
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.count("would reboot") == 1
        assert _admin_hash(root) == hashed  # the config apply carried the new hash over
        assert ET.parse(root / "conf/config.xml").getroot().findtext("interfaces/wan/ipaddr") == "10.60.200.1"

    @pytest.mark.parametrize("value", [RANGE_PW, "$6$abc$" + "x" * 86])
    def test_anything_but_a_bcrypt_hash_is_refused_and_never_echoed(self, tmp_path, value):
        root = _firewall(tmp_path)
        before = _admin_hash(root)
        (root / "guestinfo/guestinfo.tn.pfsense.admin").write_text(value)
        r = PHP(root, {}, "boot")
        assert r.returncode == 0 and "would reboot" not in r.stdout
        assert _admin_hash(root) == before
        assert _gi(root, "guestinfo.tn.pfsense.admin_ack").startswith("failed ")
        log = (root / "conf/tn-pfsense-config.log").read_text()
        for text in (r.stdout, log, _gi(root, "guestinfo.tn.pfsense.admin_ack")):
            assert value not in text

    def test_a_config_without_one_admin_user_is_a_failed_ack(self, tmp_path):
        root = _firewall(tmp_path)
        text = (root / "conf/config.xml").read_text().replace("<name>admin</name>", "<name>someone</name>")
        (root / "conf/config.xml").write_text(text)
        (root / "guestinfo/guestinfo.tn.pfsense.admin").write_text(_hash())
        r = PHP(root, {}, "boot")
        assert r.returncode == 0
        assert _gi(root, "guestinfo.tn.pfsense.admin_ack") == "failed no single admin user in " + \
            str(root / "conf/config.xml")

    def test_a_failed_config_apply_still_reboots_into_the_new_password(self, tmp_path):
        root = _firewall(tmp_path)
        hashed = _hash()
        (root / "guestinfo/guestinfo.tn.pfsense.admin").write_text(hashed)
        (root / "guestinfo/guestinfo.tn.pfsense.config").write_text("not base64 at all!")
        r = PHP(root, {}, "boot")
        assert r.returncode == 1 and "NOT applied" in r.stdout
        assert "would reboot (the admin password changed)" in r.stdout
        assert _admin_hash(root) == hashed

    def test_opnsense_sets_root_in_its_password_field(self, tmp_path):
        root = _firewall(tmp_path, "opnsense")
        hashed = aa.hash_password("opnsense", RANGE_PW)
        (root / "guestinfo/guestinfo.tn.opnsense.admin").write_text(hashed)
        env = {"TN_FIREWALL_PRODUCT": "opnsense"}
        assert PHP(root, env, "boot").returncode == 0
        assert _admin_hash(root, "opnsense") == hashed
        r = PHP(root, env, "boot")
        assert r.returncode == 0, r.stdout + r.stderr
        assert _gi(root, "guestinfo.tn.opnsense.admin_ack") == aa.fingerprint(hashed)
        assert _gi(root, "guestinfo.tn.opnsense.admin") == ""

    def test_no_key_leaves_the_admin_alone(self, tmp_path):
        root = _firewall(tmp_path)
        before = (root / "conf/config.xml").read_text()
        r = PHP(root, {}, "boot")
        assert r.returncode == 0 and "would reboot" not in r.stdout
        assert (root / "conf/config.xml").read_text() == before
        assert _gi(root, "guestinfo.tn.pfsense.admin_ack") is None


def test_hook_and_worker_agree_on_keys_and_hash_shape():
    text = (PF / "tn-pfsense-config").read_text()
    assert "define('KEY_ADMIN', 'guestinfo.tn.' . PRODUCT . '.admin');" in text
    assert "define('KEY_ADMIN_ACK', 'guestinfo.tn.' . PRODUCT . '.admin_ack');" in text
    assert pf.admin_key("pfsense") == "guestinfo.tn.pfsense.admin" and aa.ack_key("pfsense").endswith(".admin_ack")
    assert "define('BCRYPT_RE', '/^\\$2[aby]\\$\\d\\d\\$[.\\/A-Za-z0-9]{53}$/');" in text
    assert "substr(hash('sha256', $hash), 0, 16)" in text  # == appliance_admin.fingerprint


# ── VyOS: tn-admin-ack.sh under /bin/sh with stubs ────────────────────────────
def _install_module():
    spec = importlib.util.spec_from_file_location("tn_vyos_install", VY / "tn-vyos-install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _vyos_ack(tmp_path: Path, shadow: str) -> tuple[subprocess.CompletedProcess, str]:
    mod = _install_module()
    script = tmp_path / "tn-admin-ack.sh"
    script.write_text(mod.ACK_SCRIPT)
    calls = tmp_path / "rpc.calls"
    rpc = tmp_path / "vmware-rpctool"
    rpc.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {calls}\n')
    rpc.chmod(0o755)
    (tmp_path / "shadow").write_text(shadow)
    r = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, timeout=30,
                       env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TN_RPCTOOL": str(rpc),
                            "TN_SHADOW": str(tmp_path / "shadow")})
    return r, calls.read_text() if calls.exists() else ""


@pytest.mark.skipif(not (shutil.which("sha256sum") and shutil.which("awk")), reason="needs sha256sum and awk")
class TestVyosAck:
    def test_reports_the_fingerprint_of_the_vyos_shadow_hash(self, tmp_path):
        hashed = aa.hash_password("vyos", RANGE_PW)
        r, calls = _vyos_ack(tmp_path, f"root:*:19000:0:99999:7:::\nvyos:{hashed}:19000:0:99999:7:::\n")
        assert r.returncode == 0, r.stderr
        assert calls.strip() == f"info-set guestinfo.tn.vyos.admin_ack {aa.fingerprint(hashed)}"
        assert hashed not in calls and hashed not in r.stdout + r.stderr

    @pytest.mark.parametrize("shadow", ["root:*:1::::::\n", "vyos:!:1::::::\n", "vyos:*:1::::::\n"])
    def test_no_usable_hash_is_a_failed_ack(self, tmp_path, shadow):
        r, calls = _vyos_ack(tmp_path, shadow)
        assert r.returncode == 0, r.stderr
        assert calls.strip() == "info-set guestinfo.tn.vyos.admin_ack failed the vyos login has no password hash"


def test_vyos_finish_installs_the_ack_unit_after_cloud_init(tmp_path):
    mod = _install_module()
    rw = tmp_path / "boot/img/rw"
    rw.mkdir(parents=True)
    mod.finish_image(tmp_path)
    assert (rw / "etc/tn-admin-ack.sh").read_text() == mod.ACK_SCRIPT
    unit = (rw / "etc/systemd/system/tn-admin-ack.service").read_text()
    assert "After=open-vm-tools.service cloud-final.service" in unit
    assert "WantedBy=cloud-init.target" in unit and "ExecStart=/bin/sh /etc/tn-admin-ack.sh" in unit
    link = rw / "etc/systemd/system/cloud-init.target.wants/tn-admin-ack.service"
    assert link.is_symlink() and str(os.readlink(link)) == "/etc/systemd/system/tn-admin-ack.service"
    mod.finish_image(tmp_path)  # idempotent
