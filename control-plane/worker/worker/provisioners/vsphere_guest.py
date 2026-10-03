"""TrueNorth Range - deploy-time software installs inside range VMs, over VMware guest operations.

Ranges have no route from the worker (VLAN-isolated, no egress), so nothing here uses
the network into the range. The worker asks vCenter to start a process inside the guest
(``guestOperationsManager.processManager.StartProgramInGuest``) and polls its exit code
(``ListProcessesInGuest``). The guest then fetches packages from the depot, which it can
reach only through the range pfSense's WAN uplink.

Guest credentials never leave the worker process:
- Windows: the random per-VM Administrator password that Sysprep customization sets
  (vsphere_infra.windows_customization), handed from the customization step to the
  install step inside the same provision call.
- Linux: an ephemeral ``tn-install`` user with a random password, created by cloud-init
  from the guestinfo userdata, locked and deleted by the last install command, expiring
  on its own after two days if that command never runs. The provisioner rewrites the
  guestinfo userdata without it once the install is over.
Passwords are held in ``Secret`` (its repr is redacted), are never logged, and are
scrubbed from any error text that is recorded.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import secrets
import shlex
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field

try:
    from pyVmomi import vim
except ImportError:  # the provisioner reports the missing SDK when it is used
    vim = None  # type: ignore[assignment]

from ..software_catalogue import InstallSpec

logger = logging.getLogger(__name__)

CHOCO_EXE = r"C:\ProgramData\chocolatey\bin\choco.exe"
# 1641 / 3010: installed, reboot initiated / required. The range keeps running either way.
CHOCO_OK = frozenset({0, 1641, 3010})
INSTALL_USER = "tn-install"
INSTALL_LOG = "/var/log/tn-software-install.log"
CLEANUP_GRACE = 120  # seconds the Linux clean-up gets even when the install ran out of time


class Secret:
    """A password that does not print itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __reduce__(self):  # never pickled into a result backend or a broker message
        raise TypeError("Secret cannot be serialised")


@dataclass(frozen=True)
class GuestCredentials:
    family: str  # windows | linux
    username: str
    password: Secret = field(repr=False)


def windows_credentials() -> GuestCredentials:
    """The built-in Administrator with a random password that meets Windows complexity rules."""
    return GuestCredentials("windows", "Administrator", Secret(secrets.token_urlsafe(18) + "!9a"))


def linux_credentials() -> GuestCredentials:
    return GuestCredentials("linux", INSTALL_USER, Secret(secrets.token_urlsafe(24)))


def redact(text: str, *creds: GuestCredentials | None) -> str:
    out = str(text)
    for c in creds:
        if c is not None and c.password.reveal():
            out = out.replace(c.password.reveal(), "***")
    return out


