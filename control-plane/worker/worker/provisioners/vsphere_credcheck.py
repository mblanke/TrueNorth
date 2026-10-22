"""Range credentials in the guest (ADR 0013 §3): prove the template password is gone, and
change passwords in a running range.

**Template-login check.** The golden templates are built with one generic password
(infra/vsphere/packer; the Windows Administrator, the Linux build user, Security Onion's
``soadmin``). At deploy Sysprep, cloud-init and the Security Onion setup replace it with the
range's own generated password. ``check`` proves that per VM with **one** guest-operations
login attempt as the template login (``ValidateCredentialsInGuest``, no retry):

==================  =========================================================================
``rejected``        refused, after the range's own login worked in this VM (guest operations
                    proven): the template password is gone. Pass.
``accepted``        the template password still logs in. **Not safe**: with
                    ``RANGE_TEMPLATE_LOGIN_CHECK=enforce`` (the default) the VM fails its
                    readiness, with ``warn`` it is a warning.
``inconclusive``    anything else: guest operations not proven, Tools not running, a vCenter
                    error, or the worker not knowing the template password. Never a pass.
==================  =========================================================================

The worker knows the template passwords only for this check (``TN_TEMPLATE_*_PASSWORD``,
``VSPHERE_SO_INSTALL_PASSWORD``); the API no longer does. The VM's output records
``template_login = {status, username, detail}``, never a password.

**Rotation.** ``rotate`` changes passwords inside one running VM, logged in as an admin
account of it: the new ``user:password`` lines are uploaded into a file only root (Linux)
or the Administrators (Windows) can read, set with ``chpasswd`` (through ``sudo -S``, its
password in a second uploaded file) or ADSI ``SetPassword`` (local and, on a domain
controller, domain accounts), and the files are deleted in the same command. No password is
on a guest command line. Each new password is then verified by a guest-operations login.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import shlex
from collections.abc import Callable

from . import vsphere_guest as guest

logger = logging.getLogger(__name__)

MODE_ENV = "RANGE_TEMPLATE_LOGIN_CHECK"
WINDOWS_PASSWORD_ENV = "TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD"
LINUX_PASSWORD_ENV = "TN_TEMPLATE_LINUX_PASSWORD"
SO_USER_ENV, SO_PASSWORD_ENV = "VSPHERE_SO_INSTALL_USER", "VSPHERE_SO_INSTALL_PASSWORD"

REJECTED, ACCEPTED, INCONCLUSIVE = "rejected", "accepted", "inconclusive"
# Recorded on the VM, not warned per VM: a site that does not give the worker its template
# passwords sees it on every machine's record (S6 lowers its verdict), not as build noise.
NOT_CONFIGURED = "the worker does not know the template password (installer setting), so it cannot try it"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def mode(env: dict | None = None) -> str:
    """``enforce`` (default: a VM whose template password works is not ready) or ``warn``."""
    value = str((os.environ if env is None else env).get(MODE_ENV, "") or "").strip().lower()
    return "warn" if value == "warn" else "enforce"


def template_login(family: str, *, securityonion: bool = False, env: dict | None = None) -> tuple[str, str | None]:
    """(username, password or None when this worker does not know it) of the golden
    template's build login for a VM of ``family``."""
    env = os.environ if env is None else env
    if securityonion:
        return str(env.get(SO_USER_ENV) or "").strip() or "soadmin", str(env.get(SO_PASSWORD_ENV) or "") or None
    if family == "windows":
        return "Administrator", str(env.get(WINDOWS_PASSWORD_ENV) or "").strip() or None
    user = str(env.get(guest.BUILD_USER_ENV) or "").strip() or "ubuntu"
    return user, str(env.get(LINUX_PASSWORD_ENV) or "") or None


