"""TrueNorth Range - Security Onion 2.4 per-range setup, over VMware guest operations.

The ``securityonion`` golden template is the OS only: the Security Onion 2.4 ISO's Oracle
Linux 9 install, with ``so-setup`` not run (ADR 0012, decision 8). Setup bakes the
hostname, management address and certificates into the node and cannot be redone after,
so it runs per range, at deploy. Each VM whose catalogue id is ``securityonion`` becomes a
STANDALONE node:

- management on its first NIC that is not the monitor NIC (soc-training: ``soc_tools``),
  static address, gateway and DNS from the range plan;
- monitoring on the NIC on the range's mirror destination (the zone a template
  ``action: mirror`` rule targets; soc-training: ``network_monitoring``), or on a NIC the
  template flags ``monitor: true``. The guest finds both NICs by MAC address, never by
  device order;
- airgap: everything installs from the ISO (/nsm/repo), nothing is fetched. A range
  has no internet, only the depot.

How it is unattended
--------------------
Security Onion 2.4 has no answer file. The 2.3 ``setup/automation/`` files are gone, and
so-setup reads all its answers from whiptail prompts. The one non-interactive path is its
second argument, a *test profile*. With a test profile, so-setup sets ``TESTING``, which
makes every prompt return at once, and hard-codes the answers in one block (DHCP, first NIC
management, ``onionuser@somewhere.invalid`` / ``0n10nus3r``). That block ends with
``update_sudoers_for_testing``, which gives the install user password-less sudo.

The runner (``RUNNER_SCRIPT``) copies so-setup next to the original (its relative
``source`` lines need that) and replaces that one line with ``. /root/tn-so-answers.env``,
this module's answer file. So the range's values override the hard-coded ones, and the
install user's sudo rights are left alone. The copy runs as
``so-setup-truenorth iso standalone-truenorth-airgap``. A so-setup whose layout differs
(not exactly one such line) is refused, never half-patched: status 12.

With ``TESTING`` set, a value that fails one of so-setup's checks is "re-prompted" by a
prompt that returns at once, which loops forever. So every answer is checked here first
(``plan``) against the same rules: hostname, IPv4, DNS list, search domain, the web
user's e-mail form, and the web password's rules (8-72 characters, no ``' " $ \\``).

Sources (Security Onion 2.4, release 2.4.211, the ISO the template is built from):
docs.securityonion.net/en/2.4/installation.html, /airgap.html, /vmware.html; so-setup,
so-functions, so-whiptail, so-verify and so-user on the securityonion repository's
``2.4/main`` branch.

Running and reporting
---------------------
Setup takes 20 to 60 minutes and must outlive a guest-operations session, so it runs
detached. One root command (sudo with the install login) writes the answer file and the
runner, then starts the runner with ``setsid nohup``. The runner writes a number to
``/var/lib/tn-so-setup/status`` (``STATUS``). The worker reads that number with a plain
``exit $(cat ...)`` as the install user, because guest operations return an exit code,
never output. The worker polls until the node is healthy (``so-status -q``),
VSPHERE_SO_SETUP_TIMEOUT runs out, or the provision budget ends. Setup carries on in the
guest after any of these.

Nothing here fails the range. Every outcome other than ``ok`` is a provisioner warning,
and the VM's output carries ``securityonion = {status, detail, ...}``.

Credentials
-----------
The range's declared accounts for the machine (``accounts:`` in the range template,
ADR 0012 decision 12) reach the worker as ``vm_def["_accounts"]``
(worker/range_accounts.py: ``RangeAccount``, ordered by username). Two rules apply here:

- **Admin**: the first ``role: admin`` account other than the build login is the node's
  OS administrator. Setup creates it if the template lacks it, sets its password and puts
  it in ``wheel``, before so-setup runs.
- **Build login** (``soadmin``, VSPHERE_SO_INSTALL_USER): the range has an account for it
  (declared, or the implicit staff login the API adds, ADR 0013 §3). Its range password
  replaces the template's last, once the node is healthy (``admin_last``), by the build or
  the reconcile task; the template-login check (vsphere_credcheck) then proves it.
- **SOC web login**: the first ``role: user`` account (by username) is the SOC console's
  administrator. so-setup wants an e-mail address: a username without ``@`` gets
  ``@<search domain>`` (``analyst`` -> ``analyst@range.local``). Declare it with the ``@``,
  so the range's Credentials panel shows the name that is typed in. With no ``role: user``
  account, the login is ``analyst@<domain>`` with a password generated here.

Guest operations (and sudo) log in with the template's build login when the worker has
one: VSPHERE_SO_INSTALL_USER / VSPHERE_SO_INSTALL_PASSWORD, the template's generic build
password (ADR 0012 decision 5, which ADR 0013 limits to the template's construction). A
fresh clone has no range account yet; setup is what creates it. Without a build login the
admin account itself is used, which works only for a template built with that account and
password. With neither, setup is skipped.

An account is reported applied (``accounts_applied``, worker/range_accounts.record_applied)
only once verified in the guest: the admin by ``getent passwd``, the web login by
``so-user list``. Other declared accounts on the machine are not set, and get a warning.

A password generated here is never in a log, a warning or an error. Every role can read a
range's provisioner output (RANGE_READ), and observers and Students must not see it
(decision 13). So the output holds it only sealed (secretbox, TN_SECRETS_KEY), and the
guest keeps it in ``/root/tn-so-web-credentials`` (root, 0600). Declared passwords are on
the range's Credentials panel already.
- The sudo password and the answer file pass through guest-operations program arguments:
  base64, visible to a local ``ps`` for the seconds the staging command runs. vSphere has
  no other way to send data to a guest without an HTTPS file transfer to the ESXi host,
  which the worker might not reach. The answer file is deleted when setup ends.
"""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import logging
import os
import re
import secrets
import shlex
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import secretbox
from . import vsphere_guest as guest
from . import vsphere_infra as infra

