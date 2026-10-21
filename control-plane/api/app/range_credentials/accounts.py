"""A range template's ``accounts:`` block: parse, validate, generate, redact (ADR 0012 §12).

::

    accounts:
      - machine: dc01            # a node id or name (nodes:), or an asset role (assets:)
        username: Administrator
        generate: true           # or password: "..." (exactly one of the two)
        student_visible: false   # handed to Students taking part in the range
        role: admin              # admin | user (default user)
        notes: Domain admin      # optional
      - machine: ws01
        username: jsmith
        password: "Summer2024!"  # a typed-in password: only with scenario: true
        scenario: true           # the weak password is the lesson; never rotated
        student_visible: false

The JSON Schema (scenario-engine/schemas/template.schema.json) checks the shape; this
module adds what a schema cannot: the machine exists in the template, a username appears
once per machine (case-insensitively: Windows account names are), and a typed-in password
for a Windows machine meets Windows complexity.

ADR 0013 §3 (owner decision 2026-10-21): every password is generated per range, except a
**scenario** account's (``scenario: true``), whose typed-in password is part of the
exercise and is kept as authored (not rotated; the row says so). A scenario account is
never a staff login: not ``role: admin``, not the built-in Administrator, the Linux build
user or Security Onion's ``soadmin`` (``reserved_logins``). ``implicit_staff_logins`` adds a
generated row for each of those a template leaves undeclared, so no machine keeps its
golden template's build password.

A typed-in password is never stored as typed: ``seal_yaml`` replaces it in the stored
template with a range-credential seal (``seal.py``, ``tnsec:rc1:...``, a key derived for
this purpose only) on create and update; ``parse`` unseals it where it is used (the
credentials sync) and refuses any other sealed value (a hypervisor, AI or LTI secret pasted
in). ``redact_yaml`` masks passwords for readers who may not see them, from the parsed
document (decoded keys), never by searching the text.

Which machines are Windows is decided as the worker decides it (``machine_os`` mirrors
worker/render.py and vsphere_infra.os_family; tests/contracts/test_account_os_matches_worker.py).
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import string
from dataclasses import dataclass
from typing import Any

import yaml

from .. import safe_yaml
from . import seal as rc_seal

ROLES = ("admin", "user")
FIELDS = frozenset({"machine", "username", "password", "generate", "student_visible", "role", "notes", "scenario"})
MAX_ACCOUNTS = 500
MAX_PASSWORD = 128
MAX_NOTES = 500
# Letters, digits and . _ @ - ; a backslash for DOMAIN\user. No spaces, quotes or colons.
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@\\-]{0,103}$")
REDACTED = "********"

# Generated passwords: Windows complexity (three of four classes; we use all four) and
# symbols that need no quoting in a shell, YAML, Sysprep XML or cloud-config.
_SYMBOLS = "-_.!#%+="
_ALPHABET = string.ascii_letters + string.digits + _SYMBOLS
GENERATED_LENGTH = 20


@dataclass(frozen=True)
class AccountSpec:
    machine: str  # the machine's canonical id (the node id, or the asset role)
    username: str
    password: str | None  # None when generated
    generate: bool
    student_visible: bool
    role: str
    notes: str
    scenario: bool = False  # a typed-in password authored on purpose (never rotated)
    source: str = "declared"  # declared | implicit (models.DECLARED / IMPLICIT)

    @property
    def key(self) -> tuple[str, str]:
        return self.machine, self.username.casefold()


DEFAULT_OS = "ubuntu-2404"  # worker/render.py DEFAULT_OS


def built_from(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """What the worker builds machines from (render._extract_nodes): ``nodes`` when there
    are any, else one machine per ``assets`` entry (id = its role)."""
    nodes = doc.get("nodes")
    if nodes:
        return [n for n in nodes if isinstance(n, dict)]
    return [{"id": a.get("role", "vm"), "os": a.get("os", DEFAULT_OS)}
            for a in doc.get("assets") or [] if isinstance(a, dict)]


def machine_os(doc: dict[str, Any]) -> dict[str, str]:
    """Machine id -> the OS the worker builds it with (lower case): render._node_os, i.e.
    ``os``, else ``os_template``, else ``type``, else the default."""
    return {
        str(n["id"]): str(n.get("os") or n.get("os_template") or n.get("type") or DEFAULT_OS).lower()
        for n in built_from(doc) if n.get("id") not in (None, "")
    }


def is_windows(os_name: str) -> bool:
    """As vsphere_infra.os_family decides: an OS naming Windows."""
    return "win" in (os_name or "").lower()


def windows_password_problem(password: str, username: str) -> str | None:
    """Why Windows would refuse ``password`` for ``username``, or None: at least 8 characters,
    three of upper case, lower case, digits and other characters, and not containing the
    account name (Windows checks names of 3 or more characters, case-insensitively)."""
    if len(password) < 8:
        return "Windows requires at least 8 characters"
    classes = sum((any(c.isupper() for c in password), any(c.islower() for c in password),
                   any(c.isdigit() for c in password), any(not c.isalnum() for c in password)))
    if classes < 3:
        return "Windows requires three of: upper case, lower case, digits, other characters"
    name = username.rsplit("\\", 1)[-1].split("@", 1)[0]
    if len(name) >= 3 and name.casefold() in password.casefold():
        return "Windows refuses a password that contains the account name"
    return None


def machines(doc: dict[str, Any]) -> dict[str, str]:
    """Every name a template's machines can be referred to by -> the machine's id.

    Only machines the worker builds (``built_from``): a node by its ``id`` or its
    ``name``, or, in a template without nodes, an asset by its ``role``. An id wins over
    another node's name of the same spelling."""
    out: dict[str, str] = {}
    by_id: dict[str, str] = {}
    for node in built_from(doc):
        if node.get("id") not in (None, ""):
            nid = str(node["id"])
            by_id[nid] = nid
            if node.get("name"):
                out.setdefault(str(node["name"]), nid)
    out.update(by_id)
    return out