def check(session_for: Callable[[guest.GuestCredentials], guest.GuestSession], family: str, *, proven: bool,
          securityonion: bool = False, env: dict | None = None) -> dict:
    """The template-login check of one VM (module docstring): exactly one login attempt.
    ``proven``: a guest-operations login with the range's own credentials worked in this VM
    in this build, so a refusal means the password is wrong, not that the guest is not up."""
    username, password = template_login(family, securityonion=securityonion, env=env)
    out = {"status": INCONCLUSIVE, "username": username}
    if password is None:
        return {**out, "detail": NOT_CONFIGURED}
    creds = guest.GuestCredentials("linux" if securityonion else family, username, guest.Secret(password))
    try:
        answer = session_for(creds).try_login()
    except Exception as exc:  # noqa: BLE001 — never a pass; the type only (the text may name the login)
        return {**out, "detail": f"the login attempt could not be made ({type(exc).__name__})"}
    if answer is True:
        return {**out, "status": ACCEPTED, "detail": "the golden template's password still logs in"}
    if answer is False and proven:
        return {**out, "status": REJECTED, "detail": "the golden template's password is refused"}
    if answer is False:
        return {**out, "detail": "refused, but the range's own login was not proven in this VM"}
    return {**out, "detail": "guest operations did not answer the login attempt"}


def report(vm_name: str, result: dict, warnings: list[str], env: dict | None = None) -> str:
    """Record one VM's check in ``warnings``; returns the not-ready reason ("" when none):
    an ``accepted`` template login in ``enforce`` mode. An unknown template password is
    silent only under ``warn``: under ``enforce`` the worker refuses to start without them
    (``startup_problem``), and a build that still meets one says so on every VM."""
    if result.get("status") == ACCEPTED:
        text = (f"Not safe: the golden template's password still logs in as {result.get('username')} "
                f"(RANGE_TEMPLATE_LOGIN_CHECK={mode(env)})")
        if mode(env) == "enforce":
            return text
        warnings.append(f"VM {vm_name}: {text}")
    elif result.get("status") == INCONCLUSIVE and (result.get("detail") != NOT_CONFIGURED or mode(env) == "enforce"):
        warnings.append(f"VM {vm_name}: template-password check inconclusive: {result.get('detail', '')}")
    return ""


VSPHERE_BACKEND = "vsphere_api"


def startup_problem(env: dict | None = None) -> str | None:
    """Why a vSphere worker in ``enforce`` mode must not start: it would build ranges whose
    template-login check can never pass (it does not know the template passwords). None when
    fine: another backend, ``warn`` mode, or both passwords configured."""
    env = os.environ if env is None else env
    if str(env.get("PROVISIONER_BACKEND") or "").strip() != VSPHERE_BACKEND or mode(env) != "enforce":
        return None
    missing = [name for name in (WINDOWS_PASSWORD_ENV, LINUX_PASSWORD_ENV) if not str(env.get(name) or "").strip()]
    if not missing:
        return None
    return (f"RANGE_TEMPLATE_LOGIN_CHECK=enforce but {', '.join(missing)} is not set on this worker: every range VM's "
            "template-login check would be inconclusive. Set the golden templates' build passwords (installer "
            "vault_template_windows_admin_password / vault_template_linux_password), or set "
            "RANGE_TEMPLATE_LOGIN_CHECK=warn to build without proof (ADR 0013 §3).")


# --------------------------------------------------------------------------- #
# Rotation
# --------------------------------------------------------------------------- #


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _ps(path: str) -> str:
    return path.replace("'", "''")


WINDOWS_SCRIPT = """$ErrorActionPreference = 'Stop'
$f = '{path}'
$failed = 0
try {{
  foreach ($line in (Get-Content -LiteralPath $f)) {{
    if (-not $line) {{ continue }}
    $u, $p = $line -split "`t", 2
    try {{ ([ADSI]("WinNT://./" + $u + ",user")).SetPassword($p) }} catch {{ $failed++ }}
  }}
}} finally {{
  Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
}}
exit $failed
"""


def commands(family: str, login: guest.GuestCredentials, data_path: str, sudo_path: str) -> guest.GuestCommand:
    """The one command that sets the uploaded passwords and deletes the files (no secret in it)."""
    if family == "windows":
        args = (f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
                f"{_encoded(WINDOWS_SCRIPT.format(path=_ps(data_path)))}")
        return guest.GuestCommand("rotate", POWERSHELL, args)
    d, s = shlex.quote(data_path), shlex.quote(sudo_path)
    if login.username == "root":
        shell = f"chpasswd < {d}; rc=$?; rm -f {d} {s}; exit $rc"
    else:
        inner = shlex.quote(f"chpasswd < {d}; rc=$?; rm -f {d}; exit $rc")
        shell = f"/usr/bin/sudo -S -p '' /bin/sh -c {inner} < {s}; rc=$?; rm -f {d} {s}; exit $rc"
    return guest.GuestCommand("rotate", "/bin/sh", "-c " + shlex.quote(shell))