logger = logging.getLogger(__name__)

CATALOGUE_ID = "securityonion"
# Seconds from the start of a VM's setup until the setup is a ``timeout``. The build does
# not wait this long: it waits at most VSPHERE_SETUP_BUILD_WAIT, records ``running`` and lets
# the reconcile task (worker/setup_reconcile.py) read the status file and verify the accounts.
VSPHERE_SO_SETUP_TIMEOUT: int = int(os.environ.get("VSPHERE_SO_SETUP_TIMEOUT", "7200"))
VSPHERE_SETUP_BUILD_WAIT: float = float(os.environ.get("VSPHERE_SETUP_BUILD_WAIT", "0"))
# Seconds between status reads (each one a short guest-operations process).
VSPHERE_SO_POLL: float = float(os.environ.get("VSPHERE_SO_POLL", "30"))
# Seconds the runner waits, after so-setup succeeds, for so-status to report every container up.
SO_HEALTH_WAIT = 1800
MIN_START = 30  # seconds of budget the staging command needs; less, and setup is not started

STATE_DIR = "/var/lib/tn-so-setup"
STATUS_FILE = f"{STATE_DIR}/status"
ANSWERS_FILE = "/root/tn-so-answers.env"
RUNNER_FILE = "/root/tn-so-setup.sh"
RUNNER_LOG = "/root/tn-so-setup.log"
CREDENTIALS_FILE = "/root/tn-so-web-credentials"
TEST_PROFILE = "standalone-truenorth-airgap"
DEFAULT_DOMAIN = "range.local"
DEFAULT_WEB_USER = "analyst"

# The runner's status numbers (``exit $(cat status)``).
STATUS = {
    0: "healthy: so-status reports every container up",
    2: "so-setup is running",
    3: "so-setup finished; waiting for so-status to report every container up",
    10: "the management or monitor NIC's MAC address is not on any interface in the guest",
    11: "so-setup was not found in the guest (is this the Security Onion 2.4 ISO install?)",
    12: "this so-setup's layout is not the one the unattended run was written for (2.4.211); not run",
    13: f"so-setup failed; see /root/sosetup.log and /root/errors.log ({RUNNER_LOG}) in the guest",
    14: f"so-setup finished but so-status did not report every container up within {SO_HEALTH_WAIT}s",
    15: f"the setup runner stopped unexpectedly; see {RUNNER_LOG} in the guest",
    16: f"could not create the range's admin account or set its password; see {RUNNER_LOG} in the guest",
}
RUNNING = frozenset({2, 3})

# so-setup's own checks (so-common: valid_hostname, valid_fqdn; so-user: validateEmail,
# validatePassword / check_password). Under a test profile a value that fails one loops forever.
_HOSTNAME = re.compile(r"^[a-z0-9-]{1,63}$")
_FQDN = re.compile(r"(?=^.{4,253}$)(^((?!-)[a-zA-Z0-9-]{0,62}[a-zA-Z0-9]\.)+[a-zA-Z]{2,63}$)")
_EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")
_PASSWORD_FORBIDDEN = set("'\"$\\\n\r")


