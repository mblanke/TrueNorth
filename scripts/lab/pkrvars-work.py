#!/usr/bin/env python3
"""Write infra/vsphere/packer/lab.auto.pkrvars.hcl for the work site, on TN-MGMT01 (ADR 0012).

    sudo python3 scripts/lab/pkrvars-work.py                 # offline bases (no depot)
    sudo python3 scripts/lab/pkrvars-work.py --depot         # once TN-DEPOT01 answers
    sudo python3 scripts/lab/pkrvars-work.py --check         # validate the existing file

Where each value comes from:

* vCenter: ``VSPHERE_URL``, ``VSPHERE_USERNAME``, ``VSPHERE_PASSWORD`` (and
  ``VSPHERE_DATACENTER``, ``VSPHERE_VERIFY_SSL``) in the server's
  ``/srv/truenorth/config/.env.production`` (the svc-truenorth account).
* Generic template credentials (ADR 0012 decision 5): ``TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD``,
  ``TN_TEMPLATE_LINUX_USER``, ``TN_TEMPLATE_LINUX_PASSWORD``, from this process's
  environment, else ``.env.production``, else a prompt (passwords hidden).
* The site: esx04 / esx04-local, build network dPG-TN-BUILD, Packer's HTTP server on
  10.30.31.20 (TN-MGMT01 ens224), ISOs on [esx04-local] ISO/ (copied there by
  iso-sync-esx04.sh), Content Library TrueNorth-Templates, and the ISO file names from
  install/lab/work-site/isos.conf.
* ``--depot``: TN-DEPOT01 on its build NIC 10.30.31.30.
  - depot_url = http://10.30.31.30:8081/repository. The Packer tree uses
    ``<depot_url>/chocolatey`` (Nexus' NuGet group feed; Chocolatey itself is bootstrapped
    from the chocolatey .nupkg in it) and ``<depot_url>/installers/...`` (raw repository).
  - apt_proxy = http://10.30.31.30:3142 (apt-cacher-ng), the apt/dnf forward proxy for
    every Linux seed, derived image and variant (``--apt-proxy`` overrides it).
  - depot_user = tn-build-read, depot_password = ``TN_DEPOT_BUILD_READ_PASSWORD`` from
    ``/srv/truenorth/config/depot-build.env`` (root only; ``build-phase.sh depot`` generates
    it): the installers repository is not anonymous (#214).
  Without ``--depot`` all four are empty: offline bases.
* ``ssh_password_hash``: SHA-512 crypt of the Linux build password (``openssl passwd -6``,
  the password on stdin). Every Linux seed is a Packer templatefile that takes the user
  and this hash, so the one generic login works on every Linux template. The salt of the
  existing file is reused when its hash still matches, so a re-run is byte-identical.

No secret is printed or passed on a command line. The file is written atomically, mode
0600, owned by root (run with sudo; ``--allow-non-root`` is for tests). Re-running
rewrites it with the same content: idempotent.

Exit: 0 written (or --check passed); 1 --check found problems; 2 missing input.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[2]
PACKER_DIR = REPO / "infra/vsphere/packer"
DEFAULT_ENV = Path("/srv/truenorth/config/.env.production")
DEFAULT_OUT = PACKER_DIR / "lab.auto.pkrvars.hcl"
DEFAULT_ISOS = REPO / "install/lab/work-site/isos.conf"
DEFAULT_DEPOT_URL = "http://10.30.31.30:8081/repository"
DEFAULT_APT_PROXY = "http://10.30.31.30:3142"
DEFAULT_DEPOT_ENV = Path("/srv/truenorth/config/depot-build.env")
DEPOT_READ_USER = "tn-build-read"
DEPOT_READ_KEY = "TN_DEPOT_BUILD_READ_PASSWORD"

SECRET_VARS = ("vcenter_password", "windows_admin_password", "ssh_password", "ssh_password_hash", "depot_password")
TEMPLATE_ENV = {
    "windows_admin_password": ("TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD", True),
    "ssh_username": ("TN_TEMPLATE_LINUX_USER", False),
    "ssh_password": ("TN_TEMPLATE_LINUX_PASSWORD", True),
}

# Fixed for the work site (2026-10-15/19 discovery). Datacenter and TLS come from the env.
SITE: dict[str, object] = {
    "vsphere_cluster": "",
    "vsphere_host": "esx04.truenorth.lab",
    "vsphere_datastore": "esx04-local",
    "vsphere_folder": "TrueNorth/Templates",
    "iso_datastore": "esx04-local",
    "iso_folder": "ISO",
    "build_network": "dPG-TN-BUILD",
    "http_bind_address": "10.30.31.20",
    "content_library": "TrueNorth-Templates",
    # Off by default here (--library turns it on): the provisioner clones the host-local
    # copies (build.sh replicate), VSPHERE_CONTENT_LIBRARY is empty on this site, and the
    # svc-truenorth account gets 403 from the Content Library API (2026-10-20).
    "publish_to_library": False,
    "win11_vtpm": False,
    "base_ubuntu_template": "ubuntu-lts",
    "base_win10_template": "win10-22h2",
}

REQUIRED = (
    "vcenter_server",
    "vcenter_username",
    "vcenter_password",
    "insecure_connection",
    "vsphere_datacenter",
    "vsphere_host",
    "vsphere_datastore",
    "iso_datastore",
    "iso_folder",
    "build_network",
    "http_bind_address",
    "content_library",
    "publish_to_library",
    "depot_url",
    "apt_proxy",
    "windows_admin_password",
    "ssh_username",
    "ssh_password",
    "ssh_password_hash",
)


class InputError(Exception):
    """Something the operator has to supply is missing (exit 2)."""


# ── inputs ─────────────────────────────────────────────────────────────
def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines as docker compose reads them: `export ` allowed, '#' comments,
    quoted values verbatim (double quotes unescape \\" and \\\\), ' #' ends an unquoted value."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            quote, val = val[0], val[1:-1]
            if quote == '"':
                val = val.replace('\\"', '"').replace("\\\\", "\\")
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        out[key] = val
    return out


def parse_isos(path: Path) -> tuple[dict[str, str], list[str]]:
    """``(var -> path, pending notes)`` from isos.conf."""
    found: dict[str, str] = {}
    pending: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line, _, note = raw.partition("#")
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 2:
            raise InputError(f"{path}: expected '<var|-> <path|PENDING>', got: {raw.strip()}")
        var, rel = parts
        if rel == "PENDING":
            pending.append(f"{var} ({note.strip() or 'no note'})")
            continue
        if var == "-":
            continue
        if not re.fullmatch(r"iso_[a-z0-9_]+", var):
            raise InputError(f"{path}: '{var}' is not a Packer iso_* variable")
        found[var] = rel
    return found, pending


def vcenter_host(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.hostname or ""


def _prompt(label: str, secret: bool, interactive: bool) -> str:
    if not interactive or not sys.stdin.isatty():
        raise InputError(f"{label} is not set and there is no terminal to ask on")
    value = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ").strip()
    if not value:
        raise InputError(f"{label} is empty")
    if secret and getpass.getpass(f"{label} (again): ") != value:
        raise InputError(f"{label}: the two entries differ")
    return value


def template_credentials(env_file: dict[str, str], interactive: bool) -> dict[str, tuple[str, str]]:
    """``var -> (value, source)``: process env, then .env.production, then a prompt."""
    out: dict[str, tuple[str, str]] = {}
    for var, (name, secret) in TEMPLATE_ENV.items():
        if os.environ.get(name):
            out[var] = (os.environ[name], "environment")
        elif env_file.get(name):
            out[var] = (env_file[name], ".env.production")
        else:
            out[var] = (_prompt(name, secret, interactive), "prompt")
    return out


# ── the Linux build password hash (every Linux seed is a templatefile) ─
SHA512_CRYPT = re.compile(r"^\$6\$([./A-Za-z0-9]{1,16})\$[./A-Za-z0-9]{86}$")
LINUX_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _sha512_crypt(password: str, salt: str) -> str | None:
    """SHA-512 crypt via `openssl passwd -6`, the password on stdin (never argv)."""
    if shutil.which("openssl") is None:
        return None
    r = subprocess.run(
        ["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
        input=password + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def previous_hash(out: Path) -> str:
    """ssh_password_hash from an existing vars file, or ''. Never follows a symlink."""
    if out.is_symlink() or not out.is_file():
        return ""
    m = re.search(r'^ssh_password_hash = "([^"]*)"', out.read_text(encoding="utf-8"), re.M)
    return m.group(1).replace("$${", "${") if m else ""


def password_hash(password: str, previous: str = "") -> str:
    """$6$ crypt of the password. Keeps the previous hash when it still matches (idempotent
    re-runs), otherwise a fresh random salt."""
    m = SHA512_CRYPT.match(previous or "")
    if m and _sha512_crypt(password, m.group(1)) == previous:
        return previous
    salt = "".join(secrets.choice(string.ascii_letters + string.digits + "./") for _ in range(16))
    digest = _sha512_crypt(password, salt)
    if digest is None or not SHA512_CRYPT.match(digest):
        raise InputError("openssl (with `passwd -6`) is needed to hash the Linux build password (apt install openssl)")
    return digest


# ── output ─────────────────────────────────────────────────────────────
def hcl_value(value: object) -> str:
    """An HCL literal. Strings are JSON-escaped, then template sequences are escaped too:
    in HCL "${" and "%{" start interpolation, "$${" and "%%{" are the literal text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=True).replace("${", "$${").replace("%{", "%%{")