def _remove(session: guest.GuestSession, family: str, data_path: str, sudo_path: str, deadline: float) -> None:
    """Best effort: delete what an interrupted rotation may have left in the guest."""
    if family == "windows":
        script = f"Remove-Item -LiteralPath '{_ps(data_path)}' -Force -ErrorAction SilentlyContinue"
        cmd = guest.GuestCommand("rotate-cleanup", POWERSHELL, f"-NoProfile -NonInteractive -EncodedCommand "
                                                               f"{_encoded(script)}", step=True)
    else:
        cmd = guest.GuestCommand("rotate-cleanup", "/bin/rm", f"-f {shlex.quote(data_path)} {shlex.quote(sudo_path)}",
                                 step=True)
    try:
        session.run(cmd, max(deadline, session._clock() + 30))
    except Exception:  # noqa: BLE001 — the guest is not reachable: nothing more to do from here
        logger.warning("could not remove a rotation's file from the guest")


def rotate(session: guest.GuestSession, family: str, changes: list[guest.RangeAccount], deadline: float,
           session_for: Callable[[guest.GuestCredentials], guest.GuestSession]) -> tuple[list[str], str]:
    """Set ``changes`` (new passwords) in the guest ``session`` is logged in to. Returns (the
    usernames whose new password was then verified by a login, an error or ""). Never raises;
    the error never holds a password."""
    hidden = [session.creds, *changes]
    tag = secrets.token_hex(8)
    if family == "windows":
        data_path, sudo_path = rf"C:\Windows\Temp\tn-rotate-{tag}.txt", ""
        payload = "".join(f"{a.username}\t{a.password.reveal()}\r\n" for a in changes)
    else:
        data_path, sudo_path = f"/tmp/tn-rotate-{tag}", f"/tmp/tn-rotate-{tag}.s"
        payload = "".join(f"{a.username}:{a.password.reveal()}\n" for a in changes)
    error = ""
    # Changing the login's own password ends the session's auth: start the change detached
    # (polling its exit code would need the old password) and let the logins below verify it.
    own = any(a.username.casefold() == session.creds.username.casefold() for a in changes)
    try:
        mode_ = None if family == "windows" else 0o600
        session.upload(data_path, payload.encode(), deadline, posix_mode=mode_)
        if family != "windows":
            session.upload(sudo_path, (session.creds.password.reveal() + "\n").encode(), deadline, posix_mode=0o600)
        cmd = commands(family, session.creds, data_path, sudo_path)
        if own:
            session.start_detached(cmd.program, cmd.arguments)
        else:
            status, code = session.run(cmd, deadline)
            if status != "ok":
                error = f"the password change ended {status} (exit {code})"
    except Exception as exc:  # noqa: BLE001 — reported with every password scrubbed
        error = guest.redact(getattr(exc, "msg", None) or str(exc) or type(exc).__name__, *hidden)[:300]
        _remove(session, family, data_path, sudo_path, deadline)
    verified = _verify(changes, family, session_for, session, deadline if own and not error else None)
    return verified, error


def _verify(changes: list[guest.RangeAccount], family: str,
            session_for: Callable[[guest.GuestCredentials], guest.GuestSession], session: guest.GuestSession,
            wait_until: float | None) -> list[str]:
    """The accounts whose new password logs in. ``wait_until``: keep trying until then (a
    change started detached may not have run yet)."""
    verified: list[str] = []
    while True:
        for acct in changes:
            if acct.username in verified:
                continue
            try:
                if session_for(guest.GuestCredentials(family, acct.username, acct.password)).try_login() is True:
                    verified.append(acct.username)
            except Exception:  # noqa: BLE001 — not verified: recorded as not applied
                continue
        if len(verified) == len(changes) or wait_until is None or session._clock() >= wait_until:
            return [a.username for a in changes if a.username in verified]
        session._sleep(min(session._poll, max(0.0, wait_until - session._clock())))
