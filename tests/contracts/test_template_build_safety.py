"""Template-kit safety, from the adversarial review of PR #247.

* Linux: the worker configures a Linux range VM ONLY through cloud-init's VMware datasource
  (vsphere_api.py _customize_sync writes guestinfo.metadata/userdata; no vSphere Linux
  customization). So every Linux template must carry cloud-init (the build fails without
  it), regenerate the SSH host keys cleanup.sh deletes, and be test-cloned through that
  same guestinfo path, never govc vm.customize.
* ISOs: a build starts only from an esx04 copy with the source's exact size (and a recorded
  sha256 match when isos.conf pins one); iso-sync never puts a partial copy under the real
  name.
* The seed/secret CDs (cd_files/cd_content) do not outlive the build.
* Windows: the one-shot shutdown task removes itself.

Static and stubbed checks; nothing here proves a build (that needs the work vCenter).
"""

from __future__ import annotations

import base64
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PK = ROOT / "infra/vsphere/packer"
LINUX = PK / "files/linux"
LAB = ROOT / "scripts/lab"


def _sources(text: str, kind: str = "vsphere-iso") -> dict[str, str]:
    out = {}
    for m in re.finditer(rf'^source "{kind}" "([^"]+)" \{{', text, re.M):
        depth, j = 0, m.end() - 1
        for j in range(m.end() - 1, len(text)):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            if depth == 0:
                break
        out[m.group(1)] = text[m.end() : j]
    return out


def _exe(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).lstrip())
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# ── Linux: cloud-init is required ─────────────────────────────────────
def test_every_linux_build_requires_cloud_init_before_cleanup():
    linux = (PK / "linux.pkr.hcl").read_text()
    build = linux[linux.index('build {\n  name = "linux"'):]
    assert (build.index('files/linux/install-cloud-init.sh"') < build.index('files/linux/require-cloud-init.sh"')
            < build.index('files/linux/cleanup.sh"'))
    assert "tn-ssh-hostkeys.service" in build and "systemctl enable tn-ssh-hostkeys.service" in build
    for f in ("derived.pkr.hcl", "variants.pkr.hcl"):
        text = (PK / f).read_text()
        assert text.rindex("require-cloud-init.sh") < text.rindex("files/linux/cleanup.sh"), f


def test_kali_and_parrot_seeds_try_cloud_init_and_say_why():
    kali = (PK / "http/kali/preseed.cfg.pkrtpl.hcl").read_text()
    assert "No cloud-init" not in kali and "works through open-vm-tools without it" not in kali
    assert "VMware guest customization writes" not in kali  # the worker never runs it for Linux
    for seed in (kali, (PK / "http/parrot/preseed.cfg.pkrtpl.hcl").read_text()):
        late = seed[seed.index("d-i preseed/late_command"):]
        assert "apt-get install -y cloud-init || true" in late
    # Debian's DVD has it in the pool: a hard pkgsel/include there, as before.
    assert re.search(r"pkgsel/include string .*\bcloud-init\b", (PK / "http/debian/preseed.cfg.pkrtpl.hcl").read_text())
    assert "cloud-init" in (PK / "http/rocky/ks.cfg.pkrtpl.hcl").read_text()
    assert "cloud-init" in (PK / "http/ubuntu/user-data.pkrtpl.hcl").read_text()


def test_install_cloud_init_never_reaches_for_the_internet_itself():
    text = (LINUX / "install-cloud-init.sh").read_text()
    assert "http://" not in text and "https://" not in text
    assert "exit 1" in text.rsplit("have", 1)[1]  # nothing worked: fail the build


@pytest.fixture
def guest(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "etc/cloud/cloud.cfg.d").mkdir(parents=True)
    (tmp_path / "etc/cloud/cloud.cfg.d/99-vmware.cfg").write_text((LINUX / "99-vmware.cfg").read_text())
    _exe(bin_ / "cloud-init", "#!/bin/sh\necho 24.1\n")
    _exe(bin_ / "vmtoolsd", "#!/bin/sh\nexit 0\n")
    _exe(bin_ / "python3", '#!/bin/sh\n[ -z "$NO_VMWARE_DS" ]\n')
    _exe(bin_ / "systemctl", """
        #!/bin/sh
        case "$2" in
          tn-ssh-hostkeys.service) [ -z "$NO_HOSTKEYS" ] && echo enabled || echo disabled ;;
          cloud-init-local.service) echo enabled ;;
          *) echo disabled ;;
        esac
        """)

    # PATH is the stub directory ONLY, so nothing on the host leaks in: a CI runner (or this
    # Mac) with its own cloud-init/vmtoolsd/python3 must not make a "missing" case pass
    # (#247 CI, 2026-10-21: the Ubuntu runner's cloud-init 24.1 did). The one real tool the
    # script needs, grep, is linked in by absolute path.
    (bin_ / "grep").symlink_to(shutil.which("grep"))
    bash = shutil.which("bash")

    def run(**extra):
        env = {"PATH": str(bin_), "TN_TEST_ROOT": str(tmp_path), **extra}
        return subprocess.run([bash, str(LINUX / "require-cloud-init.sh")], env=env,
                              capture_output=True, text=True, timeout=30)

    return tmp_path, bin_, run


