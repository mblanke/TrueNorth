"""The pfSense and VyOS appliance builds finish their template, then power themselves off.

With ``communicator = "none"`` the vsphere-iso builder neither runs ``shutdown_command`` nor
powers the VM off: it waits ``shutdown_timeout`` for the guest to do it. The 2026-10-21 work
vCenter run ended pfSense's boot_command on the installer's "Reboot" and failed after
19m50s (40s boot_wait + ~4m keystrokes + 15m shutdown_timeout) with "timeout while waiting
for machine to shutdown".

Now each build finishes the template from a second CD and powers off ONLY when every step
worked, so a green build is a usable template and a failed finish makes no template:

* pfSense: the installer's Shell, then files/pfsense/tn-pfsense-finish.sh (Open-VM-Tools
  from sha256-pinned offline packages, tn-pfsense-config + tn-vmtools.sh, the template's
  config.xml via ``tn-pfsense-config template``), then poweroff;
* VyOS: files/vyos/tn-vyos-prep.sh (no hw-id pins) and tn-vyos-install.py (drives
  ``install image`` by prompt text, cloud-init datasource VMware first), then poweroff;
* build-phase.sh stages the pfSense packages first and test-clones both in "finish" mode:
  Tools running AND the guest publishing guestinfo.tn.template.finish, which only a
  finished template does, so an unfinished template is never registered.

The finish scripts run here against stubs and fakes. Nothing here proves a build: that
needs the work vCenter (and, for pkgs.lock, one ``stage_pfsense_pkgs.py resolve``).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PK = ROOT / "infra/vsphere/packer"
APPLIANCES = PK / "appliances.pkr.hcl"
PF = PK / "files/pfsense"
VY = PK / "files/vyos"
BUILD_PHASE = ROOT / "scripts/lab/build-phase.sh"
STAGE_SCRIPT = ROOT / "scripts/lab/stage_pfsense_pkgs.py"
BOOT_COMMAND = "/usr/local/bin/php -q /usr/local/sbin/tn-pfsense-config boot"


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


def _list(body: str, key: str) -> list[str]:
    m = re.search(rf"^\s*{key}\s*=\s*\[(.*?)\n\s*\]", body, re.S | re.M)
    assert m, f"no {key}"
    return re.findall(r'^\s*"((?:[^"\\]|\\.)*)"', m.group(1), re.M)


def _minutes(body: str, key: str) -> float:
    m = re.search(rf'^\s*{key}\s*=\s*"(\d+)([smh])"', body, re.M)
    assert m, f"no {key}"
    return int(m.group(1)) * {"s": 1 / 60, "m": 1, "h": 60}[m.group(2)]


def _exe(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ── shared: no communicator, so the guest powers itself off ───────────
@pytest.mark.parametrize("name", ["pfsense", "vyos"])
def test_no_communicator_and_no_shutdown_command(name):
    body = _source(name)
    assert re.search(r'^\s*communicator\s*=\s*"none"', body, re.M)
    # shutdown_command would be silently ignored with no communicator: do not rely on it.
    assert "shutdown_command" not in body


def test_header_does_not_claim_packer_powers_the_appliances_off():
    head = APPLIANCES.read_text(encoding="utf-8").split("source ", 1)[0]
    assert "Packer powers the VM off" not in head
    assert "does NOT power the VM off" in head


# ── pfSense: Packer source ────────────────────────────────────────────
def test_pfsense_leaves_the_installer_through_shell_and_runs_the_finish():
    keys = _list(_source("pfsense"), "boot_command")
    install = next(k for k in keys if "<left><enter>" in k)
    wait = re.search(r"<wait(\d+)>", install)
    assert wait and int(wait.group(1)) >= 180, install
    # The final dialog's default button is Reboot: a bare "<enter>" there reboots into
    # pfSense, which never halts (2026-10-21). Leave it through Shell instead.
    leave = keys[-2]
    assert leave.startswith(("<tab>", "<right>")) and "<enter>" in leave, leave
    last = keys[-1]
    assert "mount_cd9660 /dev/iso9660/TNPFSENSE /tmp/tncd" in last
    assert last.endswith("sh /tmp/tncd/tn-pfsense-finish.sh<enter>"), last
    # The script powers off; a separate poweroff keystroke would also power off a failed finish.
    assert not any(k.strip().startswith("poweroff") for k in keys)
    # No "<" or ">" other than key names: boot_command would read them as special keys.
    assert not re.search(r"<(?!enter>|wait\d*>|tab>|left>|right>|spacebar>)", "".join(keys))


def test_pfsense_cd_carries_the_finish_offline():
    body = _source("pfsense")
    files = _list(body, "cd_files")
    for f in ("tn-pfsense-finish.sh", "tn-pfsense-config", "tn-vmtools.sh", "pkgs.lock"):
        assert f"${{path.root}}/files/pfsense/{f}" in files
        assert (PF / f).is_file()
    assert "${path.root}/stage/pfsense/*.pkg" in files
    assert re.search(r'"tn-admin.hash"\s*=\s*var\.ssh_password_hash', body)
    assert re.search(r'cd_label\s*=\s*"TNPFSENSE"', body)
    assert "http_directory" not in body and "http_content" not in body  # no network needed


def test_pfsense_shutdown_timeout_is_margin_not_install_time():
    assert 3 <= _minutes(_source("pfsense"), "shutdown_timeout") <= 10


def test_stage_directory_is_not_committed():
    assert "infra/vsphere/packer/stage/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


# ── pfSense: the finish script, against a fake installed system ───────
def _pkg(name: str) -> bytes:
    return f"fake package {name}\n".encode()


@pytest.fixture
def pf(tmp_path):
    """A fake installer: the CD, the installed system at TN_FINISH_MNT, stub tools."""
    cd, mnt, bin_, log = tmp_path / "cd", tmp_path / "mnt", tmp_path / "bin", tmp_path / "calls.log"
    for d in (cd, bin_, mnt / "etc", mnt / "usr/local/bin", mnt / "usr/local/sbin", mnt / "conf.default",
              mnt / "cf/conf", mnt / "var/tmp", mnt / "dev"):
        d.mkdir(parents=True)
    (mnt / "dev/null").write_text("")
    (mnt / "etc/version").write_text("2.7.2-RELEASE\n")
    _exe(mnt / "usr/local/bin/php", "#!/bin/sh\nexit 0\n")
    (mnt / "conf.default/config.xml").write_text("<pfsense/>\n")
    for f in ("tn-pfsense-finish.sh", "tn-pfsense-config", "tn-vmtools.sh"):
        shutil.copy(PF / f, cd / f)
    pkgs = ["gettext-runtime-0.22.5.pkg", "glib-2.80.5,2.pkg", "open-vm-tools-nox11-12.4.5,2.pkg"]
    lines = ["# test lock"]
    for p in pkgs:
        (cd / p).write_bytes(_pkg(p))
        lines.append(f"{hashlib.sha256(_pkg(p)).hexdigest()}  {p}")
    (cd / "pkgs.lock").write_text("\n".join(lines) + "\n")
    (cd / "tn-admin.hash").write_text("$6$salt$" + "a" * 86 + "\n")
    _exe(bin_ / "sha256", '#!/bin/sh\n'
         'if command -v sha256sum >/dev/null; then sha256sum "$2"; else shasum -a 256 "$2"; fi | cut -d" " -f1\n')
    # Everything the script would otherwise take from the host (mounts, pools, the root
    # filesystem) is stubbed, so the result never depends on the machine running the tests.
    _exe(bin_ / "df", '#!/bin/sh\nprintf "Filesystem 1K-blocks\\n/dev/fake0 1\\n"\n')
    _exe(bin_ / "poweroff", f'#!/bin/sh\necho poweroff >> "{log}"\n')
    for tool in ("zpool", "zfs", "mount", "umount", "sync"):
        _exe(bin_ / tool, f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n')
    _exe(bin_ / "chroot", f"""
        #!/bin/sh
        root="$1"; shift
        [ "$1" = env ] && {{ shift; while case "$1" in *=*) true;; *) false;; esac; do shift; done; }}
        case "$1" in
          */pkg-static)
            if [ "$2" = info ]; then
              for n in $ALREADY; do [ "$n" = "$4" ] && exit 0; done
              exit 1
            fi
            echo "add $(basename "$3")" >> "{log}"
            case "$3" in *open-vm-tools*) printf '#!/bin/sh\\n' > "$root/usr/local/bin/vmtoolsd"; chmod +x "$root/usr/local/bin/vmtoolsd";; esac
            [ -f "$root$3" ] || exit 9 ;;
          */php)
            echo "php $*" >> "{log}"
            grep -q '^\\$6\\$' "$root$5" || exit 8
            echo '<pfsense/>' > "$root/cf/conf/config.xml" ;;
          *) exit 7 ;;
        esac
        """)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "TN_FINISH_MNT": str(mnt), "ALREADY": "gettext-runtime"}

    def run(**extra):
        r = subprocess.run(["sh", str(cd / "tn-pfsense-finish.sh")], env={**env, **extra},
                           capture_output=True, text=True, timeout=60)
        calls = log.read_text().splitlines() if log.exists() else []
        return r, calls

    return {"cd": cd, "mnt": mnt, "run": run, "pkgs": pkgs}


def test_finish_installs_everything_then_powers_off(pf):
    r, calls = pf["run"]()
    assert r.returncode == 0, r.stdout + r.stderr
    adds = [c for c in calls if c.startswith("add ")]
    # Lock order, the package pfSense already has skipped.
    assert adds == ["add glib-2.80.5,2.pkg", "add open-vm-tools-nox11-12.4.5,2.pkg"]
    assert any(c.startswith("php /usr/local/bin/php -q /usr/local/sbin/tn-pfsense-config template ") for c in calls)
    assert calls[-1] == "poweroff"
    mnt = pf["mnt"]
    for f in ("usr/local/sbin/tn-pfsense-config", "usr/local/etc/rc.d/tn-vmtools.sh"):
        assert os.access(mnt / f, os.X_OK), f
    marker = (mnt / "cf/conf/tn-template-finish.txt").read_text()
    assert "pfsense=2.7.2-RELEASE" in marker and "open-vm-tools-nox11" in marker
    assert "admin_ack=1" in marker.split()  # issue #248: the worker waits for the ack only then
    assert not (mnt / "var/tmp/tn-admin.hash").exists() and not (mnt / "var/tmp/tn-pkgs").exists()


@pytest.mark.parametrize("break_it", ["bad-sha", "missing-pkg", "empty-lock", "wrong-version", "no-system"])
def test_finish_failure_never_powers_off(pf, break_it):
    cd, mnt = pf["cd"], pf["mnt"]
    if break_it == "bad-sha":
        (cd / pf["pkgs"][1]).write_bytes(b"tampered")
    elif break_it == "missing-pkg":
        (cd / pf["pkgs"][2]).unlink()
    elif break_it == "empty-lock":
        (cd / "pkgs.lock").write_text("# nothing pinned\n")
    elif break_it == "wrong-version":
        (mnt / "etc/version").write_text("2.8.0-RELEASE\n")
    else:
        (mnt / "etc/version").unlink()
    r, calls = pf["run"]()
    assert r.returncode == 1
    assert "poweroff" not in calls
    assert "tn-finish: FAILED" in r.stdout and "NOT powering off" in r.stdout
    assert not (mnt / "cf/conf/tn-template-finish.txt").exists()


def test_finish_script_powers_off_once_at_the_very_end():
    lines = [ln.strip() for ln in (PF / "tn-pfsense-finish.sh").read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert lines[-1] == "poweroff"
    assert sum(ln == "poweroff" for ln in lines) == 1
    die = (PF / "tn-pfsense-finish.sh").read_text().split("die() {", 1)[1].split("}", 1)[0]
    assert "poweroff" not in die.replace("NOT powering off", "")


def test_finish_scripts_parse():
    for f in ("tn-pfsense-finish.sh", "tn-vmtools.sh"):
        assert subprocess.run(["sh", "-n", str(PF / f)], capture_output=True).returncode == 0, f


# ── pfSense: tn-pfsense-config template (needs php, or docker php:8.2-cli) ──
def _php():
    script = PF / "tn-pfsense-config"
    if shutil.which("php"):
        return lambda root, *a: subprocess.run(["php", str(script), *a], capture_output=True, text=True,
                                               env={**os.environ, "TN_PFSENSE_TEST_ROOT": str(root)}, timeout=60)
    if os.getenv("TN_PHP_DOCKER") == "1" and shutil.which("docker"):
        return lambda root, *a: subprocess.run(
            ["docker", "run", "--rm", "-e", f"TN_PFSENSE_TEST_ROOT={root}", "-v", f"{root}:{root}",
             "-v", f"{PF}:/s:ro", "php:8.2-cli", "php", "/s/tn-pfsense-config", *a],
            capture_output=True, text=True, timeout=300)
    return None


PHP = _php()
FACTORY = """<?xml version="1.0"?>
<pfsense>
  <version>23.3</version>
  <system>
    <user><name>admin</name><scope>system</scope><uid>0</uid><bcrypt-hash>$2y$10$factoryfactoryfactoryfa</bcrypt-hash></user>
    <webgui><protocol>https</protocol></webgui>
  </system>
  <interfaces>
    <wan><enable/><if>em0</if><ipaddr>dhcp</ipaddr></wan>
    <lan><enable/><if>em1</if><ipaddr>192.168.1.1</ipaddr><subnet>24</subnet></lan>
  </interfaces>
