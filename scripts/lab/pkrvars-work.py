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
* ``--depot``: depot_url = http://10.30.31.30:8081/repository (TN-DEPOT01's build NIC). The
  Packer tree appends ``/chocolatey`` to depot_url, and Nexus serves the group feed at
  ``/repository/chocolatey/``. Without ``--depot`` depot_url is empty: offline bases.

No secret is printed or passed on a command line. The file is written atomically, mode
0600, owned by root (run with sudo; ``--allow-non-root`` is for tests). Re-running
rewrites it with the same content: idempotent.

The Ubuntu seed (infra/vsphere/packer/http/ubuntu/user-data) carries a fixed user and
password hash that the ubuntu-lts build logs in with (ssh_username/ssh_password), and every
Linux derived image and variant clones ubuntu-lts. The Linux credentials are checked
against it: a different password stops here unless ``--write-ubuntu-seed`` rewrites the
hash in this checkout (never commit that); a different user always stops (the seed's
sudoers line names it too).

Exit: 0 written (or --check passed); 1 --check found problems; 2 missing input;
3 the Linux credentials do not match the Ubuntu seed.
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
DEFAULT_SEED = PACKER_DIR / "http/ubuntu/user-data"
DEFAULT_DEPOT_URL = "http://10.30.31.30:8081/repository"

SECRET_VARS = ("vcenter_password", "windows_admin_password", "ssh_password")
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
    "publish_to_library": True,
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
    "windows_admin_password",
    "ssh_username",
    "ssh_password",
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


# ── the Ubuntu seed ────────────────────────────────────────────────────
SEED_USER = re.compile(r"^(\s*username:\s*)(\S+)\s*$", re.M)
SEED_HASH = re.compile(r'^(\s*password:\s*")(\$6\$([^$"]+)\$[^"]+)("\s*)$', re.M)


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


def seed_check(seed: Path, user: str, password: str) -> str:
    """'match', 'user' (different user), 'password' (different password) or 'unknown'."""
    text = seed.read_text(encoding="utf-8")
    m_user, m_hash = SEED_USER.search(text), SEED_HASH.search(text)
    if not m_user or not m_hash:
        return "unknown"
    if m_user.group(2) != user:
        return "user"
    computed = _sha512_crypt(password, m_hash.group(3))
    if computed is None:
        return "unknown"
    return "match" if computed == m_hash.group(2) else "password"


def write_seed_hash(seed: Path, password: str) -> None:
    salt = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    digest = _sha512_crypt(password, salt)
    if digest is None:
        raise InputError("openssl is needed to hash the Linux build password (apt install openssl)")
    text = SEED_HASH.sub(lambda m: f"{m.group(1)}{digest}{m.group(4)}", seed.read_text(encoding="utf-8"), count=1)
    seed.write_text(text, encoding="utf-8")


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
    env: dict[str, str], creds: dict[str, tuple[str, str]], isos: dict[str, str], depot_url: str
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
        **isos,
    }
    sources = {
        k: ".env.production"
        for k in ("vcenter_server", "vcenter_username", "vcenter_password", "insecure_connection", "vsphere_datacenter")
    }
    for var, (value, source) in creds.items():
        values[var] = value
        sources[var] = source
    sources.update({k: "isos.conf" for k in isos})
    return values, sources


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
    if absent:
        problems.append(f"missing {', '.join(absent)}")
    depot = present.get("depot_url", '""')
    print(f"depot_url={json.loads(depot) if depot.startswith(chr(34)) else depot}")
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
    ap.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    ap.add_argument("--depot", action="store_true", help="set depot_url (TN-DEPOT01 must answer first)")
    ap.add_argument("--depot-url", default=DEFAULT_DEPOT_URL)
    ap.add_argument(
        "--write-ubuntu-seed",
        action="store_true",
        help="rewrite the Ubuntu seed's password hash in this checkout to the Linux build password",
    )
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
        seed = seed_check(args.seed, creds["ssh_username"][0], creds["ssh_password"][0])
        if seed == "user":
            print(
                f"pkrvars: STOP TN_TEMPLATE_LINUX_USER differs from the user in {args.seed}. The seed "
                "(identity and its sudoers line) must name the same user; change one of them.",
                file=sys.stderr,
            )
            return 3
        if seed == "password":
            if not args.write_ubuntu_seed:
                print(
                    f"pkrvars: STOP TN_TEMPLATE_LINUX_PASSWORD does not match the password hash in {args.seed}, "
                    "so ubuntu-lts (and every Linux derived image and variant) could not log in. Re-run with "
                    "--write-ubuntu-seed to rewrite the hash in this checkout (do not commit it).",
                    file=sys.stderr,
                )
                return 3
            write_seed_hash(args.seed, creds["ssh_password"][0])
            print(f"seed: rewrote the password hash in {args.seed} (local change; do not commit)")
        elif seed == "unknown":
            print(f"seed: WARNING could not verify the Linux build password against {args.seed} (openssl?)")
        values, sources = build_values(env, creds, isos, args.depot_url if args.depot else "")
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
        print("depot_url is empty: offline bases only. After `build-phase.sh depot`: re-run with --depot.")
    print(
        "note: rocky, kali, debian13 and parrot seeds still use their own fixed build users "
        "(rocky/kali/debian/parrot); ssh_username/ssh_password apply to ubuntu-lts and its clones."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