RUNNER_SCRIPT = r"""#!/bin/bash
# TrueNorth Range: unattended Security Onion 2.4 STANDALONE setup, airgap (offline, from the ISO).
# Written by the TrueNorth worker (worker/provisioners/vsphere_securityonion.py); runs once, as root.
# Progress for the worker: a number in __STATUS__ (vsphere_securityonion.STATUS).
set -u
umask 077
STATE=__STATE__
ANSWERS=__ANSWERS__
PATCHED=
DONE=
exec >>__LOG__ 2>&1
status() { printf '%s\n' "$1" > "$STATE/status.new" && chmod 644 "$STATE/status.new" && mv -f "$STATE/status.new" "$STATE/status"; }
cleanup() { [ -n "$DONE" ] || status 15; rm -f "$ANSWERS"; [ -z "$PATCHED" ] || rm -f "$PATCHED"; }
finish() { status "$1"; DONE=1; exit 0; }
trap cleanup EXIT
mkdir -p "$STATE" && chmod 755 "$STATE"
status 2
# shellcheck disable=SC1090
. "$ANSWERS"
nic_by_mac() {
  for d in /sys/class/net/*; do
    if [ "$(cat "$d/address" 2>/dev/null)" = "$1" ]; then basename "$d"; return 0; fi
  done
  return 1
}
MGMT_IF=$(nic_by_mac "$TN_MGMT_MAC") || finish 10
MON_IF=$(nic_by_mac "$TN_MON_MAC") || finish 10
echo "$(date -Is) management NIC $MGMT_IF ($TN_MGMT_MAC), monitor NIC $MON_IF ($TN_MON_MAC)"
# so-setup sets the management address on the NetworkManager connection named after the NIC.
nmcli -t -f NAME con show | grep -qx "$MGMT_IF" || nmcli con add type ethernet ifname "$MGMT_IF" con-name "$MGMT_IF"
printf 'MNIC=%s\nBNICS=(%s)\n' "$MGMT_IF" "$MON_IF" >> "$ANSWERS"
# The range's admin account (role: admin): created if the template lacks it, sudo through wheel.
if [ -n "$TN_ADMIN_USER" ]; then
  id "$TN_ADMIN_USER" >/dev/null 2>&1 || useradd -m "$TN_ADMIN_USER" || finish 16
  printf '%s:%s\n' "$TN_ADMIN_USER" "$TN_ADMIN_PASSWORD" | chpasswd || finish 16
  usermod -aG wheel "$TN_ADMIN_USER" || finish 16
  echo "$(date -Is) admin account $TN_ADMIN_USER set"
fi
SETUP=
for c in /root/manager_setup/securityonion/setup/so-setup "/home/$TN_INSTALL_USER/SecurityOnion/setup/so-setup" \
         /root/SecurityOnion/setup/so-setup; do
  if [ -f "$c" ]; then SETUP=$c; break; fi
done
[ -n "$SETUP" ] || finish 11
[ "$(grep -c '^[[:space:]]*update_sudoers_for_testing[[:space:]]*$' "$SETUP")" = 1 ] || finish 12
PATCHED="$(dirname "$SETUP")/so-setup-truenorth"
sed "s|^\([[:space:]]*\)update_sudoers_for_testing[[:space:]]*\$|\1. $ANSWERS|" "$SETUP" > "$PATCHED" || finish 12
grep -q "^[[:space:]]*\. $ANSWERS\$" "$PATCHED" || finish 12
cd "$(dirname "$SETUP")" || finish 11
echo "$(date -Is) so-setup ($SETUP) iso __PROFILE__"
SUDO_USER="$TN_INSTALL_USER" bash "$PATCHED" iso __PROFILE__ </dev/null
echo "$(date -Is) so-setup exited $?"
[ -f /root/success ] || finish 13
if [ "$TN_WEB_GENERATED" = 1 ]; then
  printf 'SOC web login: %s\npassword: %s\n' "$WEBUSER" "$WEBPASSWD1" > __CREDS__ && chmod 600 __CREDS__
fi
export PATH="$PATH:/usr/sbin:/usr/local/sbin"
for cidr in $TN_EXTRA_ALLOW; do
  so-firewall includehost analyst "$cidr" --apply || echo "so-firewall includehost analyst $cidr failed"
done
status 3
end=$((SECONDS + TN_HEALTH_WAIT))
until so-status -q; do
  [ "$SECONDS" -ge "$end" ] && finish 14
  sleep 30
done
finish 0
"""
for _key, _value in {"__STATUS__": STATUS_FILE, "__STATE__": STATE_DIR, "__ANSWERS__": ANSWERS_FILE,
                     "__LOG__": RUNNER_LOG, "__PROFILE__": TEST_PROFILE, "__CREDS__": CREDENTIALS_FILE}.items():
    RUNNER_SCRIPT = RUNNER_SCRIPT.replace(_key, _value)


def is_securityonion(vm_def: dict) -> bool:
    """A VM built from the ``securityonion`` catalogue entry (or a host-local copy of it)."""
    os_name = str(vm_def.get("os") or "").strip().lower()
    tmpl = str(vm_def.get("template_name") or "").strip().lower()
    return os_name == CATALOGUE_ID or tmpl == CATALOGUE_ID or tmpl.startswith(CATALOGUE_ID + "@")


# --------------------------------------------------------------------------- #
# Plan: every answer, checked
# --------------------------------------------------------------------------- #


@dataclass
class SetupPlan:
    hostname: str
    mgmt_index: int
    monitor_index: int
    mgmt_ip: str
    prefix: int
    gateway: str
    dns: list[str]
    search: str
    allow: list[str]  # CIDRs that may reach the web console; the first is so-setup's ALLOW_CIDR
    web_user: str
    web_password: guest.Secret = field(repr=False)
    web_generated: bool
    install: guest.GuestCredentials = field(repr=False)
    install_source: str
    # The range's first role: admin account other than the build login: set before so-setup.
    admin: guest.RangeAccount | None = field(default=None, repr=False)
    notes: list[str] = field(default_factory=list)
    # The range's account for the build login itself (``soadmin``: declared, or the implicit
    # staff login, ADR 0013 §3): its new password is set last, once the node is healthy.
    login_account: guest.RangeAccount | None = field(default=None, repr=False)

    @property
    def admin_last(self) -> bool:
        """The build login gets the range's password for it, last (it ends the login)."""
        return (self.login_account is not None
                and self.login_account.password.reveal() != self.install.password.reveal())


class PlanError(ValueError):
    """The VM cannot be set up unattended; the message says why (it never holds a secret)."""


def _accounts(vm_def: dict) -> list[guest.RangeAccount]:
    """The machine's declared accounts (worker/range_accounts.py), in the order given."""
    return [a for a in vm_def.get("_accounts") or [] if isinstance(a, guest.RangeAccount) and a.username]


def admin_account(vm_def: dict, login_user: str | None = None) -> guest.RangeAccount | None:
    """The first ``role: admin`` account other than ``login_user``: the node's OS administrator."""
    return next((a for a in _accounts(vm_def) if a.admin and a.username != login_user), None)


def login_account(vm_def: dict, login_user: str) -> guest.RangeAccount | None:
    """The range's account for the build login itself, if it has one (ADR 0013 §3)."""
    return next((a for a in _accounts(vm_def) if a.username == login_user), None)


