"""TrueNorth Range - each router appliance's admin login, per range (issue #248, ADR 0013 §3).

pfSense, OPNsense and VyOS templates are built with the generic build password (ADR 0012
decision 5: pfSense ``admin``, OPNsense ``root``, VyOS ``vyos``). The API gives every such
machine a generated, sealed, staff-only admin row (``accounts.implicit_staff_logins``); at
deploy the worker turns that row's password into a **hash** and hands only the hash to the
appliance, through the boot-time config path it already has:

=========  ==============================================  ===================================
product    delivered                                        applied by
=========  ==============================================  ===================================
pfSense    ``guestinfo.tn.pfsense.admin``: bcrypt (``$2y$``) ``tn-pfsense-config boot`` sets the
           for the admin user's ``bcrypt-hash``              admin user's hash, reboots once,
                                                             then acknowledges and clears the key
OPNsense   ``guestinfo.tn.opnsense.admin``: bcrypt, for     the same script (``tn-opnsense-config``)
           the root user's ``password``
VyOS       ``set system login user vyos authentication      cloud-init (``vyos_config_commands``);
           encrypted-password '$6$...'`` in the user data   ``tn-admin-ack.service`` acknowledges
=========  ==============================================  ===================================

The plain password never leaves the worker's memory: not guestinfo, not the result, not a
log line. The acknowledgement is ``guestinfo.tn.<product>.admin_ack``: the first 16 hex
digits of SHA-256 of the hash the appliance has in force (``fingerprint``), or
``failed <why>``. The worker removes the hash from the VM's configuration once it has
seen the matching acknowledgement (``scrub_values``).

**The template-login check** of an appliance (``check``; VMware guest operations do not work
on these images) records ``template_login = {status, username, method, detail}``:

================  =======================================================================
``ssh``           the worker reached the appliance's management address on port 22 and
                  made one SSH login attempt with the template password (after the range's
                  own login worked): refused = ``rejected``, accepted = ``accepted``.
``guestinfo_ack`` no SSH (the range's own firewall policy blocks it from the uplink by
                  design, or the worker has no SSH client): the appliance's boot hook
                  reported that the admin hash in force is the range's = ``rejected``.
                  Self-reported, so the record names the method.
``none``          neither: ``inconclusive``, never a pass.
================  =======================================================================
"""

from __future__ import annotations

import hashlib
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

try:
    import bcrypt
except ImportError:  # the worker image installs it; without it pfSense/OPNsense get no admin hash
    bcrypt = None  # type: ignore[assignment]

from . import pfsense_config, vyos_config
from .provisioners.sha512_crypt import sha512_crypt
from .provisioners.vsphere_guest import Secret

ADMIN_USERS = {"pfsense": "admin", "opnsense": "root", "vyos": "vyos"}
HASH_FORMATS = {"pfsense": "bcrypt", "opnsense": "bcrypt", "vyos": "sha512-crypt"}
BCRYPT_COST = 10  # pfSense's and OPNsense's own password_hash() default
TEMPLATE_PASSWORD_ENV = "TN_TEMPLATE_APPLIANCE_PASSWORD"  # else the Linux one (ADR 0012 decision 5)
LINUX_PASSWORD_ENV = "TN_TEMPLATE_LINUX_PASSWORD"
SSH_PORT = 22

REJECTED, ACCEPTED, INCONCLUSIVE = "rejected", "accepted", "inconclusive"
SSH, ACK, NONE = "ssh", "guestinfo_ack", "none"


class ApplianceAdminError(ValueError):
    """The admin hash cannot be made (no bcrypt library in the worker)."""


@dataclass(frozen=True)
class AdminLogin:
    """What one appliance VM gets: its admin user, the hash (never the password) and its fingerprint.

    ``password`` stays in memory for the SSH check only and is dropped with the build's other
    secrets (vsphere_api's ``finally``)."""

    product: str
    username: str  # the appliance's admin user (ADMIN_USERS)
    hash: Secret = field(repr=False)
    password: Secret = field(repr=False)
    account: str = ""  # the range account's name as stored (case may differ); reported as applied

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.hash.reveal())

    @property
    def hash_format(self) -> str:
        return HASH_FORMATS[self.product]

    def summary(self) -> dict:
        """What the VM's output records: no hash, no password."""
        return {"username": self.username, "hash_format": self.hash_format, "delivery": delivery(self.product)}


