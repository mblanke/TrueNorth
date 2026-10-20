"""The vSphere Packer tree against TN-DEPOT01 and ADR 0012 (no vCenter here).

Gaps the work-site kit left open, now closed and kept closed:

* Depot paths. Windows builds bootstrap Chocolatey from the chocolatey .nupkg in the Nexus
  feed (Nexus serves no install.ps1); Linux builds use apt-cacher-ng as an apt/dnf forward
  proxy (``apt_proxy``) instead of a ``<depot_url>/apt`` source that does not exist; vendor
  files come from the raw ``installers`` repository.
* Visual Studio business media: win10-22h2 and srv2019 pick their edition by image name +
  public GVLK, and still build from Evaluation media with ``*_eval = true``.
* One generic Linux build login (ADR 0012 decision 5): every Linux seed is a templatefile
  taking ``ssh_username`` and ``ssh_password_hash``; every Linux source logs in with it.
* Server role images (Exchange 2019, SharePoint SE, MECM) have Packer sources.

Two kinds of evidence, kept apart:
* static/rendered: a small renderer for the template subset the seeds use (a test pins the
  seeds to that subset), plus ``packer console`` when Packer is installed;
* ``packer fmt`` / ``packer validate`` (full, with placeholder vCenter values) when Packer and
  its vSphere plugin are installed. CI's packer-vsphere job runs fmt + validate -syntax-only.
Nothing here proves a build: that needs the work vCenter.
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PK = ROOT / "infra/vsphere/packer"
HTTP = PK / "http"
PROXY = "http://10.30.31.30:3142"

SEEDS = {
    "ubuntu-lts": HTTP / "ubuntu/user-data.pkrtpl.hcl",
    "rocky": HTTP / "rocky/ks.cfg.pkrtpl.hcl",
    "kali": HTTP / "kali/preseed.cfg.pkrtpl.hcl",
    "debian13": HTTP / "debian/preseed.cfg.pkrtpl.hcl",
    "parrot": HTTP / "parrot/preseed.cfg.pkrtpl.hcl",
}
PRESEEDS = ("kali", "debian13", "parrot")
LINUX_ISO_SOURCES = tuple(SEEDS)
LINUX_CLONE_SOURCES = ("remnux", "sift", "svc-emulators", "greyspace-host", "ca-host", "usersim", "cloudlog-emu",
                       "c2-server", "variant-linux")
ROLE_IMAGES = {
    "srv2022-exchange2019": ["iso_exchange2019"],
    "srv2022-sharepointse": ["iso_sharepointse", "iso_sql2022"],
    "srv2022-sharepoint2019": ["iso_sharepoint2019", "iso_sql2022", "iso_srv2022"],
    "srv2022-mecm": ["iso_mecm", "iso_sql2022", "iso_srv2022"],
}

# Microsoft Learn, "Key Management Services (KMS) client activation and product keys"
# (https://learn.microsoft.com/windows-server/get-started/kms-client-activation-keys).
GVLK = {
    "gvlk_win10": "NPPR9-FWDCX-D2C8J-H872K-2YT43",  # Windows 10 Enterprise
    "gvlk_srv2019": "N69G4-B89J2-4G8F4-WWYCC-J464C",  # Windows Server 2019 Standard
    "gvlk_srv2022": "VDYBN-27WPP-V4HQT-9VMD4-VMK7H",  # Windows Server 2022 Standard
    "gvlk_srv2025": "TVRH6-WHNXV-R9WG3-9XRFY-MY832",  # Windows Server 2025 Standard
    "gvlk_win10_ltsc": "M7XTQ-FN8P6-TTKYV-9D4CC-J462D",  # Windows 10 Enterprise LTSC 2021
}


def _text(name: str) -> str:
    return (PK / name).read_text(encoding="utf-8")


def _block(text: str, kind: str, name: str) -> str:
    """The body of `<kind> ["<type>"] "<name>" {` up to its matching brace."""
    m = re.search(rf'^{kind}(?: "[^"]+")? "{re.escape(name)}" \{{', text, re.M)
    assert m, f'no {kind} "{name}"'
    depth, i = 0, m.end() - 1
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i : j + 1]
    raise AssertionError(f"unterminated {kind} {name}")


def _variable_default(name: str):
    body = _block(_text("variables.pkr.hcl"), "variable", name)
    m = re.search(r'default\s*=\s*("(?:[^"\\]|\\.)*"|true|false|-?\d+)', body)
    assert m, f"variable {name} has no default"
    raw = m.group(1)
    return raw[1:-1] if raw.startswith('"') else {"true": True, "false": False}.get(raw, raw)


# ── the template subset the seeds use, rendered like Packer's templatefile ──
CONTROL = re.compile(r'^%\{ (?:if (\w+) != "" |endif )~\}$')
INTERP = re.compile(r"\$\{(\w+)\}")


def render(path: Path, **vars_: str) -> str:
    out, keep = [], [True]
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        m = CONTROL.match(line.rstrip("\n"))
        if m:  # whole-line directive with ~}: Packer strips the line and its newline
            if m.group(1):
                keep.append(keep[-1] and vars_[m.group(1)] != "")
            else:
                keep.pop()
            continue
        if keep[-1]:
            out.append(INTERP.sub(lambda x: vars_[x.group(1)], line))
    assert len(keep) == 1, f"{path}: unbalanced %{{ if }}"
    return "".join(out)


SEED_VARS = {"user": "tnbuild", "password_hash": "$6$saltsalt$" + "h" * 86}


@pytest.mark.parametrize("image", SEEDS)
def test_seeds_use_only_the_rendered_template_subset(image):
    text = SEEDS[image].read_text(encoding="utf-8")
    for line in text.splitlines():
        if "%{" in line:
            assert CONTROL.match(line), f"{image}: unsupported directive {line!r}"
    assert set(INTERP.findall(text)) <= {"user", "password_hash", "apt_proxy"}
    assert "${" not in INTERP.sub("", text), f"{image}: an interpolation the renderer does not model"


# ── 1. apt proxy ──────────────────────────────────────────────────────
@pytest.mark.parametrize("image", ["ubuntu-lts", *PRESEEDS])
def test_apt_proxy_is_rendered_when_set(image):
    out = render(SEEDS[image], apt_proxy=PROXY, **SEED_VARS)
    if image == "ubuntu-lts":
        assert re.search(rf'^  apt:\n    proxy: "{re.escape(PROXY)}"$', out, re.M), out
    else:
        assert f"d-i mirror/http/proxy string {PROXY}\n" in out


@pytest.mark.parametrize("image", ["ubuntu-lts", *PRESEEDS])
def test_apt_proxy_is_absent_when_empty(image):
    out = render(SEEDS[image], apt_proxy="", **SEED_VARS)
    assert "10.30.31.30" not in out and ":3142" not in out
    if image == "ubuntu-lts":
        assert not re.search(r"^\s*apt:", out, re.M) and "proxy:" not in out
    else:  # the installer's "no proxy" answer, as before the change
        assert re.search(r"^d-i mirror/http/proxy string\s*$", out, re.M)


def test_no_depot_apt_source_and_no_chocolatey_install_ps1_anywhere():
    """The two depot paths TN-DEPOT01 does not serve."""
    for path in PK.rglob("*"):
        if path.is_file() and path.suffix in {".hcl", ".ps1", ".sh", ".cfg"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            assert "/chocolatey/install.ps1" not in text, path
            assert not re.search(r"(DEPOT_URL|depot_url)\}?/apt\b", text), path
            assert not re.search(r"(?<!installers)/office/office-ltsc\.zip", text), path


def test_every_linux_build_applies_the_proxy_before_any_package_step():
    linux = _text("linux.pkr.hcl")
    build = linux[linux.index('name = "linux"') :]
    first_shell = build.index('provisioner "shell"')
    first = build[first_shell : build.index("\n  }", first_shell)]
    assert "files/linux/apt-proxy.sh" in first and '"APT_PROXY=${var.apt_proxy}"' in first
    before = "\n".join(ln for ln in build[:first_shell].splitlines() if not ln.strip().startswith("#"))
    assert "apt-get" not in before and "dnf" not in before

    derived = _text("derived.pkr.hcl")
    dbuild = derived[derived.index('name = "linux-derived"') : derived.index('source "vsphere-clone" "precomp-host"')]
    proxy_at = dbuild.index("files/linux/apt-proxy.sh")
    assert all(dbuild.index(f"roles/{r}.sh") > proxy_at for r in LINUX_CLONE_SOURCES[:-1])
    assert "TN_APT_PROXY=${var.apt_proxy}" in dbuild  # greyspace-host's own proxy hook

    variants = _text("variants.pkr.hcl")
    linux_scripts = variants[variants.index("variant_linux_scripts") :]
    assert linux_scripts.index("apt-proxy.sh") < linux_scripts.index("variant-packages.sh")
    assert '"APT_PROXY=${var.apt_proxy}"' in variants


def test_cleanup_removes_every_build_proxy():
    text = (PK / "files/linux/cleanup.sh").read_text()
    for what in ("90tn-build-proxy", "90curtin-aptproxy", "/etc/apt/apt.conf", "# tn-build-proxy"):
        assert what in text, what
    assert text.index("90tn-build-proxy") < text.index("cloud-init clean")


def _run_apt_proxy(tmp_path: Path, proxy: str, tools: tuple[str, ...]):
    root, bindir = tmp_path / "root", tmp_path / "bin"
    (root / "etc/apt/apt.conf.d").mkdir(parents=True)
    (root / "etc/yum.repos.d").mkdir(parents=True)
    (root / "etc/dnf").mkdir(parents=True)
    (root / "etc/dnf/dnf.conf").write_text("[main]\ngpgcheck=1\n")
    (root / "etc/yum.repos.d/rocky.repo").write_text(
        "[baseos]\nmirrorlist=https://mirrors.rockylinux.org/mirrorlist?repo=BaseOS-$releasever\n"
        "#baseurl=http://dl.rockylinux.org/$contentdir/$releasever/BaseOS/$basearch/os/\n"
    )
    bindir.mkdir()
    for tool in tools:
        (bindir / tool).write_text("#!/bin/sh\nexit 0\n")
    (bindir / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin", "TN_TEST_ROOT": str(root), "APT_PROXY": proxy}
    r = subprocess.run(["bash", str(PK / "files/linux/apt-proxy.sh")], capture_output=True, text=True, env=env)
    return r, root


def test_apt_proxy_script_writes_the_apt_proxy(tmp_path):
    r, root = _run_apt_proxy(tmp_path, PROXY, ("apt-get",))
    assert r.returncode == 0, r.stderr
    assert (root / "etc/apt/apt.conf.d/90tn-build-proxy").read_text() == f'Acquire::http::Proxy "{PROXY}";\n'


def test_apt_proxy_script_is_a_no_op_when_empty_and_refuses_https(tmp_path):
    r, root = _run_apt_proxy(tmp_path / "a", "", ("apt-get", "dnf"))
    assert r.returncode == 0 and not (root / "etc/apt/apt.conf.d/90tn-build-proxy").exists()
    assert "proxy" not in (root / "etc/dnf/dnf.conf").read_text()
    r, _ = _run_apt_proxy(tmp_path / "b", "https://10.30.31.30:3142", ("apt-get",))
    assert r.returncode == 1


def test_apt_proxy_script_sets_dnf_proxy_and_http_baseurl(tmp_path):
    probe = subprocess.run(["sed", "--version"], capture_output=True, text=True)
    if probe.returncode != 0 or "GNU" not in probe.stdout:
        pytest.skip("GNU sed (the guests' sed) is needed for the dnf path")
    r, root = _run_apt_proxy(tmp_path, PROXY, ("dnf",))
    assert r.returncode == 0, r.stderr
    assert (root / "etc/dnf/dnf.conf").read_text() == f"[main]\ngpgcheck=1\n# tn-build-proxy\nproxy={PROXY}\n"
    repo = (root / "etc/yum.repos.d/rocky.repo").read_text()
    assert "\n#mirrorlist=" in repo and "\nbaseurl=http://dl.rockylinux.org/" in repo
    r, root = _run_apt_proxy(tmp_path / "again", PROXY, ("dnf",))  # idempotent: one proxy line
    assert (root / "etc/dnf/dnf.conf").read_text().count("proxy=") == 1


# ── 2. edition selection for Visual Studio media ──────────────────────
def test_gvlks_are_microsofts_published_kms_client_keys():
    for var, key in GVLK.items():
        assert _variable_default(var) == key, var
    assert "kms-client-activation-keys" in _text("variables.pkr.hcl")


def test_win10_and_srv2019_select_their_edition_like_srv2022():
    assert _variable_default("win10_image_name") == "Windows 10 Enterprise"
    assert _variable_default("srv2019_image_name") == "Windows Server 2019 Standard (Desktop Experience)"
    assert _variable_default("win10_eval") is False and _variable_default("srv2019_eval") is False
    common = _text("windows-common.pkr.hcl")
    assert 'var.win10_eval ? "Windows 10 Enterprise Evaluation" : var.win10_image_name' in common
    assert 'var.win10_eval ? "" : var.gvlk_win10' in common
    assert "var.srv2019_eval ?" in common and 'var.srv2019_eval ? "" : var.gvlk_srv2019' in common
    windows = _text("windows.pkr.hcl")
    win10, srv2019 = _block(windows, "source", "win10-22h2"), _block(windows, "source", "srv2019")
    assert "image_name          = local.win10_image_name" in win10 and "local.win10_product_key" in win10
    assert "image_name          = local.srv2019_image_name" in srv2019 and "local.srv2019_product_key" in srv2019
    assert "Evaluation" not in win10 and 'product_key         = ""' not in srv2019


# ── 3. one generic Linux login ────────────────────────────────────────
@pytest.mark.parametrize("image", LINUX_ISO_SOURCES)
def test_every_linux_seed_is_templated_with_the_generic_login(image):
    raw = SEEDS[image].read_text()
    out = render(SEEDS[image], apt_proxy="", **SEED_VARS)
    assert SEED_VARS["password_hash"] in out
    assert "NOPASSWD:ALL\" > /target/etc/sudoers.d/tnbuild" in out or "tnbuild ALL=(ALL) NOPASSWD:ALL" in out
    for fixed in ("ubuntu", "rocky", "kali", "debian", "parrot"):
        assert not re.search(rf"(username|passwd/username string|--name=)[: ]*{fixed}\b", raw), (image, fixed)
    assert "user-password password" not in raw and "--plaintext" not in raw and "$6$" not in raw
    if image in PRESEEDS:
        assert "d-i passwd/username string tnbuild\n" in out
        assert f"d-i passwd/user-password-crypted password {SEED_VARS['password_hash']}\n" in out
    elif image == "rocky":
        assert f"user --name=tnbuild --iscrypted --password={SEED_VARS['password_hash']} --groups=wheel" in out
    else:
        assert "    username: tnbuild\n" in out and f'    password: "{SEED_VARS["password_hash"]}"' in out


@pytest.mark.parametrize("image", LINUX_ISO_SOURCES)
def test_every_linux_iso_source_renders_its_seed_and_logs_in_generically(image):
    linux = _text("linux.pkr.hcl")
    body = _block(linux, "source", image)
    assert "ssh_username = var.ssh_username" in body and "ssh_password = var.ssh_password" in body
    rel = SEEDS[image].relative_to(PK).as_posix()
    assert f'templatefile("${{path.root}}/{rel}", local.linux_seed)' in body
    assert "http_directory" not in body and "cd_files" not in body
    assert "user          = var.ssh_username" in linux and "password_hash = var.ssh_password_hash" in linux


@pytest.mark.parametrize("name", LINUX_CLONE_SOURCES)
def test_every_linux_clone_logs_in_with_the_generic_login(name):
    text = _text("variants.pkr.hcl") if name.startswith("variant") else _text("derived.pkr.hcl")
    body = _block(text, "source", name)
    assert re.search(r"ssh_username\s*=\s*var\.ssh_username", body)
    assert re.search(r"ssh_password\s*=\s*var\.ssh_password", body)


def test_pkrvars_has_no_seed_check_left():
    text = (ROOT / "scripts/lab/pkrvars-work.py").read_text()
    for gone in ("return 3", "--write-ubuntu-seed", "seed_check", "http/ubuntu/user-data"):
        assert gone not in text, gone


# ── 4. server role images ─────────────────────────────────────────────
@pytest.mark.parametrize("image", ROLE_IMAGES)
def test_role_image_sources_clone_srv2022_with_their_media(image):
    roles = _text("roles.pkr.hcl")
    body = _block(roles, "source", image)
    assert "template     = var.base_srv2022_template" in body
    assert f'iso_paths    = local.role_images["{image}"].isos' in body
    entry = roles[roles.index(f'"{image}" = {{') :]
    isos_line = entry[entry.index("isos") : entry.index("\n", entry.index("isos"))]
    assert re.findall(r"var\.(iso_\w+)", isos_line) == ROLE_IMAGES[image]
    assert "local.variant_winrm_runonce" in body and "sysprep.ps1" in body
    assert f'"source.vsphere-clone.{image}"' in roles
    assert f'"{image}"' in (ROOT / "control-plane/api/app/windows_roles.py").read_text()  # the name the API asks for


def test_role_images_reference_the_right_media():
    assert _variable_default("iso_exchange2019").endswith("ExchangeServer2019-x64-cu15.iso")
    assert _variable_default("iso_sql2022") == "enu_sql_server_2022_enterprise_edition_x64_dvd_aa36de9e.iso"
    assert _variable_default("base_srv2022_template") == "srv2022"
    isos = (ROOT / "install/lab/work-site/isos.conf").read_text()
    assert "enu_sql_server_2022_enterprise_edition_x64_dvd_aa36de9e.iso" in isos
    assert "en_sharepoint_server_2019_x64_dvd_68e34c9e.iso" in isos


def test_role_image_script_covers_each_image_and_needs_the_depot():
    script = (PK / "files/windows/role-image.ps1").read_text()
    for image in ROLE_IMAGES:
        assert script.count(f"'{image}'") >= 2, image  # a pass-1 and a pass-2 branch
    assert "/installers/roles/" in script and "DEPOT_URL is empty" in script
    for marker in ("/ACTION=PrepareImage", "UCMARedist", "prerequisiteinstaller.exe", "role-image.json"):
        assert marker in script, marker
    # Domain-dependent setup is only described (role-image.json), never executed at build time.
    runs = [ln for ln in script.splitlines() if re.search(r"Invoke-Installer|Start-Process|^\s*&", ln)]
    for never in ("Mode:Install", "PrepareAD", "PrepareSchema", "psconfig", "extadsch", "CompleteImage"):
        assert not [ln for ln in runs if never in ln], never


def test_role_images_are_built_by_build_sh_and_catalogued():
    order = _text("build.sh")
    rows = {r["template_id"]: r for r in csv.DictReader(
        (ROOT / "content/catalogue/vm_iso_catalogue.csv").open(encoding="utf-8-sig"))}
    for image in ROLE_IMAGES:
        assert f" {image}" in order and f"|{image}" in order  # ORDER and is_clone
        assert "STAGED MEDIA" in rows[image]["notes"] and "at deploy" in rows[image]["notes"], image
    phase = (ROOT / "scripts/lab/build-phase.sh").read_text()
    assert "srv2022-mecm:iso_mecm,iso_sql2022" in phase
    assert "srv2022-sharepointse:iso_sharepointse,iso_sql2022" in phase


# Role images whose role needs no guest work at deploy: the image is the finished product.
# Add one here only when that is true; otherwise give it a deploy step (vsphere_products.py).
FINISHED_ROLE_IMAGES = {"srv2022-sql2022", "srv2022-entraconnect-sim", "srv2022-erp-app", "srv2019-historian-sim"}


def test_every_enabled_role_image_has_a_deploy_path():
    """An enabled catalogue row resolves at render time, so a range clones that image. A role
    image that only stages its product must then have the deploy-time step that finishes it
    (worker/provisioners/vsphere_products.py), or its role would report installed when it is not."""
    from worker import windows_roles  # noqa: PLC0415
    from worker.provisioners import vsphere_products  # noqa: PLC0415

    rows = {r["template_id"]: r for r in csv.DictReader(
        (ROOT / "content/catalogue/vm_iso_catalogue.csv").open(encoding="utf-8-sig"))}
    images = {img for r in windows_roles.ROLES if r["install"]["method"] == "image"
              for img in r["install"]["images"].values()}
    enabled = {i for i in images if rows.get(i, {}).get("enabled") == "yes"}
    assert {"srv2022-exchange2019", "srv2022-sharepointse", "srv2022-sharepoint2019", "srv2022-mecm"} <= enabled
    for image in enabled:
        assert image in vsphere_products.SETUP_IMAGES or image in FINISHED_ROLE_IMAGES, image
    for image, role in vsphere_products.SETUP_IMAGES.items():
        assert windows_roles.BY_ID[role]["install"]["images"]["windows-server-2022"] == image
    # The marker the deploy step reads is the one role-image.ps1 writes.
    script = (PK / "files/windows/role-image.ps1").read_text()
    assert "role-image.json" in script and "staged = $staged" in script
    assert vsphere_products.MARKER == "C:\\TrueNorth\\role-image.json"
    assert script.count("sql = 'prepared'") == 3  # both SharePoint farms' databases and MECM's site database


def test_sharepoint_2019_installs_its_prerequisites_one_by_one_on_server_2022():
    """SharePoint Server 2019's prerequisite installer does not support Windows Server 2022, so
    the image installs each prerequisite from the depot; the SE image still uses the installer."""
    script = (PK / "files/windows/role-image.ps1").read_text()
    body = script[script.index("'srv2022-sharepoint2019' {"):script.index("'srv2022-mecm' {")]
    assert "prerequisiteinstaller.exe') '/unattended'" not in body
    for f in ("sqlncli.msi", "Synchronization.msi", "MicrosoftIdentityExtensions-64.msi", "setup_msipc_x64.exe",
              "WcfDataServices.exe", "vcredist_x64.exe", "VC_redist.x64.exe", "WindowsServerAppFabricSetup_x64.exe",
              "AppFabric-KB3092423-x64-ENU.exe"):
        assert f"Get-DepotFile '{f}'" in body, f
    assert "Windows-Identity-Foundation" in body and "NET-HTTP-Activation" in body
    assert _variable_default("iso_sharepoint2019") == "en_sharepoint_server_2019_x64_dvd_68e34c9e.iso"


def test_sharepoint_product_key_reaches_the_worker_only_and_no_template():
    vault = (ROOT / "install/inventory/group_vars/all/vault.yml.example").read_text()
    assert re.search(r'^vault_sharepoint_product_key: ""$', vault, re.M)
    main = (ROOT / "install/inventory/group_vars/all/main.yml").read_text()
    assert "tn_sharepoint_product_key: \"{{ vault_sharepoint_product_key | default('') }}\"" in main
    env = (ROOT / "install/roles/tn_config/templates/env.production.j2").read_text()
    assert "TN_SHAREPOINT_PRODUCT_KEY='{{ tn_sharepoint_product_key }}'" in env
    assert "VSPHERE_ROLE_SETUP_TIMEOUT={{ tn_vsphere_role_setup_timeout }}" in env
    compose = (ROOT / "infra/platform/docker/compose.prod.yml").read_text()
    # Once, next to the worker's other deploy-time settings.
    assert compose.count("TN_SHAREPOINT_PRODUCT_KEY: ${TN_SHAREPOINT_PRODUCT_KEY:-}") == 1
    assert compose.index("VSPHERE_SO_SETUP_TIMEOUT:") < compose.index("TN_SHAREPOINT_PRODUCT_KEY:")
    assert "vault_sharepoint_product_key" in (ROOT / "install/roles/tn_preflight/tasks/main.yml").read_text()
    # ADR 0012 decision 3: the image build never sees a product key.
    assert "product_key" not in _text("variables.pkr.hcl").lower()
    assert "$env:TN_SHAREPOINT_PRODUCT_KEY" not in (PK / "files/windows/role-image.ps1").read_text()


# ── 5. Chocolatey from the depot feed ─────────────────────────────────
def test_windows_builds_bootstrap_chocolatey_from_the_feed_nupkg():
    boot = (PK / "files/windows/choco-bootstrap.ps1").read_text()
    assert "FindPackagesById()?id='chocolatey'" in boot
    assert '"$feed/chocolatey/$version"' in boot and "tools\\chocolateyInstall.ps1" in boot
    windows = _text("windows.pkr.hcl")
    assert windows.index("files/windows/choco-bootstrap.ps1") < windows.index("files/windows/setup.ps1")
    variants = _text("variants.pkr.hcl")
    assert variants.index("choco-bootstrap.ps1") < variants.index("variant-packages.ps1")
    prefetch = (ROOT / "install/lab/roles/tn_prefetch/defaults/main.yml").read_text()
    assert "{ choco: chocolatey }" in prefetch
    assert "tn_prefetch_choco_extra" in (ROOT / "install/lab/roles/tn_prefetch/tasks/main.yml").read_text()


# ── Packer itself (skipped where it is not installed) ─────────────────
def _packer(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    if shutil.which("packer") is None:
        pytest.skip("packer not installed")
    return subprocess.run(["packer", *args], cwd=PK, input=stdin, capture_output=True, text=True, timeout=300)


BASE_VARS = ("-var", "vcenter_password=placeholder", "-var", "vsphere_host=esx04.example")


def _validate(*extra: str) -> subprocess.CompletedProcess:
    r = _packer("validate", *BASE_VARS, *extra, ".")
    if "Missing plugins" in r.stdout + r.stderr or "packer init" in r.stdout + r.stderr:
        pytest.skip("the vSphere plugin is not installed (packer init needs the internet)")
    return r


def test_packer_fmt_and_syntax():
    assert _packer("fmt", "-check", "-recursive", ".").returncode == 0
    r = _packer("validate", "-syntax-only", ".")
    assert r.returncode == 0, r.stdout + r.stderr


def test_packer_validate_offline_and_with_the_depot():
    r = _validate()
    assert r.returncode == 0, r.stdout + r.stderr
    r = _validate("-var", f"apt_proxy={PROXY}", "-var", "depot_url=http://10.30.31.30:8081/repository",
                  "-var", "win10_eval=true", "-var", "srv2019_eval=true")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("bad", ["apt_proxy=https://10.30.31.30:3142", "ssh_password_hash=plaintext",
                                 "ssh_username=root"])
def test_packer_validate_rejects(bad):
    r = _validate("-var", bad)
    assert r.returncode != 0


def test_packer_renders_the_seeds_like_the_test_renderer():
    def console(expr: str, *extra: str) -> str:
        r = _packer("console", "-var", "ssh_password=Zq-not-a-substring-9", *extra, ".", stdin=expr + "\n")
        assert r.returncode == 0, r.stderr
        return r.stdout

    on = console('templatefile("http/ubuntu/user-data.pkrtpl.hcl", local.linux_seed)',
                 "-var", f"apt_proxy={PROXY}", "-var", "ssh_username=tnbuild")
    assert f'    proxy: "{PROXY}"' in on and "    username: tnbuild" in on
    off = console('templatefile("http/ubuntu/user-data.pkrtpl.hcl", local.linux_seed)')
    assert "proxy:" not in off and "apt:" not in off and "    username: ubuntu" in off
    kali = console('templatefile("http/kali/preseed.cfg.pkrtpl.hcl", local.linux_seed)', "-var", f"apt_proxy={PROXY}")
    assert f"d-i mirror/http/proxy string {PROXY}" in kali
    assert console("local.win10_image_name").strip() == "Windows 10 Enterprise"
    assert console("local.win10_product_key").strip() == GVLK["gvlk_win10"]
    assert console("local.win10_image_name", "-var", "win10_eval=true").strip() == "Windows 10 Enterprise Evaluation"
    assert console("local.srv2019_product_key").strip() == GVLK["gvlk_srv2019"]
    assert console("local.srv2019_product_key", "-var", "srv2019_eval=true").strip() == ""