def web_account(vm_def: dict) -> guest.RangeAccount | None:
    """The first ``role: user`` account: the SOC console's web login."""
    return next((a for a in _accounts(vm_def) if not a.admin), None)


def install_login(vm_def: dict, env: dict | None = None) -> tuple[guest.GuestCredentials, str]:
    """The login guest operations (and sudo) use: the template's build login
    (VSPHERE_SO_INSTALL_USER / VSPHERE_SO_INSTALL_PASSWORD), else the range's admin account.
    The build login's own range row (``login_account``) replaces that password last."""
    env = os.environ if env is None else env
    user = str(env.get("VSPHERE_SO_INSTALL_USER") or "").strip()
    password = str(env.get("VSPHERE_SO_INSTALL_PASSWORD") or "")
    if user and password:
        return guest.GuestCredentials("linux", user, guest.Secret(password)), "VSPHERE_SO_INSTALL_USER"
    if admin := admin_account(vm_def):
        return guest.GuestCredentials("linux", admin.username, admin.password), "range admin account"
    raise PlanError("no install login: VSPHERE_SO_INSTALL_USER / VSPHERE_SO_INSTALL_PASSWORD are not set and "
                    "the range declares no role: admin account for this machine")


def _password_problem(password: str) -> str | None:
    if not 8 <= len(password) <= 72:
        return "must be 8 to 72 characters"
    if set(password) & _PASSWORD_FORBIDDEN:
        return "must not contain quotes, dollar signs, backslashes or line breaks"
    return None


def web_login(vm_def: dict, domain: str) -> tuple[str, guest.Secret, bool]:
    """(e-mail, password, generated) of the SOC web admin. so-setup needs an e-mail address."""
    acc = web_account(vm_def)
    user = acc.username.strip().lower() if acc else DEFAULT_WEB_USER
    if "@" not in user:
        user = f"{user}@{domain}"
    if not _EMAIL.match(user):
        raise PlanError(f"web login {user!r} is not an e-mail address so-setup accepts")
    if acc is None:
        return user, guest.Secret(secrets.token_urlsafe(18)), True
    if problem := _password_problem(acc.password.reveal()):
        raise PlanError(f"the web password of {user} {problem} (so-setup's rules)")
    return user, acc.password, False


def pick_nics(vm_def: dict, mirror_dst_vlans: set[int]) -> tuple[int, int]:
    """(management, monitor) NIC indexes, in the VM's NIC order.

    The monitor NIC is one the template flags ``monitor: true``, else the one on a mirror
    destination zone, else the one on a sensor (promiscuous) network. Management is the
    first other NIC with an address."""
    nics = vm_def.get("nics") or []
    if len(nics) < 2:
        raise PlanError("a standalone node needs two NICs, management and monitor; the template gives this "
                        "machine one (add an `interfaces:` entry for the monitoring zone with `monitor: true`)")
    monitor = next((i for i, n in enumerate(nics) if n.get("monitor")), None)
    if monitor is None:
        monitor = next((i for i, n in enumerate(nics) if n.get("vlan") is not None
                        and int(n["vlan"]) in mirror_dst_vlans), None)
    if monitor is None:
        monitor = next((i for i, n in enumerate(nics) if infra.wants_promiscuous(str(n.get("network") or ""))), None)
    if monitor is None:
        raise PlanError("no monitor NIC: none is flagged `monitor: true`, on a mirror destination or on a "
                        "monitoring network")
    mgmt = next((i for i, n in enumerate(nics) if i != monitor and n.get("ip")), None)
    if mgmt is None:
        raise PlanError("no management NIC: no NIC other than the monitor NIC has an address")
    return mgmt, monitor


def _allow(entries, networks: list[dict], notes: list[str]) -> list[str]:
    by_name = {str(n.get("name")): str(n.get("cidr") or "") for n in networks if not n.get("rspan")}
    out = []
    for raw in entries or []:
        value = by_name.get(str(raw), str(raw))
        try:
            out.append(str(ipaddress.IPv4Network(value, strict=False)))
        except ValueError:
            notes.append(f"so_allow entry {raw!r} is neither a range zone nor an IPv4 CIDR (skipped)")
    return out