ADMIN = "administrator"
LINUX_USER_ENV = "TN_TEMPLATE_LINUX_USER"  # the golden Linux templates' build user (a name, never a password)
SO_USER_ENV = "VSPHERE_SO_INSTALL_USER"  # Security Onion's build login (a name)
DEFAULT_LINUX_USER = "ubuntu"  # infra/vsphere/packer ssh_username's default
DEFAULT_SO_USER = "soadmin"

# vsphere_infra.os_family and vsphere_securityonion.is_securityonion, mirrored (the API may not
# import the worker; tests/contracts/test_account_os_matches_worker.py keeps them equal).
APPLIANCE_PREFIXES = ("pfsense", "opnsense", "vyos")
APPLIANCE_ROLES = frozenset({"firewall", "router", "gateway"})
SECURITY_ONION = "securityonion"


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
    roles = {str(n.get("id")): str(n.get("role") or "") for n in built_from(doc)}
    return {m: family(os_name, roles.get(m, "")) for m, os_name in machine_os(doc).items()}


def is_securityonion(os_name: str) -> bool:
    return (os_name or "").strip().lower() == SECURITY_ONION


def linux_build_user() -> str:
    return os.getenv(LINUX_USER_ENV, "").strip() or DEFAULT_LINUX_USER


def so_build_user() -> str:
    return os.getenv(SO_USER_ENV, "").strip() or DEFAULT_SO_USER


def reserved_logins() -> frozenset[str]:
    """Account names that are always staff logins (case-folded): never a scenario account."""
    return frozenset({ADMIN, linux_build_user().casefold(), so_build_user().casefold()})


IMPLICIT_NOTES = {
    "windows": "Built-in Administrator, generated per range (replaces the template password at deploy)",
    "linux": "Build user of the golden template, generated per range (replaces the template password at deploy)",
    SECURITY_ONION: "Security Onion OS login, generated per range (set when its setup finishes)",
}


def implicit_staff_logins(doc: dict[str, Any] | None, declared: list[AccountSpec], *,
                          student_visible: bool = False) -> list[AccountSpec]:
    """A generated row for each built-in login a machine's golden template carries that the
    template does not declare (ADR 0013 §3): the built-in Administrator of a Windows machine,
    the Linux build user (``TN_TEMPLATE_LINUX_USER``) of a Linux one, ``soadmin``
    (``VSPHERE_SO_INSTALL_USER``) of a Security Onion node. The worker sets each at deploy
    (Sysprep, cloud-init, the Security Onion setup), so the template password stops working.

    It replaces ADR 0012's product-only ``implicit_admins``: every Windows machine now gets
    its own Administrator, the product VMs and domain controllers included.

    ``role: admin``, staff-only, except for a lab session's range (``student_visible``),
    which is the Student's own (owner decision 2026-10-21). Appliances get none: their
    logins are their own configuration (a follow-up issue)."""
    if not isinstance(doc, dict):
        return []
    have = {spec.key for spec in declared}
    os_of = machine_os(doc)
    out: list[AccountSpec] = []
    for machine, fam in machine_families(doc).items():
        if fam == "appliance":
            continue
        if is_securityonion(os_of.get(machine, "")):
            username, kind = so_build_user(), SECURITY_ONION
        elif fam == "windows":
            username, kind = "Administrator", "windows"
        else:
            username, kind = linux_build_user(), "linux"
        if (machine, username.casefold()) in have:
            continue
        out.append(AccountSpec(machine=machine, username=username, password=None, generate=True,
                               student_visible=student_visible, role="admin", notes=IMPLICIT_NOTES[kind],
                               source="implicit"))
    return out