def test_require_cloud_init_accepts_a_usable_template(guest):
    _, _, run = guest
    r = run()
    assert r.returncode == 0, r.stderr
    assert "template OK" in r.stdout


@pytest.mark.parametrize("breakage", ["no-cloud-init", "no-vmware-ds", "datasource", "disabled", "no-tools",
                                      "no-hostkeys"])
def test_require_cloud_init_refuses_an_unusable_template(guest, breakage):
    root, bin_, run = guest
    extra = {}
    if breakage == "no-cloud-init":
        (bin_ / "cloud-init").unlink()
        assert shutil.which("cloud-init", path=str(bin_)) is None  # not the host's either
    elif breakage == "no-vmware-ds":
        extra["NO_VMWARE_DS"] = "1"
    elif breakage == "datasource":
        (root / "etc/cloud/cloud.cfg.d/99-vmware.cfg").write_text("datasource_list: [ NoCloud, None ]\n")
    elif breakage == "disabled":
        (root / "etc/cloud/cloud-init.disabled").write_text("")
    elif breakage == "no-tools":
        (bin_ / "vmtoolsd").unlink()
        assert shutil.which("vmtoolsd", path=str(bin_)) is None
    else:
        extra["NO_HOSTKEYS"] = "1"
    r = run(**extra)
    assert r.returncode == 1
    assert "TEMPLATE NOT USABLE" in r.stderr


def test_host_key_unit_runs_before_sshd_only_when_keys_are_missing():
    unit = (LINUX / "tn-ssh-hostkeys.service").read_text()
    assert "Before=ssh.service sshd.service" in unit
    assert "ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key" in unit
    assert "ExecStart=/usr/bin/ssh-keygen -A" in unit and "WantedBy=multi-user.target" in unit


# ── Linux test clone = the worker's cloud-init path ───────────────────
def test_linux_test_clone_uses_cloud_init_guestinfo_not_vm_customize():
    text = (LAB / "test-clone.sh").read_text()
    linux_branch = text[text.index("      *)    # Linux: the worker's path"):text.index("    esac\n  fi\n  act govc vm.power")]
    assert "vm.customize" not in linux_branch.replace("not govc vm.customize", "")
    assert 'linux_guestinfo "$vm" "$nic" "$want_name" "$ip"' in linux_branch
    fn = text[text.index("linux_guestinfo() {"):]
    fn = fn[: fn.index("\n}\n")]
    assert "linux_guestinfo.py" in fn and 'govc vm.change -vm "$vm" "${args[@]}"' in fn


