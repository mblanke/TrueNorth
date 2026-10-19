"""The golden templates' generic build login, shown to staff for machines with no declared
account (ADR 0012 §5; owner decision 2026-10-19).

A machine whose range template declares no account keeps the login its golden template was
built with: the Packer build credentials (``infra/vsphere/packer``: ``windows_admin_password``,
``ssh_username``/``ssh_password``). The installer renders the same values to the API
(``TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD``, ``TN_TEMPLATE_LINUX_USER``,
``TN_TEMPLATE_LINUX_PASSWORD``); they are read here, per call, and never logged.

Only holders of ``range:credentials_read`` see these rows (``service.visible_accounts``):
never Students, never observers. Appliances (pfSense, OPNsense, VyOS, routers) and Security
Onion get none: their logins are their own config, with no per-appliance setting yet.

Windows caveat: at deploy, Sysprep sets the Administrator password of a Windows VM with no
declared Administrator to ``TN_RANGE_DEFAULT_PASSWORD`` (when set) or a random one, so the
template's build password is the live login only where those are the same.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from . import accounts as account_rules

WINDOWS_PASSWORD_ENV = "TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD"
LINUX_USER_ENV = "TN_TEMPLATE_LINUX_USER"
LINUX_PASSWORD_ENV = "TN_TEMPLATE_LINUX_PASSWORD"
DEFAULT_LINUX_USER = "ubuntu"
NOT_CONFIGURED = "template default not configured"
WINDOWS_NOTE = ("Template build login. Sysprep sets this machine's Administrator password at deploy: it is "
                "this one only if TN_RANGE_DEFAULT_PASSWORD is set to it")
LINUX_NOTE = "Template build login (no account declared for this machine)"
# Token names in the generated wiki page (wiki_doc.TOKEN_RE).
TOKEN_WINDOWS = "template-windows"
TOKEN_LINUX = "template-linux"

# vsphere_infra.os_family (mirrored; tests/contracts/test_account_os_matches_worker.py).
APPLIANCE_PREFIXES = ("pfsense", "opnsense", "vyos")
APPLIANCE_ROLES = frozenset({"firewall", "router", "gateway"})
NO_TEMPLATE_LOGIN = ("securityonion",)  # its login comes from so-setup at deploy (ADR 0012 §8)


@dataclass(frozen=True)
class TemplateLogin:
    machine: str
    username: str
    password: str | None  # None: not configured
    token: str  # TOKEN_WINDOWS | TOKEN_LINUX
    notes: str


def family(os_name: str, role: str) -> str:
    """``windows``, ``appliance`` or ``linux``, as the worker's os_family decides."""
    os_name = (os_name or "").lower()
    if "win" in os_name:
        return "windows"
    if os_name.startswith(APPLIANCE_PREFIXES) or (role or "").lower() in APPLIANCE_ROLES:
        return "appliance"
    return "linux"


def machine_families(doc: dict[str, Any]) -> dict[str, str]:
    """Machine id -> family, for every machine the worker builds from ``doc``."""
    roles = {str(n.get("id")): str(n.get("role") or "") for n in account_rules.built_from(doc)}
    return {m: family(os_name, roles.get(m, "")) for m, os_name in account_rules.machine_os(doc).items()}


def _env(name: str) -> str | None:
    return os.getenv(name, "").strip() or None


def password_for(token: str) -> str | None:
    """The configured password a wiki token names, or None."""
    return {TOKEN_WINDOWS: _env(WINDOWS_PASSWORD_ENV), TOKEN_LINUX: _env(LINUX_PASSWORD_ENV)}.get(token)


def logins(doc: dict[str, Any], declared_machines: set[str]) -> list[TemplateLogin]:
    """A template login for each Windows or Linux machine of ``doc`` with no declared account."""
    os_of = account_rules.machine_os(doc)
    out = []
    for machine, fam in sorted(machine_families(doc).items()):
        if machine in declared_machines or fam == "appliance" or os_of.get(machine, "").startswith(NO_TEMPLATE_LOGIN):
            continue
        if fam == "windows":
            pw = _env(WINDOWS_PASSWORD_ENV)
            out.append(TemplateLogin(machine, "Administrator", pw, TOKEN_WINDOWS,
                                     WINDOWS_NOTE if pw else NOT_CONFIGURED))
        else:
            pw = _env(LINUX_PASSWORD_ENV)
            out.append(TemplateLogin(machine, _env(LINUX_USER_ENV) or DEFAULT_LINUX_USER, pw, TOKEN_LINUX,
                                     LINUX_NOTE if pw else NOT_CONFIGURED))
    return out