def parse(doc: dict[str, Any] | None) -> tuple[list[AccountSpec], list[dict[str, str]]]:
    """The accounts a template declares, and what is wrong with them (``{path, message}``).

    Only a document with no errors should be acted on; callers refuse it (422) otherwise."""
    if not isinstance(doc, dict) or doc.get("accounts") is None:
        return [], []
    raw = doc["accounts"]
    if not isinstance(raw, list):
        return [], [{"path": "accounts", "message": "accounts must be a list"}]
    if len(raw) > MAX_ACCOUNTS:
        return [], [{"path": "accounts", "message": f"at most {MAX_ACCOUNTS} accounts"}]
    known = machines(doc)
    os_of = machine_os(doc)
    specs: list[AccountSpec] = []
    errors: list[dict[str, str]] = []
    seen: dict[tuple[str, str], int] = {}
    for i, item in enumerate(raw):
        path = f"accounts.{i}"

        def bad(msg: str, field: str = "", _path: str = path) -> None:
            errors.append({"path": f"{_path}.{field}" if field else _path, "message": msg})

        if not isinstance(item, dict):
            bad("each account must be a mapping")
            continue
        if extra := sorted(set(map(str, item)) - FIELDS):
            bad(f"unknown field(s): {', '.join(extra)}")
        machine = item.get("machine")
        machine_id = known.get(str(machine)) if isinstance(machine, (str, int)) else None
        if not isinstance(machine, (str, int)) or str(machine).strip() == "":
            bad("machine is required", "machine")
        elif machine_id is None:
            bad(f"machine {machine!r} is not a node or asset of this template", "machine")
        username = item.get("username")
        if not isinstance(username, str) or not USERNAME_RE.match(username):
            bad("username is required: letters, digits and . _ @ - \\ (at most 104)", "username")
        password = item.get("password")
        if rc_seal.looks_sealed(password):
            try:
                password = rc_seal.unseal(password)
            except rc_seal.NotARangeSealError:
                bad("password is a sealed value (tnsec:) that is not a range-account seal this platform made, "
                    "so it cannot unseal it; type the password or use generate: true", "password")
                password = ""
        generate = item.get("generate", False)
        if not isinstance(generate, bool):
            bad("generate must be true or false", "generate")
            generate = False
        if password is not None and (not isinstance(password, str) or not password):
            bad("password must be a non-empty string", "password")
        elif isinstance(password, str) and len(password) > MAX_PASSWORD:
            bad(f"password is longer than {MAX_PASSWORD} characters", "password")
        elif password == REDACTED:
            bad("password is the placeholder a redacted copy shows; set the real password or generate: true",
                "password")
        elif (isinstance(password, str) and isinstance(username, str) and machine_id is not None
              and is_windows(os_of.get(machine_id, ""))
              and (why := windows_password_problem(password, username))):
            bad(f"password for a Windows machine: {why}", "password")
        if (password is not None) == bool(generate):
            bad("set exactly one of password and generate: true")
        visible = item.get("student_visible")
        if not isinstance(visible, bool):
            bad("student_visible is required: true or false", "student_visible")
        role = item.get("role", "user")
        if role not in ROLES:
            bad(f"role must be one of {', '.join(ROLES)}", "role")
        scenario = item.get("scenario", False)
        if not isinstance(scenario, bool):
            bad("scenario must be true or false", "scenario")
            scenario = False
        if scenario and password is None:
            bad("scenario: true is for a typed-in password the exercise needs; set password (or drop scenario "
                "and use generate: true)", "scenario")
        elif password is not None and not scenario:
            bad("a typed-in password is allowed only on a scenario account (scenario: true), whose weak password "
                "is part of the exercise; use generate: true (ADR 0013)", "password")
        if scenario and (role == "admin" or (isinstance(username, str)
                                             and username.casefold() in reserved_logins())):
            bad("a staff login (role: admin, Administrator, the Linux build user or soadmin) cannot be a scenario "
                "account: its password is always generated (ADR 0013)", "scenario")
        notes = item.get("notes", "")
        if notes is None:
            notes = ""
        if not isinstance(notes, str) or len(notes) > MAX_NOTES:
            bad(f"notes must be text of at most {MAX_NOTES} characters", "notes")
        if machine_id is None or not isinstance(username, str) or not USERNAME_RE.match(username):
            continue
        key = (machine_id, username.casefold())
        if key in seen:
            bad(f"username {username!r} is declared twice for machine {machine_id!r} (also accounts.{seen[key]})",
                "username")
            continue
        seen[key] = i
        specs.append(AccountSpec(
            machine=machine_id, username=username, password=password if isinstance(password, str) else None,
            generate=bool(generate), student_visible=visible is True, role=role if role in ROLES else "user",
            notes=notes if isinstance(notes, str) else "", scenario=scenario is True,
        ))
    return (specs if not errors else []), errors