def fingerprint(crypt_hash: str) -> str:
    """The acknowledgement's value for ``crypt_hash`` (not a secret: a digest of a salted hash)."""
    return hashlib.sha256(crypt_hash.encode()).hexdigest()[:16]


def delivery(product: str) -> str:
    return "cloud-init guestinfo (encrypted-password)" if product == "vyos" else f"guestinfo.tn.{product}.admin"


def product_of(vm_def: dict) -> str | None:
    """``pfsense``, ``opnsense``, ``vyos``, or None for a VM that is none of them."""
    return pfsense_config.product_of(vm_def) or ("vyos" if vyos_config.is_vyos(vm_def) else None)


def hash_password(product: str, password: str) -> str:
    """bcrypt (``$2y$``, the prefix PHP's password_hash writes; the same algorithm as
    ``$2b$``) for pfSense and OPNsense, SHA-512-crypt (``$6$``) for VyOS."""
    if product == "vyos":
        return sha512_crypt(password)
    if bcrypt is None:
        raise ApplianceAdminError("the worker has no bcrypt library (pip install bcrypt), so it cannot set "
                                  f"the {product} admin password")
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_COST)).decode()
    return "$2y$" + hashed[4:]


def plan(vm_def: dict) -> AdminLogin | None:
    """The admin login this build gives ``vm_def`` (an appliance carrying its ``_accounts``),
    or None: not a product TrueNorth configures, or no account for its admin user."""
    product = product_of(vm_def)
    if product is None:
        return None
    want = ADMIN_USERS[product]
    for acct in vm_def.get("_accounts") or []:
        if acct.username.casefold() == want and acct.password.reveal():
            pw = acct.password.reveal()
            return AdminLogin(product, want, Secret(hash_password(product, pw)), Secret(pw), acct.username)
    return None


def ack_key(product: str) -> str:
    return f"guestinfo.tn.{product}.admin_ack"


def scrub_values(vm_def: dict, nic_macs: list[str]) -> dict[str, str]:
    """extraConfig that takes the hash out of a VM whose appliance acknowledged it: an empty
    ``guestinfo.tn.<product>.admin`` (vSphere removes a key set to ""), or VyOS's user data
    rendered again without the encrypted-password line. The VyOS instance id does not change,
    so cloud-init does not run the configuration again."""
    if vm_def.get("_vyos") is not None:
        values = vyos_config.guestinfo(vm_def["_vyos"], nic_macs, vm_def["name"])
        return {"guestinfo.userdata": values["guestinfo.userdata"]}
    if (cfg := vm_def.get("_pfsense")) is not None:
        return {pfsense_config.admin_key(cfg.product): ""}
    return {}


# --------------------------------------------------------------------------- #
# The template-login check
# --------------------------------------------------------------------------- #


def template_password(env: dict | None = None) -> str | None:
    env = os.environ if env is None else env
    return str(env.get(TEMPLATE_PASSWORD_ENV) or env.get(LINUX_PASSWORD_ENV) or "") or None