def plan(vm_def: dict, networks: list[dict], mirror_dst_vlans: set[int], env: dict | None = None) -> SetupPlan:
    """Every so-setup answer for this VM, checked against so-setup's own rules (PlanError)."""
    mgmt_i, mon_i = pick_nics(vm_def, mirror_dst_vlans)
    nic = vm_def["nics"][mgmt_i]
    try:
        iface = ipaddress.IPv4Interface(f"{nic['ip']}/{int(nic.get('prefix') or 24)}")
        gateway = str(ipaddress.IPv4Address(str(nic.get("gateway") or "")))
        dns = [str(ipaddress.IPv4Address(str(d))) for d in (vm_def.get("dns") or [gateway])]
    except (ValueError, TypeError) as exc:
        raise PlanError(f"management NIC address, gateway or DNS is not valid IPv4: {exc}") from None
    host = infra.hostname(vm_def).lower()
    if not _HOSTNAME.match(host) or host == "localhost":
        raise PlanError(f"hostname {host!r} is not one so-setup accepts")
    search = str((vm_def.get("dns_search") or [DEFAULT_DOMAIN])[0]).lower()
    if not _FQDN.match(search):
        raise PlanError(f"DNS search domain {search!r} is not one so-setup accepts")
    install, source = install_login(vm_def, env)
    user, password, generated = web_login(vm_def, search)
    notes: list[str] = []
    admin = admin_account(vm_def, install.username)
    if admin is not None and not guest.POSIX_USERNAME.match(admin.username):
        notes.append(f"admin account {admin.username!r} is not a Linux login name; not set")
        admin = None
    elif admin is not None and set(admin.password.reveal()) & set("\n\r"):
        notes.append(f"admin account {admin.username!r}: a password with a line break cannot be set; not set")
        admin = None
    login = login_account(vm_def, install.username)
    if login is not None and set(login.password.reveal()) & set("\n\r:"):
        notes.append(f"account {login.username!r}: a password with a line break or colon cannot be set; not set")
        login = None
    allow = [str(iface.network)]
    allow += [c for c in _allow(vm_def.get("so_allow"), networks, notes) if c not in allow]
    return SetupPlan(hostname=host, mgmt_index=mgmt_i, monitor_index=mon_i, mgmt_ip=str(iface.ip),
                     prefix=iface.network.prefixlen, gateway=gateway, dns=dns, search=search, allow=allow,
                     web_user=user, web_password=password, web_generated=generated, install=install,
                     install_source=source, admin=admin, notes=notes, login_account=login)


# --------------------------------------------------------------------------- #
# Guest commands
# --------------------------------------------------------------------------- #


def answers(p: SetupPlan, macs: list[str], health_wait: int = SO_HEALTH_WAIT) -> str:
    """The answer file so-setup sources in place of its test-profile sudoers line."""
    q = shlex.quote
    pw = p.web_password.reveal()
    return "\n".join([
        "# TrueNorth Range: Security Onion setup answers (worker/provisioners/vsphere_securityonion.py).",
        "# Sourced by so-setup in place of update_sudoers_for_testing; deleted when setup ends.",
        f"TN_INSTALL_USER={q(p.install.username)}",
        f"TN_MGMT_MAC={q(macs[p.mgmt_index].lower())}",
        f"TN_MON_MAC={q(macs[p.monitor_index].lower())}",
        f"TN_EXTRA_ALLOW={q(' '.join(p.allow[1:]))}",
        f"TN_HEALTH_WAIT={int(health_wait)}",
        f"TN_WEB_GENERATED={1 if p.web_generated else 0}",
        # The admin account is set before so-setup; the login's own password last, by the worker.
        f"TN_ADMIN_USER={q(p.admin.username if p.admin else '')}",
        f"TN_ADMIN_PASSWORD={q(p.admin.password.reveal() if p.admin else '')}",
        "install_type=STANDALONE",
        f"HOSTNAME={q(p.hostname)}",
        f"NODE_DESCRIPTION={q('TrueNorth range sensor')}",
        "address_type=STATIC",
        f"manager_ip_mask={q(f'{p.mgmt_ip}/{p.prefix}')}",
        f"MGATEWAY={q(p.gateway)}",
        f"MDNS={q(','.join(p.dns))}",
        f"MSEARCH={q(p.search)}",
        "is_airgap=true",
        "REDIRECTINFO=IP",
        f"ALLOW_CIDR={q(p.allow[0])}",
        "MINION_CIDR=",
        f"WEBUSER={q(p.web_user)}",
        f"WEBPASSWD1={q(pw)}",
        f"WEBPASSWD2={q(pw)}",
        "",
    ])


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode("ascii")


def as_root(label: str, script: str, creds: guest.GuestCredentials) -> guest.GuestCommand:
    """``script`` as root: directly for a root login, else through ``sudo -S`` (the
    template's install user may need its password for sudo)."""
    if creds.username == "root":
        return guest.GuestCommand(label, "/bin/sh", "-c " + shlex.quote(script), step=True)
    shell = (f"printf '%s\\n' {shlex.quote(creds.password.reveal())} | "
             f"/usr/bin/sudo -S -p '' /bin/sh -c {shlex.quote(script)}")
    return guest.GuestCommand(label, "/bin/sh", "-c " + shlex.quote(shell), step=True)


def stage_script(answer_text: str) -> str:
    """Write the answer file and the runner, then start the runner detached. A guest where
    setup was already started (a status file exists) is left alone: never run so-setup twice."""
    return "; ".join([
        "set -e", "umask 077",
        f"if [ -e {STATUS_FILE} ]; then exit 0; fi",
        f"mkdir -p {STATE_DIR}", f"chmod 755 {STATE_DIR}",
        f"printf %s {_b64(answer_text)} | base64 -d > {ANSWERS_FILE}",
        f"printf %s {_b64(RUNNER_SCRIPT)} | base64 -d > {RUNNER_FILE}",
        f"chmod 700 {RUNNER_FILE}",
        f"printf '2\\n' > {STATUS_FILE}", f"chmod 644 {STATUS_FILE}",
        f"setsid nohup /bin/bash {RUNNER_FILE} </dev/null >/dev/null 2>&1 &",
    ])


def poll_command() -> guest.GuestCommand:
    """Exit with the runner's status number; no root needed, no secret on the command line."""
    shell = (f"c=$(cat {STATUS_FILE} 2>/dev/null); case \"$c\" in ''|*[!0-9]*) c=2;; esac; exit \"$c\"")
    return guest.GuestCommand("so-setup-status", "/bin/sh", "-c " + shlex.quote(shell), frozenset({0}), step=True)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def _scrub(text: str, *values: guest.Secret | None) -> str:
    out = str(text)
    for v in values:
        if v is not None and v.reveal():
            out = out.replace(v.reveal(), "***")
    return out