def parse_yaml(text: str | None) -> tuple[list[AccountSpec], list[dict[str, str]]]:
    """``parse`` on template YAML. Unparseable YAML declares nothing (it is refused elsewhere)."""
    try:
        doc = safe_yaml.load(text or "")
    except yaml.YAMLError:  # safe_yaml refusals included
        return [], []
    return parse(doc if isinstance(doc, dict) else None)


def generate_password(username: str = "", length: int = GENERATED_LENGTH) -> str:
    """A random password with an upper, a lower, a digit and a symbol, starting with a letter
    or digit, that Windows accepts for ``username`` (``windows_password_problem``)."""
    while True:
        pw = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if (pw[0].isalnum() and any(c.isupper() for c in pw) and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in _SYMBOLS for c in pw)
                and windows_password_problem(pw, username) is None):
            return pw


def _load(text: str | None) -> Any:
    return safe_yaml.load(text or "")


def seal_yaml(text: str | None) -> str | None:
    """The template to store: every typed-in ``accounts[].password`` sealed (``tnsec:rc1:``).

    Unchanged (formatting and comments kept) when there is nothing to seal. Call after
    ``parse`` accepted the document: a sealed value that does not unseal is refused there."""
    try:
        doc = _load(text)
    except yaml.YAMLError:  # refused elsewhere; nothing is acted on
        return text
    if not isinstance(doc, dict) or not isinstance(doc.get("accounts"), list):
        return text
    changed = False
    for item in doc["accounts"]:
        pw = item.get("password") if isinstance(item, dict) else None
        if isinstance(pw, str) and pw and not rc_seal.looks_sealed(pw):
            item["password"] = rc_seal.seal(pw)
            changed = True
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True) if changed else text


def redact_yaml(text: str | None) -> str:
    """Template YAML for a reader who may not see passwords: every ``accounts[].password``
    (sealed or not) is ``REDACTED``, and so is any of the template's passwords wherever else
    it appears in the document (another account's notes, a description, a key).

    Decided on the parsed document, so an escaped key (``"passwor\\x64"``) is still a
    password. A document that does not parse (or YAML the API refuses, such as aliases) is
    withheld entirely ("")."""
    try:
        doc = _load(text)
    except yaml.YAMLError:
        return ""
    if not isinstance(doc, dict) or not isinstance(doc.get("accounts"), list):
        return text or ""
    plains: set[str] = set()
    for item in doc["accounts"]:
        if not isinstance(item, dict) or item.get("password") is None:
            continue
        pw = item["password"]
        if isinstance(pw, str) and pw:
            plains.add(pw)  # the stored token, or a password typed in before sealing
            with contextlib.suppress(Exception):  # not ours, or no key: masked as stored
                plains.add(rc_seal.unseal(pw))
        item["password"] = REDACTED
    # Longest first, so a password containing another is masked whole.
    return yaml.safe_dump(_mask(doc, sorted(plains, key=len, reverse=True)), sort_keys=False, allow_unicode=True)


def _mask(value: Any, plains: list[str]) -> Any:
    """``value`` with every one of ``plains`` replaced by ``REDACTED`` in every string, keys too."""
    if isinstance(value, str):
        for p in plains:
            value = value.replace(p, REDACTED)
        return value
    if isinstance(value, dict):
        return {_mask(k, plains): _mask(v, plains) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, plains) for v in value]
    return value
