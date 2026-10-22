"""The work-site template kit (ADR 0012): TN-MGMT01 builds the golden templates on esx04.

Contracts kept here, in CI, where there is no vCenter, govc or TN-MGMT01:

* scripts/register_golden_image_inprocess.py marks a catalogue image built (PATCH only what
  differs), creates a missing variant, and fails on a missing catalogue image.
* scripts/lab/pkrvars-work.py writes lab.auto.pkrvars.hcl mode 0600 with every variable the
  work site needs, never prints a password, escapes HCL template sequences, and
  hashes the one generic Linux build password for every Linux seed (ssh_password_hash),
  and sets the depot feed + apt proxy with --depot.
* scripts/lab/iso-sync-esx04.sh copies only what is missing or differs (stub govc).
* The shell scripts parse (bash -n) and are shellcheck-clean when shellcheck is installed.
* The work-site inventory reuses tn_build without touching TN-MGMT01's Docker.

The scripts were also run against vcsim (ISO sync, DRS rule, replicate, test clones); that
is simulator evidence, not a vCenter run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "scripts/lab"
WORK = ROOT / "install/lab/work-site"
VARIABLES = ROOT / "infra/vsphere/packer/variables.pkr.hcl"
SHELL_SCRIPTS = ["build-phase.sh", "test-clone.sh", "iso-sync-esx04.sh", "lib.sh", "deploy-mgmt-vm.sh"]

VC_SECRET = "Vc-S3cret-${HOME}-%{x}"
WIN_SECRET = "Win-Generic-Pw1!"
LINUX_SECRET = "Generic-Linux-Pw9"  # any value: every Linux seed is rendered from its hash


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── register_golden_image_inprocess.py ────────────────────────────────
class _FakeApi:
    def __init__(self, rows=None, *, list_status=200, patch_status=200):
        self.rows = [dict(r) for r in (rows or [])]
        self.list_status = list_status
        self.patch_status = patch_status
        self.calls: list[tuple] = []

    def get(self, path, params=None, **kw):
        self.calls.append(("GET", path, params))
        assert path == "/golden-images"
        if self.list_status != 200:
            return self.list_status, {"detail": "boom"}
        hv = (params or {}).get("hypervisor")
        return 200, [dict(r) for r in self.rows if hv is None or r["hypervisor"] == hv]

    def patch(self, path, json=None, **kw):
        self.calls.append(("PATCH", path, json))
        if self.patch_status != 200:
            return self.patch_status, {"detail": "Forbidden"}
        row = next(r for r in self.rows if r["id"] == path.rsplit("/", 1)[1])
        row.update(json)
        return 200, dict(row)

    def post(self, path, json=None, **kw):
        self.calls.append(("POST", path, json))
        assert path == "/golden-images"
        row = {"id": f"g{len(self.rows) + 1}", **json}
        self.rows.append(row)
        return 201, dict(row)


def _img(**kw):
    base = {
        "id": "g1",
        "catalogue_id": "srv2022",
        "hypervisor": "vsphere",
        "template_name": "",
        "build_status": "planned",
        "datastore": "",
        "enabled": True,
    }
    return {**base, **kw}


@pytest.fixture
def reg():
    return _load("register_golden_image_inprocess", ROOT / "scripts/register_golden_image_inprocess.py")


def test_register_patches_only_what_differs(reg, capsys):
    api = _FakeApi([_img(), _img(id="g2", catalogue_id="srv2022", hypervisor="proxmox")])
    assert reg.register(api, catalogue_id="srv2022", template="srv2022") == 0
    patches = [c for c in api.calls if c[0] == "PATCH"]
    assert patches == [("PATCH", "/golden-images/g1", {"template_name": "srv2022", "build_status": "built"})]
    assert ("GET", "/golden-images", {"hypervisor": "vsphere"}) in api.calls
    assert "srv2022 -> built" in capsys.readouterr().out


def test_register_is_idempotent(reg, capsys):
    api = _FakeApi([_img(template_name="srv2022", build_status="built")])
    assert reg.register(api, catalogue_id="srv2022", template="srv2022") == 0
    assert not [c for c in api.calls if c[0] != "GET"]
    assert "unchanged" in capsys.readouterr().out


def test_register_missing_catalogue_image_fails_without_creating(reg, capsys):
    api = _FakeApi([])
    assert reg.register(api, catalogue_id="kali", template="kali") == 1
    assert not [c for c in api.calls if c[0] != "GET"]
    assert "Import the catalogue first" in capsys.readouterr().out


def test_register_creates_a_missing_variant(reg):
    api = _FakeApi([])
    variant = {"os_family": "linux", "version": "24.04", "role": "web", "os_aliases": ["ubuntu-24.04-webstack"]}
    assert reg.register(api, catalogue_id="ubuntu-webstack", template="ubuntu-webstack", variant=variant) == 0
    ((_, path, body),) = [c for c in api.calls if c[0] == "POST"]
    assert body["catalogue_id"] == "ubuntu-webstack" and body["build_status"] == "built"
    assert body["template_name"] == "ubuntu-webstack" and body["os_aliases"] == ["ubuntu-24.04-webstack"]


def test_register_reports_api_failures(reg):
    assert reg.register(_FakeApi([], list_status=500), catalogue_id="x1", template="x1") == 1
    assert reg.register(_FakeApi([_img()], patch_status=403), catalogue_id="srv2022", template="srv2022") == 1


def test_register_cli_needs_os_family_for_a_variant(reg):
    with pytest.raises(SystemExit):
        reg.main(["--admin", "a@b", "--id", "v1", "--variant"])


# ── pkrvars-work.py ───────────────────────────────────────────────────
@pytest.fixture
def pk():
    return _load("pkrvars_work", LAB / "pkrvars-work.py")


@pytest.fixture
def site(tmp_path, monkeypatch):
    env = tmp_path / ".env.production"
    env.write_text(
        "# TN-MGMT01\n"
        "VSPHERE_URL=https://192.168.1.10\n"
        "VSPHERE_USERNAME=svc-truenorth@vsphere.local\n"
        f'VSPHERE_PASSWORD="{VC_SECRET}"\n'
        "VSPHERE_DATACENTER=DC-Lab\n"
        "VSPHERE_VERIFY_SSL=false\n"
        f"TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD='{WIN_SECRET}'\n"
    )
    if shutil.which("openssl") is None:
        pytest.skip("openssl hashes the Linux build password (pkrvars-work.py needs it)")
    for name in ("TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD", "TN_TEMPLATE_LINUX_USER", "TN_TEMPLATE_LINUX_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TN_TEMPLATE_LINUX_USER", "ubuntu")
    monkeypatch.setenv("TN_TEMPLATE_LINUX_PASSWORD", LINUX_SECRET)
    out = tmp_path / "lab.auto.pkrvars.hcl"
    args = ["--env-file", str(env), "--out", str(out), "--allow-non-root", "--non-interactive"]
    return {"env": env, "out": out, "args": args}


def _hcl(path: Path) -> dict[str, str]:
    return dict(re.findall(r"^([a-z0-9_]+) = (.*?)(?:  # .*)?$", path.read_text(), re.M))


def _crypt(password: str, salt: str) -> str:
    """Independent of pkrvars-work.py: openssl passwd -6, password on stdin."""
    r = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input=password + "\n",
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _verifies(digest: str, password: str) -> bool:
    m = re.fullmatch(r"\$6\$([./A-Za-z0-9]{1,16})\$[./A-Za-z0-9]{86}", digest)
    return bool(m) and _crypt(password, m.group(1)) == digest


def test_pkrvars_writes_every_required_variable_privately(pk, site, capsys):
    assert pk.main(site["args"]) == 0
    out = site["out"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    vals = _hcl(out)
    assert set(pk.REQUIRED) <= set(vals)
    assert vals["vcenter_server"] == '"192.168.1.10"'
    assert vals["vsphere_host"] == '"esx04.truenorth.lab"'
    assert vals["vsphere_datastore"] == vals["iso_datastore"] == '"esx04-local"'
    assert vals["build_network"] == '"dPG-TN-BUILD"'
    assert vals["http_bind_address"] == '"10.30.31.20"'
    assert vals["content_library"] == '"TrueNorth-Templates"'
    assert vals["publish_to_library"] == "false"  # opt-in (--library): svc-truenorth gets 403 there
    assert vals["insecure_connection"] == "true"
    assert vals["depot_url"] == vals["apt_proxy"] == '""'  # offline bases
    assert vals["ssh_username"] == '"ubuntu"' and vals["ssh_password"] == f'"{LINUX_SECRET}"'
    digest = json.loads(vals["ssh_password_hash"])
    assert _verifies(digest, LINUX_SECRET) and LINUX_SECRET not in digest
    assert vals["iso_folder"] == '"ISO"'
    assert vals["iso_srv2022"] == '"windows/en-us_windows_server_2022_updated_july_2026_x64_dvd_75aa9e18.iso"'
    assert vals["iso_vyos"] == '"appliances/vyos-2026.10.01-0035-rolling-generic-amd64.iso"'
    assert "iso_win10" not in vals and "iso_srv2019" not in vals  # pending: Packer keeps MUST-UPLOAD-FIRST
    # HCL template sequences in a password are escaped, so Packer reads the literal text.
    assert vals["vcenter_password"] == '"Vc-S3cret-$${HOME}-%%{x}"'
    printed = capsys.readouterr()
    for secret in (VC_SECRET, WIN_SECRET, "Vc-S3cret", LINUX_SECRET, digest):
        assert secret not in printed.out and secret not in printed.err
    assert "vcenter_password         <set>" in printed.out
    assert "ssh_password_hash        <set>" in printed.out


def test_pkrvars_depot_and_check(pk, site, capsys, monkeypatch, tmp_path):
    secret = "d3p0t-read-" + "x" * 30
    monkeypatch.delenv("TN_DEPOT_BUILD_READ_PASSWORD", raising=False)
    # Without the tn-build-read password (build-phase.sh depot makes it) --depot refuses (#214).
    assert pk.main([*site["args"], "--depot", "--depot-env", str(tmp_path / "none.env")]) == 2
    assert "build-phase.sh depot" in capsys.readouterr().err
    (tmp_path / "depot-build.env").write_text(f"# root only\nTN_DEPOT_BUILD_READ_PASSWORD={secret}\n")
    assert pk.main([*site["args"], "--depot", "--depot-env", str(tmp_path / "depot-build.env")]) == 0
    vals = _hcl(site["out"])
    assert vals["depot_url"] == '"http://10.30.31.30:8081/repository"'
    assert vals["apt_proxy"] == '"http://10.30.31.30:3142"'  # apt-cacher-ng on the depot's BUILD NIC
    assert vals["depot_user"] == '"tn-build-read"' and vals["depot_password"] == f'"{secret}"'
    printed = capsys.readouterr()
    assert secret not in printed.out + printed.err and "depot_password           <set>" in printed.out
    assert pk.main(["--check", "--allow-non-root", "--out", str(site["out"])]) == 0
    checked = capsys.readouterr().out
    assert "depot_url=http://10.30.31.30:8081/repository" in checked and "apt_proxy=http://10.30.31.30:3142" in checked
    site["out"].chmod(0o644)
    assert pk.main(["--check", "--allow-non-root", "--out", str(site["out"])]) == 1
    assert "want 0600" in capsys.readouterr().out


def test_pkrvars_rewrite_is_idempotent_and_never_follows_a_symlink(pk, site, tmp_path):
    assert pk.main(site["args"]) == 0
    first = site["out"].read_text()
    assert pk.main(site["args"]) == 0
    assert site["out"].read_text() == first
    site["out"].unlink()
    target = tmp_path / "elsewhere"
    target.write_text("x")
    site["out"].symlink_to(target)
    assert pk.main(site["args"]) == 2
    assert target.read_text() == "x"


def test_pkrvars_missing_input_fails_without_prompting(pk, site, monkeypatch, capsys):
    site["env"].write_text(site["env"].read_text().replace("TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD", "UNRELATED"))
    assert pk.main(site["args"]) == 2
    assert "TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD" in capsys.readouterr().err
    assert not site["out"].exists()


def test_pkrvars_requires_root_by_default(pk, site):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root")
    args = [a for a in site["args"] if a != "--allow-non-root"]
    assert pk.main(args) == 2
    assert not site["out"].exists()


def test_pkrvars_any_generic_linux_login_works_without_touching_the_seeds(pk, site, monkeypatch):
    """ADR 0012 decision 5: the seeds are templatefiles, so another user or password is just
    another vars file. No exit 3, no rewriting of committed files."""
    seeds = sorted((ROOT / "infra/vsphere/packer/http").glob("*/*.pkrtpl.hcl"))
    before = {p: p.read_bytes() for p in seeds}
    monkeypatch.setenv("TN_TEMPLATE_LINUX_USER", "tnbuild")
    monkeypatch.setenv("TN_TEMPLATE_LINUX_PASSWORD", "Another-Pw 7$")
    assert pk.main(site["args"]) == 0
    vals = _hcl(site["out"])
    assert vals["ssh_username"] == '"tnbuild"'
    assert _verifies(json.loads(vals["ssh_password_hash"]), "Another-Pw 7$")
    assert {p: p.read_bytes() for p in seeds} == before


def test_pkrvars_new_password_new_hash_same_password_same_file(pk, site, monkeypatch):
    assert pk.main(site["args"]) == 0
    first = json.loads(_hcl(site["out"])["ssh_password_hash"])
    monkeypatch.setenv("TN_TEMPLATE_LINUX_PASSWORD", "Changed-Pw1")
    assert pk.main(site["args"]) == 0
    second = json.loads(_hcl(site["out"])["ssh_password_hash"])
    assert second != first and _verifies(second, "Changed-Pw1") and not _verifies(second, LINUX_SECRET)


@pytest.mark.parametrize("user", ["root", "Tn Build", "Administrator", "-x"])
def test_pkrvars_refuses_a_linux_user_no_seed_can_create(pk, site, monkeypatch, user):
    monkeypatch.setenv("TN_TEMPLATE_LINUX_USER", user)
    assert pk.main(site["args"]) == 2
    assert not site["out"].exists()


def test_pkrvars_refuses_an_https_apt_proxy(pk, site):
    assert pk.main([*site["args"], "--depot", "--apt-proxy", "https://10.30.31.30:3142"]) == 2


def test_the_default_linux_hash_is_the_default_password():
    if shutil.which("openssl") is None:
        pytest.skip("openssl")
    text = VARIABLES.read_text()
    block = text[text.index('variable "ssh_password_hash"'):]
    digest = re.search(r'default\s*=\s*"([^"]+)"', block).group(1)
    pw_block = text[text.index('variable "ssh_password"'):]
    password = re.search(r'default\s*=\s*"([^"]+)"', pw_block).group(1)
    assert _verifies(digest, password)


def test_isos_conf_names_declared_packer_variables(pk):
    isos, pending = pk.parse_isos(WORK / "isos.conf")
    declared = set(
        re.findall(r'^variable "(iso_[a-z0-9_]+)"', (ROOT / "infra/vsphere/packer/variables.pkr.hcl").read_text(), re.M)
    )
    assert set(isos) <= declared
    assert len(isos) == 12  # + iso_sharepoint2019 (on esx01, 2026-10-20)
    pending_vars = {p.split()[0] for p in pending} - {"-"}
    assert pending_vars <= declared  # a PENDING line names a variable Packer already has
    assert {"iso_win10", "iso_srv2019", "iso_exchange2019", "iso_sharepointse", "iso_mecm", "iso_sql2022"} <= pending_vars


# ── shell scripts ─────────────────────────────────────────────────────
@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_shell_scripts_parse(name):
    if shutil.which("bash") is None:
        pytest.skip("bash")
    r = subprocess.run(["bash", "-n", str(LAB / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_shell_scripts_are_shellcheck_clean(name):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    r = subprocess.run(["shellcheck", "-x", name], cwd=LAB, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("name", ["build-phase.sh", "test-clone.sh", "iso-sync-esx04.sh", "pkrvars-work.py"])
def test_entry_points_are_executable(name):
    assert os.access(LAB / name, os.X_OK)


def test_build_phase_needs_root_before_touching_vcenter(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GOVC_", "TN_"))}
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    (tmp_path / "govc").write_text("#!/bin/sh\necho govc-called >&2\nexit 99\n")
    (tmp_path / "govc").chmod(0o755)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root")
    r = subprocess.run(["bash", str(LAB / "build-phase.sh"), "base"], capture_output=True, text=True, env=env)
    assert r.returncode != 0
    assert "run with sudo" in r.stderr and "govc-called" not in r.stderr


# A govc with two datastores (state dir: <ds>/<path> files) for iso-sync-esx04.sh.
STUB = r"""#!/usr/bin/env bash
state="$STUB_STATE"; echo "$*" >> "$state/calls"
ds=""; args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -ds) ds="$2"; shift ;;
    -ds-target) dst="$2"; shift ;;
    -json|-l|-p|-f) ;;
    *) args+=("$1") ;;
  esac
  shift