def _sealed(password: guest.Secret) -> str | None:
    try:
        return secretbox.seal(password.reveal())
    except secretbox.SecretKeyMissingError:
        return None


def setup(session_for: Callable[[guest.GuestCredentials], object], vm_def: dict, macs: list[str],
          networks: list[dict], mirror_dst_vlans: set[int], deadline: float, budget_end: float, *,
          poll: float | None = None, env: dict | None = None,
          clock: Callable[[], float] | None = None, build_wait: float | None = None) -> tuple[dict, list[str]]:
    """Set the VM up as a standalone node; returns (its ``securityonion`` output, warnings).

    ``deadline``: VSPHERE_SO_SETUP_TIMEOUT from now; ``budget_end``: the provision budget.
    The build waits at most ``build_wait`` (VSPHERE_SETUP_BUILD_WAIT) for a healthy node, then
    records ``running`` with a reconcile block for the reconcile task to finish.
    ``session_for(creds)`` opens a GuestSession as the install login.

    ``out["accounts_applied"]``: the machine's declared accounts verified in the guest
    (usernames only; worker/range_accounts.record_applied): the admin by ``getent passwd``,
    the SOC web login by ``so-user list``. Any other declared account gets a warning and
    stays not applied."""
    out, warnings, applied = _run(session_for, vm_def, macs, networks, mirror_dst_vlans, deadline, budget_end,
                                  poll=poll, env=env, clock=clock, build_wait=build_wait)
    declared = [a.username for a in _accounts(vm_def)]
    out["accounts_applied"] = [n for n in declared if n in applied]
    if missing := [n for n in declared if n not in applied]:
        warnings.append(f"VM {vm_def.get('name') or ''}: account(s) not verified, recorded as not set: "
                        f"{', '.join(missing)}")
    return out, warnings


def _run(session_for, vm_def: dict, macs: list[str], networks: list[dict], mirror_dst_vlans: set[int],
         deadline: float, budget_end: float, *, poll: float | None, env: dict | None,
         clock: Callable[[], float] | None, build_wait: float | None = None) -> tuple[dict, list[str], set[str]]:
    """``setup`` without the account report: (output, warnings, declared usernames verified)."""
    name = str(vm_def.get("name") or "")
    tag = f"VM {name}: Security Onion setup"
    out: dict = {"status": "skipped", "detail": "", "mode": "standalone, airgap"}
    warnings: list[str] = []
    try:
        p = plan(vm_def, networks, mirror_dst_vlans, env)
    except PlanError as exc:
        out["detail"] = str(exc)
        return out, [f"{tag} skipped: {exc}"], set()
    if len(macs) < len(vm_def.get("nics") or []):
        out["detail"] = f"the VM has {len(macs)} NICs, its plan {len(vm_def['nics'])}"
        return out, [f"{tag} skipped: {out['detail']}"], set()
    hidden = (p.install.password, p.web_password, p.admin.password if p.admin else None,
              p.login_account.password if p.login_account else None)
    warnings += [f"{tag}: {n}" for n in p.notes]
    out.update({"web_url": f"https://{p.mgmt_ip}", "web_user": p.web_user,
                "web_password": "generated" if p.web_generated else "range account",
                "management_mac": macs[p.mgmt_index].lower(), "monitor_mac": macs[p.monitor_index].lower(),
                "allow": p.allow, "admin_user": p.admin.username if p.admin else None,
                "login_user": p.login_account.username if p.login_account else None})
    if p.web_generated:
        if sealed := _sealed(p.web_password):
            out["web_password_sealed"] = sealed
        where = (f"sealed in this range's output (securityonion.web_password_sealed) and in "
                 f"{CREDENTIALS_FILE} in the guest" if sealed else
                 f"in {CREDENTIALS_FILE} in the guest only (TN_SECRETS_KEY is not set, so it is not sealed)")
        warnings.append(f"VM {name}: Security Onion web login {p.web_user} has a generated password, {where}; "
                        "declare a role: user account for this machine in the range to choose it")
    session = session_for(p.install)
    now = (clock or session._clock)
    at = _time.time
    wait = poll if poll is not None else VSPHERE_SO_POLL
    bw = VSPHERE_SETUP_BUILD_WAIT if build_wait is None else build_wait
    stop = min(deadline, budget_end)
    since = at()
    build_stop = min(stop, now() + bw)
    if budget_end - now() < MIN_START:
        out["detail"] = "the provision time budget (VSPHERE_PROVISION_BUDGET) is spent; setup not started"
        return out, [*warnings, f"{tag} skipped: {out['detail']}"], set()

    def fail(state: str, detail: str, started: bool = True) -> tuple[dict, list[str], set[str]]:
        out["status"], out["detail"] = state, _scrub(detail, *hidden)[:400]
        warnings.append(f"{tag} {state}: {out['detail']}")
        # The runner sets the admin account before so-setup: it may exist although setup did not finish.
        verified = _verified_admin(session, p, stop, hidden) if started else set()
        if p.admin_last:
            warnings.append(f"{tag}: the password of admin account {p.login_account.username} (also the build "
                            "login) is set only once the node is healthy; it is unchanged")
        return out, warnings, verified

    try:
        session.wait_ready(stop)
        result, code = session.run(as_root("so-setup-start", stage_script(answers(p, macs)), p.install), stop)
    except Exception as exc:  # noqa: BLE001 -- reported (scrubbed) as a warning, never raised past the VM
        return fail("failed", f"could not start: {getattr(exc, 'msg', None) or exc or type(exc).__name__}", False)
    if result != "ok":
        return fail("failed", f"could not start (staging command {result}, exit {code}); does the install "
                              f"login {p.install.username} have sudo?", False)
    logger.info("security onion setup on %s: started (standalone, airgap)", name)
    last = 2
    while True:
        try:
            result, code = session.run(poll_command(), min(stop, now() + 120))
        except Exception as exc:  # noqa: BLE001 -- a guest-ops hiccup; the deadline bounds the loop
            logger.warning("security onion status read on %s failed: %s", name, _scrub(str(exc), *hidden))
            result, code = "failed", None
        if code == 0:
            out["status"], out["detail"] = "ok", STATUS[0]
            logger.info("security onion setup on %s: healthy", name)
            # Verify before the login's own password may change (the session's last command).
            verified = _verified_web(session, p, vm_def, stop, hidden) | _verified_admin(session, p, stop, hidden)
            if p.admin_last:
                note = _set_admin_last(session, session_for, p, stop, hidden)
                warnings += note
                if not note:  # chpasswd for an account that is the login: it exists
                    verified.add(p.login_account.username)
            elif p.login_account is not None:  # the range's password for the login is the one it has
                verified.add(p.login_account.username)
            return out, warnings, verified
        if code is not None and code not in RUNNING:
            return fail("failed", STATUS.get(code, f"setup runner status {code}"))
        if code in RUNNING:
            last = code
        if now() >= build_stop:
            break
        with contextlib.suppress(Exception):
            session._sleep(max(0.0, min(wait, build_stop - now())))
    # Not healthy within the build's wait: leave it running for the reconcile task, unless the
    # whole timeout is already spent (then it is a timeout even in the build).
    if now() >= stop and deadline <= budget_end:
        return fail("timeout", f"not healthy within VSPHERE_SO_SETUP_TIMEOUT; last status: {STATUS[last]} "
                               f"(it carries on in the guest; check {STATUS_FILE})")
    out, warnings2, _ = fail("running", f"last status: {STATUS[last]}; continues in the guest, the reconcile "
                                        f"task records the result (check {STATUS_FILE})")
    acc = web_account(vm_def)
    out["reconcile"] = {"kind": "securityonion", "status_file": STATUS_FILE, "since": since,
                        "timeout": VSPHERE_SO_SETUP_TIMEOUT, "web_user": p.web_user,
                        # The declared username, as the build records it (_verified_web), not the e-mail.
                        "web_account": acc.username if acc is not None and not p.web_generated else None,
                        "web_generated": p.web_generated, "admin_user": p.admin.username if p.admin else None,
                        "login_user": p.login_account.username if p.admin_last else None,
                        "admin_last": p.admin_last, "node": str(vm_def.get("node_id") or ""),
                        "install_user": p.install.username,
                        "install_from_env": p.install_source == "VSPHERE_SO_INSTALL_USER"}
    # Nothing is applied yet: the reconcile task verifies the accounts once the node is healthy.
    return out, warnings2, set()