def render(values: dict[str, object], sources: dict[str, str]) -> str:
    lines = [
        "# GENERATED by scripts/lab/pkrvars-work.py for the work site. Do not edit by hand:",
        "# re-run it. Holds the vCenter and build passwords: mode 0600, root only, git-ignored.",
        "",
    ]
    for key in sorted(values):
        note = f"  # {sources[key]}" if key in sources else ""
        lines.append(f"{key} = {hcl_value(values[key])}{note}")
    return "\n".join(lines) + "\n"


def write_private(path: Path, content: str, *, chown_root: bool) -> None:
    """Atomic write, mode 0600 from the first byte. Refuses to follow a symlink."""
    if path.is_symlink():
        raise InputError(f"{path} is a symlink; refusing to write through it")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        if chown_root:
            os.fchown(fd, 0, 0)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if fd != -1:
            os.close(fd)
        if tmp.exists():
            tmp.unlink()


def build_values(
    env: dict[str, str],
    creds: dict[str, tuple[str, str]],
    isos: dict[str, str],
    depot_url: str,
    apt_proxy: str = "",
    ssh_hash: str = "",
    depot_password: str = "",
) -> tuple[dict[str, object], dict[str, str]]:
    missing = [k for k in ("VSPHERE_URL", "VSPHERE_USERNAME", "VSPHERE_PASSWORD") if not env.get(k)]
    if missing:
        raise InputError(f".env.production lacks {', '.join(missing)}")
    server = vcenter_host(env["VSPHERE_URL"])
    if not server:
        raise InputError("VSPHERE_URL in .env.production has no host")
    values: dict[str, object] = {
        "vcenter_server": server,
        "vcenter_username": env["VSPHERE_USERNAME"],
        "vcenter_password": env["VSPHERE_PASSWORD"],
        "insecure_connection": env.get("VSPHERE_VERIFY_SSL", "false").strip().lower() not in ("1", "true", "yes"),
        "vsphere_datacenter": env.get("VSPHERE_DATACENTER") or "DC-Lab",
        **SITE,
        "depot_url": depot_url,
        "apt_proxy": apt_proxy,
        "depot_user": DEPOT_READ_USER if depot_password else "",
        "depot_password": depot_password,
        **isos,
    }
    sources = {
        k: ".env.production"
        for k in ("vcenter_server", "vcenter_username", "vcenter_password", "insecure_connection", "vsphere_datacenter")
    }
    for var, (value, source) in creds.items():
        values[var] = value
        sources[var] = source
    if ssh_hash:
        values["ssh_password_hash"] = ssh_hash
        sources["ssh_password_hash"] = "openssl passwd -6 of ssh_password"
    sources.update({k: "isos.conf" for k in isos})
    if depot_password:
        sources["depot_password"] = "depot-build.env"
    return values, sources