def reachable(host: str, port: int = SSH_PORT, timeout: float = 5.0) -> bool:
    """Whether a TCP connection to ``host:port`` opens (no data is sent)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def paramiko_login(host: str, username: str, password: Secret, timeout: float = 10.0) -> bool | None:
    """One SSH password login: True accepted, False refused, None no answer or no SSH client
    (the worker image does not ship paramiko; the acknowledgement is then the method)."""
    try:
        import paramiko  # type: ignore[import-not-found]
    except ImportError:
        return None
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # a fresh range appliance: no known key
    try:
        client.connect(host, port=SSH_PORT, username=username, password=password.reveal(), timeout=timeout,
                       auth_timeout=timeout, banner_timeout=timeout, look_for_keys=False, allow_agent=False)
        return True
    except paramiko.AuthenticationException:
        return False
    except Exception:  # noqa: BLE001 — no answer is not a refusal
        return None
    finally:
        client.close()


SshLogin = Callable[[str, str, Secret], "bool | None"]


def _ssh(admin: AdminLogin, host: str, template_pw: str, ssh_login: SshLogin) -> dict | None:
    """The SSH method's result, or None when it could not decide (fall back to the ack)."""
    own = ssh_login(host, admin.username, admin.password)
    if own is not True:
        return None  # the range's own login not proven over SSH: a refusal would prove nothing
    answer = ssh_login(host, admin.username, Secret(template_pw))
    if answer is True:
        return {"status": ACCEPTED, "method": SSH, "detail": f"the golden template's password still logs in over "
                                                              f"SSH at {host}"}
    if answer is False:
        return {"status": REJECTED, "method": SSH, "detail": f"the golden template's password is refused over SSH "
                                                              f"at {host}, after the range's own login worked"}
    return None


def classify_ack(value: str, admin: AdminLogin) -> dict | None:
    """The ack method's result for ``value``, or None while there is no decisive answer yet."""
    value = (value or "").strip()
    if not value:
        return None
    if value.startswith("failed"):
        why = value[len("failed"):].strip()[:200] or "no reason given"
        return {"status": INCONCLUSIVE, "method": ACK, "detail": f"the appliance's boot hook could not set the "
                                                                  f"range's admin password: {why}"}
    if value == admin.fingerprint:
        return {"status": REJECTED, "method": ACK, "detail": "the appliance's boot hook reports the range's own "
                "admin password hash in force (the template password is replaced; no login attempt was made)"}
    return None  # another hash (the template's, before cloud-init or the reboot): keep waiting


def check(admin: AdminLogin | None, *, mgmt_ip: str = "", read_ack: Callable[[], str],
          deadline: float, poll: float = 10.0, env: dict | None = None,
          ssh_login: SshLogin | None = None, is_reachable: Callable[[str], bool] | None = None,
          clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> dict:
    """The template-login check of one appliance (module docstring). Never raises; the
    result never holds a password or a hash. ``read_ack``: the VM's current
    ``guestinfo.tn.<product>.admin_ack`` ("" when unset). ``ssh_login`` / ``is_reachable``:
    this module's ``paramiko_login`` / ``reachable`` unless given."""
    ssh_login = ssh_login or paramiko_login
    is_reachable = is_reachable or reachable
    if admin is None:
        return {"status": INCONCLUSIVE, "method": NONE, "detail": "no range admin login was delivered to this "
                "appliance (an image TrueNorth does not configure, or no generated admin row for it)"}
    out = {"username": admin.username}
    notes: list[str] = []
    template_pw = template_password(env)
    if mgmt_ip and template_pw:
        try:
            if is_reachable(mgmt_ip):
                if (res := _ssh(admin, mgmt_ip, template_pw, ssh_login)) is not None:
                    return {**out, **res}
                notes.append(f"SSH at {mgmt_ip} answered no login decisively (or the worker has no SSH client)")
            else:
                notes.append(f"SSH at {mgmt_ip} is not reachable from the worker")
        except Exception as exc:  # noqa: BLE001 — fall back to the acknowledgement
            notes.append(f"SSH check failed ({type(exc).__name__})")
    elif not mgmt_ip:
        notes.append("no management address the worker can reach")
    else:
        notes.append("the worker does not know the template password (installer setting), so it cannot try SSH")
    last = ""
    while True:
        try:
            last = read_ack() or ""
        except Exception as exc:  # noqa: BLE001 — never a pass
            notes.append(f"reading the acknowledgement failed ({type(exc).__name__})")
            break
        if (res := classify_ack(last, admin)) is not None:
            return {**out, **res, "detail": "; ".join([res["detail"], *notes])}
        if clock() >= deadline:
            break
        sleep(min(poll, max(0.0, deadline - clock())))
    seen = "a different admin password hash" if last else "nothing"
    return {**out, "status": INCONCLUSIVE, "method": NONE,
            "detail": "; ".join([*notes, f"the appliance acknowledged {seen} before the deadline"])}