def admin_check_command(username: str) -> guest.GuestCommand:
    """Exit 0 when the account exists (no root needed, no secret)."""
    return guest.GuestCommand("so-admin-check", "/bin/sh",
                              "-c " + shlex.quote(f"getent passwd {shlex.quote(username)} >/dev/null"), step=True)


def web_check_script(email: str) -> str:
    """Exit 0 when so-user lists the SOC web login (``email: roles`` per line)."""
    return f"/usr/sbin/so-user list 2>/dev/null | cut -d: -f1 | grep -qxF {shlex.quote(email)}"


def _check(session, cmd: guest.GuestCommand, stop: float, hidden) -> bool:
    try:
        result, _ = session.run(cmd, max(stop, session._clock() + 120))
    except Exception as exc:  # noqa: BLE001 -- not verified: recorded as not set
        logger.warning("security onion account check %s failed: %s", cmd.label, _scrub(str(exc), *hidden))
        return False
    return result == "ok"


def _verified_admin(session, p: SetupPlan, stop: float, hidden) -> set[str]:
    if p.admin is None:
        return set()
    return {p.admin.username} if _check(session, admin_check_command(p.admin.username), stop, hidden) else set()


def _verified_web(session, p: SetupPlan, vm_def: dict, stop: float, hidden) -> set[str]:
    acc = web_account(vm_def)
    if acc is None or p.web_generated:
        return set()
    cmd = as_root("so-web-check", web_check_script(p.web_user), p.install)
    return {acc.username} if _check(session, cmd, stop, hidden) else set()


def change_login_password(session, session_for, install: guest.GuestCredentials, username: str,
                          password: guest.Secret, stop: float, hidden) -> bool:
    """Give the login ``session`` runs as its range password, and verify it by a login with it.

    Started detached: once chpasswd ran, polling the command's exit code would still
    authenticate with the old password and fail until the deadline. Waits at most two
    minutes (never past ``stop`` once that is later) for the new password to log in."""
    q = shlex.quote
    cmd = as_root("so-admin-password", f"printf '%s:%s\\n' {q(username)} {q(password.reveal())} | chpasswd", install)
    try:
        session.start_detached(cmd.program, cmd.arguments)
    except Exception as exc:  # noqa: BLE001
        logger.warning("security onion login password change not started: %s", _scrub(str(exc), *hidden))
        return False
    return _login_works(session_for, username, password, max(stop, session._clock() + 120))