</pfsense>
"""


@pytest.mark.skipif(PHP is None, reason="no php CLI (install php, or TN_PHP_DOCKER=1 with docker)")
def test_template_config_from_the_factory_config(tmp_path):
    import xml.etree.ElementTree as ET

    root = tmp_path / "fw"
    (root / "conf.default").mkdir(parents=True)
    (root / "conf.default/config.xml").write_text(FACTORY)
    hash_ = "$6$salt$" + "b" * 86
    (root / "admin.hash").write_text(hash_ + "\n")
    for _ in range(2):  # idempotent: the second run starts from the written config
        r = PHP(root, "template", str(root / "admin.hash"))
        assert r.returncode == 0, r.stdout + r.stderr
    cfg = ET.parse(root / "conf/config.xml").getroot()
    assert cfg.findtext("version") == "23.3"  # the release's own version, never a newer one
    assert cfg.findtext("interfaces/wan/if") == "vmx0" and cfg.findtext("interfaces/lan/if") == "vmx1"
    assert cfg.findtext("system/enablesshd") == "enabled"
    assert [e.text for e in cfg.findall("system/earlyshellcmd")] == [BOOT_COMMAND]
    admin = cfg.find("system/user")
    assert admin.findtext("bcrypt-hash") == hash_ and len(admin.findall("bcrypt-hash")) == 1
    assert cfg.findtext("system/webgui/protocol") == "https"  # everything else kept


@pytest.mark.skipif(PHP is None, reason="no php CLI (install php, or TN_PHP_DOCKER=1 with docker)")
def test_template_config_refuses_a_bad_hash(tmp_path):
    root = tmp_path / "fw"
    (root / "conf.default").mkdir(parents=True)
    (root / "conf.default/config.xml").write_text(FACTORY)
    (root / "admin.hash").write_text("plaintext\n")
    r = PHP(root, "template", str(root / "admin.hash"))
    assert r.returncode == 1
    assert not (root / "conf/config.xml").exists()


def test_boot_starts_vmware_tools_before_applying():
    text = (PF / "tn-pfsense-config").read_text()
    boot = text.split("case 'boot':", 1)[1].split("case 'apply':", 1)[0]
    assert boot.index("start_tools()") < boot.index("apply($mode)")
    assert "define('VMTOOLS_RC', '/usr/local/etc/rc.d/tn-vmtools.sh');" in text


# ── pfSense: staging the pinned packages ──────────────────────────────
def _stage_mod():
    spec = importlib.util.spec_from_file_location("stage_pfsense_pkgs", STAGE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_lock_parses_and_an_empty_lock_says_how_to_pin():
    st = _stage_mod()
    try:
        pins = st.read_lock()
    except st.StageError as exc:  # not pinned yet: the message must say how
        assert "resolve --out" in str(exc) and "installers/pfsense/2.7.2" in str(exc)
    else:
        assert pins and all(n.endswith(".pkg") for _, n in pins)


def test_stage_from_dir_checks_every_sha256(tmp_path):
    st = _stage_mod()
    src, out, lock = tmp_path / "src", tmp_path / "out", tmp_path / "pkgs.lock"
    src.mkdir()
    names = ["glib-2.80.5,2.pkg", "open-vm-tools-nox11-12.4.5,2.pkg"]
    for n in names:
        (src / n).write_bytes(_pkg(n))
    st.write_lock([(hashlib.sha256(_pkg(n)).hexdigest(), n) for n in names], lock)
    (out).mkdir()
    (out / "stale-1.0.pkg").write_bytes(b"old")
    assert st.stage(depot=None, from_dir=src, out=out, lock=lock) == names
    assert sorted(p.name for p in out.glob("*.pkg")) == sorted(names)
    assert st.check(out, lock) == []
    (src / names[1]).write_bytes(b"tampered")
    (out / names[1]).unlink()
    with pytest.raises(st.StageError, match="does not match pkgs.lock"):
        st.stage(depot=None, from_dir=src, out=out, lock=lock)
    assert not (out / names[1]).exists()
    assert st.check(out, lock) == [f"missing: {out / names[1]}"]


def test_stage_refuses_ambiguous_or_non_http_sources(tmp_path):
    st = _stage_mod()
    with pytest.raises(st.StageError):
        st.stage(depot=None, from_dir=None, out=tmp_path)
    with pytest.raises(st.StageError):
        st.stage(depot="ftp://depot", from_dir=None, out=tmp_path)


def test_resolve_orders_dependencies_first():
    st = _stage_mod()
    cat = st.parse_catalogue("\n".join(json.dumps(e) for e in [
        {"name": "open-vm-tools-nox11", "deps": {"glib": {}, "libmspack": {}}, "path": "All/ovt.pkg", "sum": "a"},
        {"name": "glib", "deps": {"gettext-runtime": {}, "pcre2": {}}, "path": "All/glib.pkg", "sum": "b"},
        {"name": "gettext-runtime", "deps": {}, "path": "All/gt.pkg", "sum": "c"},
        {"name": "pcre2", "path": "All/pcre2.pkg", "sum": "d"},
        {"name": "libmspack", "path": "All/mspack.pkg", "sum": "e"},
        {"name": "unrelated", "path": "All/x.pkg", "sum": "f"},
    ]))
    order = [e["name"] for e in st.closure(cat)]
    assert order == ["gettext-runtime", "pcre2", "glib", "libmspack", "open-vm-tools-nox11"]
    del cat["pcre2"]
    with pytest.raises(st.StageError, match="pcre2"):
        st.closure(cat)


# ── VyOS ──────────────────────────────────────────────────────────────
def test_vyos_runs_the_finish_from_its_cd_and_types_no_installer_answers():
    body = _source("vyos")
    keys = _list(body, "boot_command")
    assert keys[-1] == "sudo python3 /run/tncd/tn-vyos-install.py<enter>"
    assert any("/dev/disk/by-label/TNVYOS /run/tncd" in k for k in keys)
    assert any("tn-vyos-prep.sh" in k for k in keys)
    assert not any("install image" in k for k in keys)  # answered by prompt text, not timing
    files = _list(body, "cd_files")
    for f in ("tn-vyos-prep.sh", "tn-vyos-install.py"):
        assert f"${{path.root}}/files/vyos/{f}" in files and (VY / f).is_file()
    assert re.search(r'"tn-vyos.pw"\s*=\s*var\.ssh_password\b', body)
    assert re.search(r'cd_label\s*=\s*"TNVYOS"', body)
    assert 10 <= _minutes(body, "shutdown_timeout") <= 30


def test_vyos_prep_drops_the_hw_id_pins():
    lines = [ln.strip() for ln in (VY / "tn-vyos-prep.sh").read_text().splitlines() if not ln.startswith("#")]
    for nic in ("eth0", "eth1"):
        assert f"delete interfaces ethernet {nic} hw-id" in lines
    assert lines.index("configure") < lines.index("commit") < lines.index("save")


FAKE_INSTALLER = r'''
import getpass, json, sys
answers = {}
def ask(key, prompt, secret=False):
    answers[key] = getpass.getpass(prompt) if secret else input(prompt)
print("Welcome to VyOS installation!\nThis command will install VyOS to your permanent storage.")
ask("continue", "Would you like to continue? [y/N] ")
ask("name", "What would you like to name this image? (Default: 2026.10.01-0035-rolling) ")
ask("pw1", 'Please enter a password for the "vyos" user: ', True)
ask("pw2", 'Please confirm password for the "vyos" user: ', True)
ask("console", "What console should be used by default? (K: KVM, S: Serial)? (Default: K) ")
print("Probing disks\n1 disk(s) found\nThe following disks were found:\nDrive: /dev/sda (8.0 GB)")
ask("disk", "Which one should be used for installation? (Default: /dev/sda) ")
ask("wipe", "Installation will delete all data on the drive. Continue? [y/N] ")
ask("space", "Would you like to use all the free space on the drive? [Y/n] ")
print("Creating partition table...")
EXTRA
ask("keys", "Would you like to copy SSH host keys? [Y/n] ")
print("The following config files are available:")
print("1: /opt/vyatta/etc/config.boot.default\n2: /opt/vyatta/etc/config/config.boot")
ask("boot", "Which file would you like as boot config? (Default: 1) ")
print("Copying system image files\nInstalling GRUB configuration files")
print("The image installed successfully; please reboot now.")
json.dump(answers, open(sys.argv[1], "w"))
'''


def _vyos(tmp_path, extra="", stuck="120"):
    work = tmp_path / "cd"
    work.mkdir()
    shutil.copy(VY / "tn-vyos-install.py", work / "tn-vyos-install.py")
    (work / "tn-vyos.pw").write_text("Generic-Build-Pw1\n")
    fake = tmp_path / "fake_installer.py"
    fake.write_text(FAKE_INSTALLER.replace("EXTRA", extra))
    image = tmp_path / "root/boot/2026.10.01-0035-rolling/rw"
    image.mkdir(parents=True)
    answers = tmp_path / "answers.json"
    env = {**os.environ, "TN_VYOS_INSTALLER": f"{sys.executable} {fake} {answers}",
           "TN_VYOS_ROOT": str(tmp_path / "root"), "TN_VYOS_NO_POWEROFF": "1", "TN_VYOS_STUCK_AFTER": stuck}
    r = subprocess.run([sys.executable, str(work / "tn-vyos-install.py")], env=env, capture_output=True,
                       text=True, timeout=120)
    return r, answers, image


def test_vyos_install_answers_by_prompt_text(tmp_path):
    r, answers, image = _vyos(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads(answers.read_text())
    assert got == {"continue": "y", "name": "", "pw1": "Generic-Build-Pw1", "pw2": "Generic-Build-Pw1",
                   "console": "K", "disk": "", "wipe": "y", "space": "y", "keys": "n", "boot": "2"}
    cfg = (image / "etc/cloud/cloud.cfg.d/99-tn-vmware.cfg").read_text()
    assert re.search(r"datasource_list: \[ VMware\b", cfg)
    assert (image / "etc/tn-template-finish.txt").is_file()
    unit = (image / "etc/systemd/system/tn-template-marker.service").read_text()
    assert "info-set guestinfo.tn.template.finish $$(" in unit and "ConditionPathExists=/etc/tn-template-finish.txt" in unit
    link = image / "etc/systemd/system/multi-user.target.wants/tn-template-marker.service"
    assert os.readlink(link) == "/etc/systemd/system/tn-template-marker.service"
    assert "Generic-Build-Pw1" not in r.stdout  # the password is never echoed to the console
    assert "OK: installed" in r.stdout


def test_vyos_install_unknown_prompt_fails_without_powering_off(tmp_path):
    r, answers, image = _vyos(tmp_path, extra='ask("odd", "Would you like to frobnicate the flux? [y/N] ")',
                              stuck="2")
    assert r.returncode == 1
    assert "no answer for prompt" in r.stdout and "NOT powering off" in r.stdout
    assert not answers.exists()
    assert not (image / "etc/cloud").exists()


def test_vyos_install_powers_off_only_after_success():
    text = (VY / "tn-vyos-install.py").read_text()
    main = text.split("def main()", 1)[1]
    assert main.index("return 1") < main.index('"systemctl", "poweroff"')
    assert text.count('"poweroff"') == 1


# ── build-phase.sh: stage first, prove Tools before registering ───────
def test_build_phase_tests_appliance_clones_for_the_finish_marker():
    text = BUILD_PHASE.read_text()
    assert re.search(r"^\s*pfsense\|vyos\) echo finish ;;", text, re.M)
    assert "echo power" not in text.split("test_mode() {", 1)[1].split("}", 1)[0]
    clone = (ROOT / "scripts/lab/test-clone.sh").read_text()
    assert 'finish) [[ "$tools" == "guestToolsRunning" && -n "${marker:-}" ]] && ok=1 ;;' in clone
    assert '"guestinfo.tn.template.finish"' in clone.split("finish_marker() {", 1)[1].split("\n}\n", 1)[0]


@pytest.mark.skipif(PHP is None, reason="no php CLI (install php, or TN_PHP_DOCKER=1 with docker)")
def test_pfsense_boot_publishes_the_finish_marker(tmp_path):
    root = tmp_path / "fw"
    (root / "conf").mkdir(parents=True)
    (root / "guestinfo").mkdir()
    (root / "conf/config.xml").write_text(FACTORY)
    (root / "conf/tn-template-finish.txt").write_text("finished=2026-10-21T00:00:00Z\npfsense=2.7.2-RELEASE\n")
    r = PHP(root, "boot")
    assert r.returncode == 0, r.stdout + r.stderr
    got = (root / "guestinfo/guestinfo.tn.template.finish").read_text().strip()
    assert got == "finished=2026-10-21T00:00:00Z pfsense=2.7.2-RELEASE"
    # No marker (a template whose finish never ran): nothing published.
    (root / "conf/tn-template-finish.txt").unlink()
    (root / "guestinfo/guestinfo.tn.template.finish").unlink()
    assert PHP(root, "boot").returncode == 0
    assert not (root / "guestinfo/guestinfo.tn.template.finish").exists()


def test_build_phase_stages_pfsense_packages_before_building_it():
    text = BUILD_PHASE.read_text()
    loop = text.split("phase_appliances() {", 1)[1].split("\n}\n", 1)[0]
    assert loop.index("pfsense_stage") < loop.index('build_and_post "$id"')
    fn = text.split("pfsense_stage() {", 1)[1].split("\n}\n", 1)[0]
    assert "stage_pfsense_pkgs.py" in fn and "check" in fn and "--depot-env" in fn
    assert "https://" not in fn  # depot or a local directory, never the internet