def _sha512_crypt(password: str) -> str | None:
    """A SHA-512 crypt hash where the platform's crypt(3) makes one (glibc does), else None."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import crypt  # removed in Python 3.13; the worker image runs 3.11
        hashed = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    except Exception:  # noqa: BLE001 — no crypt module or no SHA-512 support
        return None
    return hashed if hashed and hashed.startswith("$6$") else None


def cloud_init_user(creds: GuestCredentials, today: _dt.date | None = None) -> dict:
    """cloud-config ``users`` entry for the ephemeral install user (passwordless sudo).

    Hashed where possible, so the guestinfo vCenter readers can see holds no plaintext.
    The account expires after two days even if the clean-up command never runs.
    """
    expires = (today or _dt.datetime.now(_dt.UTC).date()) + _dt.timedelta(days=2)
    user = {
        "name": creds.username,
        "gecos": "TrueNorth deploy-time installer (removed after install)",
        "shell": "/bin/sh",
        "sudo": "ALL=(ALL) NOPASSWD:ALL",
        "lock_passwd": False,
        "expiredate": expires.isoformat(),
    }
    hashed = _sha512_crypt(creds.password.reveal())
    if hashed:
        user["hashed_passwd"] = hashed
    else:
        user["plain_text_passwd"] = creds.password.reveal()
    return user


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GuestCommand:
    label: str  # the software name, or the name of a bookkeeping step
    program: str
    arguments: str
    ok_codes: frozenset = frozenset({0})
    step: bool = False  # bookkeeping (apt update, cloud-init wait), not a package outcome


def windows_commands(specs: list[InstallSpec], choco_feed: str) -> list[GuestCommand]:
    """One ``choco install`` per catalogue entry, from the depot feed only."""
    out = []
    for spec in specs:
        args = f'install {" ".join(spec.packages)} -y --no-progress --source "{choco_feed}"'
        if spec.version:
            args += f" --version {spec.version}"
        out.append(GuestCommand(spec.name, CHOCO_EXE, args, CHOCO_OK))
    return out


def _sudo(shell: str) -> tuple[str, str]:
    """Run ``shell`` as root with its output appended to the install log in the guest."""
    return "/usr/bin/sudo", "-n /bin/sh -c " + shlex.quote(f"{{ {shell} ; }} >>{INSTALL_LOG} 2>&1")


def linux_commands(specs: list[InstallSpec], proxy: str) -> list[GuestCommand]:
    """Wait for cloud-init, refresh apt, then one install per catalogue entry, through the depot proxy."""
    out = [GuestCommand("cloud-init", *_sudo("cloud-init status --wait || true"), step=True)]
    p = shlex.quote(proxy)
    if any(s.manager == "apt" for s in specs):
        apt = (f"DEBIAN_FRONTEND=noninteractive apt-get -q -o Acquire::http::Proxy={p} "
               f"-o Acquire::https::Proxy={p} -o DPkg::Lock::Timeout=600")
        out.append(GuestCommand("apt-update", *_sudo(f"{apt} update"), step=True))
    for spec in specs:
        pkgs = " ".join(shlex.quote(x) for x in spec.packages)
        if spec.manager == "dnf":
            cmd = f"dnf -y -q --setopt=proxy={p} install {pkgs}"
        else:
            cmd = (f"{apt} -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "
                   f"install --no-install-recommends {pkgs}")
        out.append(GuestCommand(spec.name, *_sudo(cmd)))
    return out


def linux_cleanup_command(username: str = INSTALL_USER) -> GuestCommand:
    """Lock and delete the install user, a few seconds after this guest operation returns.

    Delayed, because guest operations log in afresh on every call: locking the account at
    once would fail the poll that collects this command's own exit code.
    """
    u = shlex.quote(username)
    later = (f"sleep 15; passwd -l {u}; usermod --expiredate 1 {u}; pkill -KILL -u {u}; userdel -r {u}; "
             f"sed -i '/^{username}[[:space:]]/d' /etc/sudoers.d/90-cloud-init-users")
    shell = f"nohup /bin/sh -c {shlex.quote(later)} >/dev/null 2>&1 &"
    return GuestCommand("cleanup", "/usr/bin/sudo", "-n /bin/sh -c " + shlex.quote(shell), step=True)


# --------------------------------------------------------------------------- #
# Guest operations
# --------------------------------------------------------------------------- #


class GuestNotReadyError(RuntimeError):
    """Guest operations did not become usable before the deadline."""


class GuestSession:
    """Run programs inside one VM as ``creds`` (blocking pyVmomi; call through a thread)."""

    def __init__(self, content, vm, creds: GuestCredentials, *, poll: float = 5.0,
                 clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None):
        gom = content.guestOperationsManager
        self._pm = gom.processManager
        self._am = gom.authManager
        self.vm = vm
        self.creds = creds
        self._poll = poll
        self._clock = clock or (lambda: time.monotonic())
        self._sleep = sleep or (lambda s: time.sleep(s))

    def _auth(self):
        return vim.vm.guest.NamePasswordAuthentication(
            username=self.creds.username, password=self.creds.password.reveal(), interactiveSession=False
        )

    def wait_ready(self, deadline: float) -> None:
        """Until VMware Tools runs, guest customization has finished and the login works."""
        last = "VMware Tools not running"
        while True:
            try:
                guest = self.vm.guest
                ready = bool(getattr(guest, "guestOperationsReady", False))
                if self.creds.family == "windows":
                    status = getattr(getattr(guest, "customizationInfo", None), "customizationStatus", None)
                    if status == "TOOLSDEPLOYPKG_FAILED":
                        raise GuestNotReadyError("guest customization (Sysprep) failed")
                    if status is not None and status != "TOOLSDEPLOYPKG_SUCCEEDED":
                        ready, last = False, f"guest customization {status}"
                if ready:
                    self._am.ValidateCredentialsInGuest(vm=self.vm, auth=self._auth())
                    return
            except GuestNotReadyError:
                raise
            except Exception as exc:  # noqa: BLE001 — not up yet: InvalidGuestLogin, GuestOperationsUnavailable…
                last = redact(getattr(exc, "msg", None) or type(exc).__name__, self.creds)
            if self._clock() >= deadline:
                raise GuestNotReadyError(f"guest operations not ready before the deadline: {last}")
            self._sleep(self._poll)

    def run(self, cmd: GuestCommand, deadline: float) -> tuple[str, int | None]:
        """Start ``cmd`` and wait for it: (``ok`` | ``failed`` | ``timeout``, exit code)."""
        spec = vim.vm.guest.ProcessManager.ProgramSpec(programPath=cmd.program, arguments=cmd.arguments)
        pid = self._pm.StartProgramInGuest(vm=self.vm, auth=self._auth(), spec=spec)
        while True:
            try:
                procs = self._pm.ListProcessesInGuest(vm=self.vm, auth=self._auth(), pids=[pid])
            except Exception:  # noqa: BLE001 — a transient guest-ops hiccup; the deadline bounds it
                procs = []
            if procs and procs[0].endTime is not None:
                code = procs[0].exitCode
                return ("ok" if code in cmd.ok_codes else "failed"), code
            if self._clock() >= deadline:
                with contextlib.suppress(Exception):
                    self._pm.TerminateProcessInGuest(vm=self.vm, auth=self._auth(), pid=pid)
                return "timeout", None
            self._sleep(self._poll)


def install(session: GuestSession, commands: list[GuestCommand], deadline: float,
            cleanup: GuestCommand | None = None) -> dict:
    """Run ``commands`` in order, then ``cleanup``. Returns the VM's software outcome.

    ``{"status": ok | partial | failed, "packages": [{"name", "status", "exit_code"}],
    "error": "..."}``; ``status`` is ``ok`` only when every package installed.
    """
    creds = session.creds
    packages: list[dict] = []
    error = ""
    names = [c.label for c in commands if not c.step]
    try:
        session.wait_ready(deadline)
    except Exception as exc:  # noqa: BLE001
        error = redact(str(exc), creds)
        packages = [{"name": n, "status": "not_run", "exit_code": None} for n in names]
        cleanup = None  # the login does not work, so neither would the clean-up
    else:
        try:
            for cmd in commands:
                if session._clock() >= deadline:
                    status, code = "timeout", None
                else:
                    logger.info("guest install on %s: %s", getattr(session.vm, "name", ""), cmd.label)
                    try:
                        status, code = session.run(cmd, deadline)
                    except Exception as exc:  # noqa: BLE001
                        status, code = "failed", None
                        error = error or redact(getattr(exc, "msg", None) or str(exc), creds)
                if cmd.step:
                    if status != "ok":
                        logger.warning("guest install step %s ended %s (exit %s)", cmd.label, status, code)
                    continue
                packages.append({"name": cmd.label, "status": status, "exit_code": code})
        finally:
            if cleanup is not None:
                try:
                    status, code = session.run(cleanup, max(deadline, session._clock() + CLEANUP_GRACE))
                except Exception as exc:  # noqa: BLE001
                    status, code = "failed", None
                    logger.warning("install user clean-up failed: %s", redact(str(exc), creds))
                if status != "ok":
                    error = error or f"could not remove the install user (clean-up {status}, exit {code})"
    good = sum(1 for p in packages if p["status"] == "ok")
    overall = "ok" if packages and good == len(packages) and not error else ("partial" if good else "failed")
    if not packages and not error:
        overall = "ok"
    out = {"status": overall, "packages": packages}
    if error:
        out["error"] = error
    return out