def test_linux_guestinfo_helper_renders_exactly_what_the_worker_does():
    spec = importlib.util.spec_from_file_location("linux_guestinfo_helper", LAB / "linux_guestinfo.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    got = dict(helper.guestinfo("tntc-esx02", "10.30.31.241", "00:50:56:AA:BB:CC"))
    assert set(got) == {"guestinfo.metadata", "guestinfo.metadata.encoding", "guestinfo.userdata",
                        "guestinfo.userdata.encoding"}
    meta = yaml.safe_load(base64.b64decode(got["guestinfo.metadata"]))
    assert meta["local-hostname"] == "tntc-esx02"
    nic = meta["network"]["ethernets"]["nic0"]
    assert nic["match"]["macaddress"] == "00:50:56:aa:bb:cc" and nic["addresses"] == ["10.30.31.241/24"]
    user = base64.b64decode(got["guestinfo.userdata"]).decode()
    assert user.startswith("#cloud-config\nhostname: tntc-esx02\n")
    pytest.importorskip("pyVmomi")
    import sys
    sys.path.insert(0, str(ROOT / "control-plane/worker"))
    from worker.provisioners import vsphere_infra

    vm_def = {"name": "tntc-esx02", "node_id": "tntc-esx02", "nics": [{"ip": "10.30.31.241", "prefix": 24}]}
    worker = {o.key: o.value for o in vsphere_infra.linux_guestinfo(vm_def, ["00:50:56:AA:BB:CC"])}
    assert worker == got


# ── ISOs ──────────────────────────────────────────────────────────────
def test_iso_ready_compares_with_the_source_and_gates_every_iso_build():
    text = (LAB / "build-phase.sh").read_text()
    fn = text[text.index("iso_ready() {"):]
    fn = fn[: fn.index("\n}\n")]
    assert '"$ISO_SRC_DS"' in fn and '[[ "$dst" == "$src" ]]' in fn and "sha256" in fn
    assert 'ISO_SRC_DS="esx01-local"; ISO_BUILD_DS="esx04-local"' in text
    build = text[text.index("build_and_post() {"):]
    build = build[: build.index("\n}\n")]
    assert build.index('iso_ready "$(iso_var_of "$id")"') < build.index("build.sh")
    assert "NOT BUILDABLE" in build
    for id_ in ("srv2022", "ubuntu-lts", "win11-24h2", "kali", "rocky", "debian13", "parrot", "srv2025",
                "win10-22h2", "srv2019", "pfsense", "vyos", "securityonion"):
        assert re.search(rf"\b{re.escape(id_)}\) echo iso_\w+ ;;", text), id_
    prereq = text[text.index("phase_prereq() {"):]
    prereq = prereq[: prereq.index("\n}\n")]
    assert "NOT BUILDABLE" in prereq and prereq.index("iso-sync-esx04.sh\" --apply") < prereq.index("NOT BUILDABLE")


def test_iso_sync_never_overwrites_a_good_iso_with_a_partial_one():
    text = (LAB / "iso-sync-esx04.sh").read_text()
    assert 'part="$path.tn-partial"' in text
    copy = text[text.index('part="$path.tn-partial"'):]
    assert copy.index('"$path" "$part"') < copy.index("datastore.mv -f")
    assert 'size_of "$DST_DS" "$part"' in copy  # checked before the rename


def test_isos_conf_takes_an_optional_pinned_sha256(tmp_path):
    spec = importlib.util.spec_from_file_location("pkrvars_work", LAB / "pkrvars-work.py")
    pk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pk)
    conf = tmp_path / "isos.conf"
    conf.write_text(f"iso_kali security-tools/k.iso sha256={'a' * 64}  # note\niso_rocky linux/r.iso\n")
    found, _ = pk.parse_isos(conf)
    assert found == {"iso_kali": "security-tools/k.iso", "iso_rocky": "linux/r.iso"}
    conf.write_text("iso_kali security-tools/k.iso sha256=nothex\n")
    with pytest.raises(pk.InputError):
        pk.parse_isos(conf)


# ── seed/secret CDs ───────────────────────────────────────────────────
@pytest.mark.parametrize("f", ["appliances.pkr.hcl", "linux.pkr.hcl", "windows.pkr.hcl"])
def test_every_iso_build_cleans_its_cd_images(f):
    sources = _sources((PK / f).read_text())
    assert sources
    for name, body in sources.items():
        assert re.search(r"^\s*remote_cache_cleanup\s*=\s*true", body, re.M), name
        assert re.search(r"^\s*remove_cdrom\s*=\s*true", body, re.M), name


def test_build_sh_sweeps_packer_cache_after_every_build():
    text = (PK / "build.sh").read_text()
    one = text[text.index("build_one() {"):]
    one = one[: one.index("\n}\n")]
    assert one.index("|| rc=1") < one.index("sweep_cd_cache") < one.index('if [ "$rc" = 0 ]')
    sweep = text[text.index("sweep_cd_cache() {"):]
    sweep = sweep[: sweep.index("\n}\n")]
    assert "packer_cache" in sweep and "datastore.rm" in sweep and r"\.iso$" in sweep
    assert "packer_cache/" in (PK / "README.md").read_text()


# ── Windows ───────────────────────────────────────────────────────────
def test_windows_failed_customization_risk_is_documented():
    guide = (ROOT / "docs/deployment/vm-build-guide.md").read_text()
    assert "If guest customization fails on a Windows clone" in guide
    assert "WinRM with Basic auth" in guide


def test_executable_bits():
    for f in ("install-cloud-init.sh", "require-cloud-init.sh"):
        assert os.access(LINUX / f, os.X_OK), f