def _set_admin_last(session, session_for, p: SetupPlan, stop: float, hidden) -> list[str]:
    """Give the build login its range password (it replaces the template's). Returns warnings."""
    acct = p.login_account
    if change_login_password(session, session_for, p.install, acct.username, acct.password, stop, hidden):
        return []
    return [f"VM {p.hostname}: Security Onion admin account {acct.username}: its declared password was not set"]


def reconcile(session_for: Callable[[guest.GuestCredentials], object], install: guest.GuestCredentials,
              info: dict, *, admin_password: guest.Secret | None = None, clock: Callable[[], float] | None = None,
              epoch: Callable[[], float] | None = None, deadline: float | None = None,
              login_password: guest.Secret | None = None) -> tuple[dict | None, set]:
    """Read a ``running`` Security Onion setup's status (worker/setup_reconcile.py) as the
    install login. Returns (final outcome or None, the DECLARED usernames verified in the guest).

    The timeout is checked first, before any guest operation, so a powered-off or unreachable
    sensor still ends. On a healthy node, as the build does: the SOC web login by ``so-user
    list`` (recorded under its declared username, ``web_account``); the admin account, recorded
    applied once a guest-operations login with its password works; then the build login's own
    range password (``login_password``), set by chpasswd last (``admin_last``: it ends that
    login) and recorded applied once a login with it works."""
    base = {"status": "", "detail": "", "mode": "standalone, airgap", "reconcile": info}
    if (expired := timed_out(info, epoch=epoch)) is not None:
        return expired, set()
    session = session_for(install)
    now = clock or session._clock
    stop = deadline if deadline is not None else now() + 120
    hidden = (install.password, admin_password, login_password)
    try:
        session.wait_ready(stop)
        _result, code = session.run(poll_command(), stop)  # code is the status number
    except Exception as exc:  # noqa: BLE001
        logger.info("security onion reconcile %s: not reachable this round: %s", info.get("node"),
                    _scrub(getattr(exc, "msg", None) or type(exc).__name__, *hidden))
        return None, set()
    if code is not None and code != 0 and code not in RUNNING:
        return {**base, "status": "failed", "detail": STATUS.get(code, f"setup runner status {code}")}, set()
    if code != 0:
        return None, set()
    verified: set[str] = set()
    web, account = info.get("web_user"), info.get("web_account")
    if web and account and not info.get("web_generated") and _check(
            session, as_root("so-web-check", web_check_script(str(web)), install), stop, hidden):
        verified.add(str(account))
    notes: list[str] = []
    admin = info.get("admin_user")
    # A record written before ADR 0013 names the login as its admin_user (admin_last).
    login = info.get("login_user") or (admin if info.get("admin_last") else None)
    if login_password is None and login is not None and login == admin:
        login_password = admin_password  # such a record's admin password is the login's
    if admin and admin != login and admin_password is not None:
        if _login_works(session_for, str(admin), admin_password, stop):
            verified.add(str(admin))
        else:
            notes.append(f"admin account {admin}: a login with its declared password did not work")
    if info.get("admin_last") and login and login_password is not None:
        # The build login: give it its range password now, last (ADR 0013 §3).
        if change_login_password(session, session_for, install, str(login), login_password, stop, hidden):
            verified.add(str(login))
        else:
            notes.append(f"admin account {login}: its declared password was not set")
    detail = STATUS[0] + (f"; {'; '.join(notes)}" if notes else "")
    return {**base, "status": "ok", "detail": _scrub(detail, *hidden)}, verified


def timed_out(info: dict, *, epoch: Callable[[], float] | None = None) -> dict | None:
    """The ``timeout`` outcome of a ``running`` setup past VSPHERE_SO_SETUP_TIMEOUT from its
    start, else None. No guest operation: works for a powered-off or unreachable sensor."""
    at = epoch or _time.time
    if at() - float(info.get("since") or 0) <= float(info.get("timeout") or VSPHERE_SO_SETUP_TIMEOUT):
        return None
    return {"status": "timeout", "mode": "standalone, airgap", "reconcile": info,
            "detail": f"not healthy within VSPHERE_SO_SETUP_TIMEOUT; see {STATUS_FILE} and {RUNNER_LOG} in the guest"}


def cleanup(session, install: guest.GuestCredentials, deadline: float) -> bool:
    """Best effort, bounded, for a terminal reconcile (timeout): remove the answer file (it
    holds the SOC web password) if setup never got as far as removing it. Never raises."""
    try:
        session.wait_ready(deadline)
        result, _ = session.run(as_root("so-cleanup", f"rm -f {ANSWERS_FILE}", install),
                                max(deadline, session._clock() + 60))
    except Exception:  # noqa: BLE001 -- unreachable: nothing to clean from here
        return False
    return result == "ok"


def _login_works(session_for, username: str, password: guest.Secret, stop: float) -> bool:
    """Whether a guest-operations login as ``username`` with ``password`` is accepted."""
    try:
        session_for(guest.GuestCredentials("linux", username, password)).wait_ready(stop)
    except Exception:  # noqa: BLE001 -- not verified: recorded as not set
        return False
    return True
