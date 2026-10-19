"""A range template's ``accounts:`` block: parse, validate, generate, redact (ADR 0012 §12).

::

    accounts:
      - machine: dc01            # a node id or name (nodes:), or an asset role (assets:)
        username: Administrator
        generate: true           # or password: "..." (exactly one of the two)
        student_visible: false   # handed to Students taking part in the range
        role: admin              # admin | user (default user)
        notes: Domain admin      # optional

The JSON Schema (scenario-engine/schemas/template.schema.json) checks the shape; this
module adds what a schema cannot: the machine exists in the template, and a username
appears once per machine (case-insensitively: Windows account names are).
"""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass
from typing import Any

import yaml

from .. import safe_yaml

ROLES = ("admin", "user")
FIELDS = frozenset({"machine", "username", "password", "generate", "student_visible", "role", "notes"})
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

    @property
    def key(self) -> tuple[str, str]:
        return self.machine, self.username.casefold()


def machines(doc: dict[str, Any]) -> dict[str, str]:
    """Every name a template's machines can be referred to by -> the machine's id.

    A node by its ``id`` or its ``name``; an asset (``assets:``) by its ``role``. An id
    wins over another node's name of the same spelling."""
    out: dict[str, str] = {}
    by_id: dict[str, str] = {}
    for node in doc.get("nodes") or []:
        if isinstance(node, dict) and node.get("id") not in (None, ""):
            nid = str(node["id"])
            by_id[nid] = nid
            if node.get("name"):
                out.setdefault(str(node["name"]), nid)
    for asset in doc.get("assets") or []:
        if isinstance(asset, dict) and asset.get("role"):
            by_id.setdefault(str(asset["role"]), str(asset["role"]))
    out.update(by_id)
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
        if (password is not None) == bool(generate):
            bad("set exactly one of password and generate: true")
        visible = item.get("student_visible")
        if not isinstance(visible, bool):
            bad("student_visible is required: true or false", "student_visible")
        role = item.get("role", "user")
        if role not in ROLES:
            bad(f"role must be one of {', '.join(ROLES)}", "role")
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
            notes=notes if isinstance(notes, str) else "",
        ))
    return (specs if not errors else []), errors


def parse_yaml(text: str | None) -> tuple[list[AccountSpec], list[dict[str, str]]]:
    """``parse`` on template YAML. Unparseable YAML declares nothing (it is refused elsewhere)."""
    try:
        doc = safe_yaml.load(text or "")
    except yaml.YAMLError:  # safe_yaml refusals included
        return [], []
    return parse(doc if isinstance(doc, dict) else None)


def generate_password(length: int = GENERATED_LENGTH) -> str:
    """A random password with an upper, a lower, a digit and a symbol (Windows complexity)."""
    while True:
        pw = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if (pw[0].isalnum() and any(c.isupper() for c in pw) and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in _SYMBOLS for c in pw)):
            return pw


def redact_yaml(text: str | None) -> str | None:
    """Template YAML with every ``accounts[].password`` replaced by ``REDACTED``.

    Unchanged (formatting and comments kept) when there is no password to hide; a document
    that does not parse is returned as it is, since it declares no usable account."""
    if not text or "password" not in text:
        return text
    try:
        doc = safe_yaml.load(text)
    except yaml.YAMLError:  # safe_yaml refusals included
        return text
    if not isinstance(doc, dict) or not isinstance(doc.get("accounts"), list):
        return text
    changed = False
    for item in doc["accounts"]:
        if isinstance(item, dict) and item.get("password") is not None:
            item["password"] = REDACTED
            changed = True
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True) if changed else text