def depot_read_password(path: Path) -> str:
    """TN_DEPOT_BUILD_READ_PASSWORD: the environment, else the root-only depot-build.env."""
    value = os.environ.get(DEPOT_READ_KEY, "")
    if not value and path.is_file():
        value = parse_env_file(path).get(DEPOT_READ_KEY, "")
    if len(value) < 24:
        raise InputError(
            f"{DEPOT_READ_KEY} is not in {path} (or the environment): run `sudo scripts/lab/build-phase.sh depot`"
            " first; it generates it and gives it to the depot's tn-build-read user"
        )
    return value


def check_file(path: Path, *, require_root: bool = True) -> int:
    """Validate an existing file: mode 0600, root-owned, every required variable present."""
    if not path.exists():
        print(f"pkrvars: MISSING {path} (run: sudo python3 scripts/lab/pkrvars-work.py)")
        return 1
    problems = []
    st = path.stat()
    if st.st_mode & 0o077:
        problems.append(f"mode {oct(st.st_mode & 0o777)} (want 0600)")
    if require_root and st.st_uid != 0:
        problems.append(f"owner uid {st.st_uid} (want root)")
    present = {
        m.group(1): m.group(2)
        for m in re.finditer(r"^([a-z0-9_]+)\s*=\s*(.*?)(?:\s+#.*)?$", path.read_text(encoding="utf-8"), re.M)
    }
    absent = [k for k in REQUIRED if k not in present]
    if present.get("depot_url", '""') != '""' and present.get("depot_password", '""') == '""':
        problems.append("depot_url is set but depot_password is not (re-run with --depot)")
    if absent:
        problems.append(f"missing {', '.join(absent)}")
    for key in ("depot_url", "apt_proxy"):
        raw = present.get(key, '""')
        print(f"{key}={json.loads(raw) if raw.startswith(chr(34)) else raw}")
    for p in problems:
        print(f"pkrvars: PROBLEM {p}")
    if not problems:
        print(f"pkrvars: ok {path}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--isos", type=Path, default=DEFAULT_ISOS)
    ap.add_argument("--depot", action="store_true", help="set depot_url and apt_proxy (TN-DEPOT01 must answer first)")
    ap.add_argument("--depot-url", default=DEFAULT_DEPOT_URL)
    ap.add_argument("--apt-proxy", default=DEFAULT_APT_PROXY, help="apt-cacher-ng URL written with --depot")
    ap.add_argument("--depot-env", type=Path, default=DEFAULT_DEPOT_ENV,
                    help=f"root-only file with {DEPOT_READ_KEY} (written by build-phase.sh depot)")
    ap.add_argument("--library", action="store_true",
                    help="also publish each base build to the content library (publish_to_library = true)")
    ap.add_argument("--check", action="store_true", help="validate the existing file and exit")
    ap.add_argument("--non-interactive", action="store_true", help="never prompt; missing input is an error")
    ap.add_argument("--allow-non-root", action="store_true", help="tests only: do not require root")
    args = ap.parse_args(argv)

    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if args.check:
        return check_file(args.out, require_root=not args.allow_non_root)
    if not is_root and not args.allow_non_root:
        print("pkrvars: run as root (sudo): the file holds the vCenter password and is root-only", file=sys.stderr)
        return 2
    try:
        if not args.env_file.is_file():
            raise InputError(f"{args.env_file} not found or not readable")
        env = parse_env_file(args.env_file)
        isos, pending = parse_isos(args.isos)
        creds = template_credentials(env, interactive=not args.non_interactive)
        user = creds["ssh_username"][0]
        if not LINUX_USER.match(user) or user == "root":
            raise InputError(f"TN_TEMPLATE_LINUX_USER '{user}' is not a lowercase POSIX user name (or is root)")
        if args.depot and not re.fullmatch(r"http://[^/\s]+/?", args.apt_proxy):
            raise InputError(
                f"--apt-proxy must be http://host:port (apt-cacher-ng has no CONNECT), got {args.apt_proxy!r}"
            )
        ssh_hash = password_hash(creds["ssh_password"][0], previous_hash(args.out))
        values, sources = build_values(
            env,
            creds,
            isos,
            args.depot_url if args.depot else "",
            args.apt_proxy if args.depot else "",
            ssh_hash,
            depot_read_password(args.depot_env) if args.depot else "",
        )
        if args.library:
            values["publish_to_library"] = True
            sources["publish_to_library"] = "--library"
        write_private(args.out, render(values, sources), chown_root=is_root)
    except InputError as exc:
        print(f"pkrvars: {exc}", file=sys.stderr)
        return 2

    print(f"pkrvars: wrote {args.out} (mode 0600{', root' if is_root else ''})")
    for key in sorted(values):
        shown = "<set>" if key in SECRET_VARS else hcl_value(values[key])
        print(f"  {key:24} {shown}{'  (' + sources[key] + ')' if key in sources else ''}")
    if pending:
        print("pending ISOs (their builds stay on MUST-UPLOAD-FIRST defaults):")
        for p in pending:
            print(f"  {p}")
    if not args.depot:
        print(
            "depot_url and apt_proxy are empty: offline bases only. After `build-phase.sh depot`: re-run with --depot."
        )
    print(f"linux: every Linux seed creates '{user}' with ssh_password_hash (one generic login, ADR 0012 decision 5)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