done
case "${args[0]}" in
  about) echo '{"About":{"ApiType":"VirtualCenter"}}' ;;
  find) echo "/DC-Lab/datastore/${args[${#args[@]}-1]}" ;;
  datastore.ls)
    f="$state/$ds/${args[1]}"; [ -f "$f" ] || exit 1
    printf '[{"file":[{"path":"x","fileSize":%s}]}]\n' "$(wc -c < "$f" | tr -d ' ')" ;;
  datastore.mkdir) mkdir -p "$state/$ds/${args[1]}" ;;
  datastore.cp) mkdir -p "$(dirname "$state/$dst/${args[2]}")"; cp "$state/$ds/${args[1]}" "$state/$dst/${args[2]}" ;;
  datastore.mv) mv "$state/$ds/${args[1]}" "$state/$ds/${args[2]}" ;;
  datastore.rm) rm -f "$state/$ds/${args[1]}" ;;
esac
"""


def test_iso_sync_copies_only_what_is_missing_or_differs(tmp_path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq")
    bindir, state = tmp_path / "bin", tmp_path / "state"
    bindir.mkdir()
    (bindir / "govc").write_text(STUB)
    (bindir / "govc").chmod(0o755)
    for ds, rel, size in [
        ("esx01-local", "ISO/windows/a.iso", 64),
        ("esx01-local", "ISO/linux/b.iso", 32),
        ("esx04-local", "ISO/linux/b.iso", 32),
        ("esx01-local", "ISO/linux/c.iso", 16),
        ("esx04-local", "ISO/linux/c.iso", 8),
    ]:
        p = state / ds / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    (state / "calls").write_text("")
    conf = tmp_path / "isos.conf"
    conf.write_text(
        "iso_srv2022 windows/a.iso\niso_ubuntu linux/b.iso\niso_rocky linux/c.iso\n"
        "iso_kali security-tools/gone.iso\niso_win10 PENDING  # later\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_STATE": str(state),
        "GOVC_USERNAME": "u",
        "GOVC_PASSWORD": "p",
        "GOVC_URL": "https://vcsa.test/sdk",
    }

    def run(*extra):
        return subprocess.run(
            ["bash", str(LAB / "iso-sync-esx04.sh"), "--list", str(conf), *extra],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
        )

    plan = run()
    assert plan.returncode == 0, plan.stderr
    assert "COPY    ISO/windows/a.iso" in plan.stdout and "REPLACE ISO/linux/c.iso" in plan.stdout
    assert (
        "ok      ISO/linux/b.iso" in plan.stdout
        and "missing on esx01-local: ISO/security-tools/gone.iso" in plan.stderr
    )
    assert "datastore.cp" not in (state / "calls").read_text()

    declined = run("--apply")  # no terminal: the y/N answer is no
    assert declined.returncode != 0 and "not confirmed" in declined.stderr
    assert "datastore.cp" not in (state / "calls").read_text()

    applied = run("--apply", "--yes")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert (state / "esx04-local/ISO/windows/a.iso").stat().st_size == 64
    assert (state / "esx04-local/ISO/linux/c.iso").stat().st_size == 16
    cps = [c for c in (state / "calls").read_text().splitlines() if c.startswith("datastore.cp")]
    assert len(cps) == 2 and any(" -f " in c for c in cps)  # c.iso replaced, b.iso untouched
    # Copied under a temporary name and renamed only once complete: never a partial ISO.
    assert all(c.endswith(".tn-partial") for c in cps)
    mvs = [c for c in (state / "calls").read_text().splitlines() if c.startswith("datastore.mv")]
    assert len(mvs) == 2
    assert not list((state / "esx04-local").rglob("*.tn-partial"))

    again = run("--apply", "--yes")
    assert again.returncode == 0 and "Nothing to copy." in again.stdout


# ── work-site inventory ───────────────────────────────────────────────
def test_work_site_inventory_reuses_tn_build_without_docker():
    hosts = yaml.safe_load((WORK / "inventory/hosts.yml").read_text())
    children = hosts["all"]["children"]
    assert children["build"]["hosts"]["tn-mgmt01"]["ansible_host"] == "10.30.30.20"
    assert children["depot"]["hosts"]["tn-depot01"]["ansible_host"] == "10.30.30.30"
    gv = yaml.safe_load((WORK / "inventory/group_vars/all/main.yml").read_text())
    assert gv["tn_build_manage_docker_group"] is False
    assert "docker.io" not in gv["tn_build_packages"] and "xorriso" in gv["tn_build_packages"]
    assert gv["tn_build_repo_dir"] != "/srv/truenorth/app"  # never build from the installed app
    assert gv["tn_depot_build_ip"] == "10.30.31.30" and gv["tn_depot_acng_port"] == 3142
    assert gv["tn_depot_nexus_port"] == 8081
    cfg = (WORK / "ansible.cfg").read_text()
    assert "roles_path = ../roles" in cfg and "inventory = inventory/hosts.yml" in cfg
    lab_defaults = yaml.safe_load((ROOT / "install/lab/roles/tn_build/defaults/main.yml").read_text())
    assert lab_defaults["tn_build_manage_docker_group"] is True  # TN-BUILD01 behaviour unchanged
    assert "docker.io" in lab_defaults["tn_build_packages"]


def test_work_site_vault_is_git_ignored():
    r = subprocess.run(
        ["git", "check-ignore", "-q", "install/lab/work-site/inventory/group_vars/all/vault.yml"],
        cwd=ROOT,
        capture_output=True,
    )
    if r.returncode == 128:
        pytest.skip("not a git checkout")
    assert r.returncode == 0


def test_windows_test_clone_uses_the_workers_sysprep_spec():
    """govc vm.customize defaults to -type Linux, which a Windows guest cannot apply. The
    test clone mirrors the worker's windows_customization() (vsphere_infra.py)."""
    text = (LAB / "test-clone.sh").read_text()
    win = text[text.index("      win*)"):text.index("      *)    # Linux: the worker's path")]
    call = win[win.index('govc vm.customize -vm "$vm"'):]
    for flag in ("-type Windows", "-username TrueNorth", "-org TrueNorth", "-tz 85", '-name "$want_name"',
                 '-ip "$ip"', "-netmask 255.255.255.0", "-pwd "):
        assert flag in call, flag
    assert "-type Windows" in win[: win.index('govc vm.customize -vm "$vm"')]  # the echo line too
    worker = (ROOT / "control-plane/worker/worker/provisioners/vsphere_infra.py").read_text()
    spec = worker[worker.index("def windows_customization"):]
    assert "timeZone=85" in spec and 'fullName="TrueNorth"' in spec and 'orgName="TrueNorth"' in spec
