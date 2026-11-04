"""Human approval and blackout gates.

Approvals live in .claude/approvals/<RANGE_ID>.json (git-ignored). Only a human creates
them, with `tools/range approve`, run in their own terminal. The Claude Code guard hook
blocks Claude from running `approve` or writing to .claude/approvals/.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

from .common import RANGE_ID_RE, RangeError, load_json, now_utc, write_json

ACTIONS = {"build", "reset", "teardown", "promote", "release"}
PLAN_BOUND = {"build", "reset", "teardown"}   # approval is tied to the exact compiled plan
APPROVAL_DIR = Path(".claude") / "approvals"
MAX_HOURS = 24
# Optional signing key. When this file exists, every approval must carry a valid HMAC.
# Make it readable by the humans who approve (and the CI account), NOT by the account
# Claude Code runs as. That turns approvals into a real OS-level boundary.
KEY_FILE = Path(os.environ.get("TNRANGE_APPROVAL_KEY", "/etc/truenorth/approval.key"))
CODE_DIRS = ("playbooks", "tools")


def code_sha256(root: Path) -> str:
    """Hash of the code an approval covers (playbooks and compiler)."""
    h = hashlib.sha256()
    for d in CODE_DIRS:
        for f in sorted((root / d).rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and not f.name.endswith((".pyc", ".tmp")):
                h.update(str(f.relative_to(root)).encode())
                h.update(f.read_bytes())
    return h.hexdigest()


def _signing_key() -> bytes | None:
    try:
        return KEY_FILE.read_bytes().strip() or None
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        return None


def signing_required(root: Path) -> bool:
    """Signed approvals are required if config/security.json says so (a human-only file),
    or if the key file exists at all, even when this account cannot see it."""
    try:
        if json.loads((root / "config" / "security.json").read_text()).get("require_signed_approvals"):
            return True
    except (OSError, ValueError):
        pass
    try:
        os.stat(KEY_FILE)
        return True
    except PermissionError:
        return True          # exists behind a directory we cannot read
    except (FileNotFoundError, NotADirectoryError):
        return False


def _sig(key: bytes, rec: dict) -> str:
    body = json.dumps({k: v for k, v in rec.items() if k != "hmac"}, sort_keys=True).encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def _confirm_human(rid: str, actions: list[str]) -> None:
    """Require an interactive terminal and the range ID typed back."""
    if os.environ.get("TNRANGE_CI_APPROVER") == "1" and _signing_key() is not None:
        return  # CI service account: allowed only where it can read the signing key
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RangeError("approve must be run by a person at an interactive terminal")
    try:
        with open("/dev/tty") as tty:
            sys.stdout.write(f"Approve {', '.join(actions)} for {rid}. Type the range ID to confirm: ")
            sys.stdout.flush()
            typed = tty.readline().strip()
    except OSError as exc:
        raise RangeError("approve needs a controlling terminal") from exc
    if typed != rid:
        raise RangeError("confirmation did not match; nothing approved")


def approval_path(root: Path, rid: str) -> Path:
    return root / APPROVAL_DIR / f"{rid}.json"


def approve(root: Path, rid: str, actions: list[str], hours: float, by: str | None) -> Path:
    if not RANGE_ID_RE.match(rid):
        raise RangeError(f"invalid range id {rid!r}")
    bad = [a for a in actions if a not in ACTIONS]
    if bad:
        raise RangeError(f"unknown action(s) {bad}; allowed: {sorted(ACTIONS)}")
    if not 0 < hours <= MAX_HOURS:
        raise RangeError(f"--hours must be between 0 and {MAX_HOURS}")
    _confirm_human(rid, actions)
    now = now_utc()
    rec = {
        "range_id": rid,
        "actions": sorted(set(actions)),
        "approved_by": by or getpass.getuser(),
        "created": now.isoformat(timespec="seconds"),
        "expires": (now + _dt.timedelta(hours=hours)).isoformat(timespec="seconds"),
    }
    if PLAN_BOUND.intersection(actions):
        plan_file = root / "build" / rid / "plan.json"
        if not plan_file.exists():
            raise RangeError(f"compile {rid} first; build/reset/teardown approvals are tied to a specific plan")
        rec["plan_sha256"] = load_json(plan_file)["plan_sha256"]
        rec["code_sha256"] = code_sha256(root)
    key = _signing_key()
    if signing_required(root) and key is None:
        raise RangeError(f"approvals must be signed, and this account cannot read {KEY_FILE}")
    if key is not None:
        rec["hmac"] = _sig(key, rec)
    p = approval_path(root, rid)
    write_json(p, rec)
    return p


def check_approval(root: Path, rid: str, action: str) -> dict:
    p = approval_path(root, rid)
    if not p.exists():
        raise RangeError(f"no approval for {rid}. A human must run, in their own terminal:\n"
                         f"      tools/range approve {rid} --action {action}")
    rec = load_json(p)
    if signing_required(root):
        key = _signing_key()
        if key is None:
            # e.g. the account Claude Code runs as. It may plan and run --check, never a real change.
            raise RangeError(f"approvals are signed and this account cannot read {KEY_FILE}; "
                             f"a real run must be started by an approver account")
        if not hmac.compare_digest(str(rec.get("hmac", "")), _sig(key, rec)):
            raise RangeError(f"approval for {rid} has no valid signature")
    if rec.get("range_id") != rid:
        raise RangeError(f"approval file {p.name} is for {rec.get('range_id')}, not {rid}")
    if action not in rec.get("actions", []):
        raise RangeError(f"approval for {rid} does not cover '{action}' (covers {rec.get('actions')})")
    exp = _dt.datetime.fromisoformat(rec["expires"])
    if now_utc() >= exp:
        raise RangeError(f"approval for {rid} expired at {rec['expires']}")
    if action in PLAN_BOUND:
        plan_file = root / "build" / rid / "plan.json"
        current = load_json(plan_file)["plan_sha256"] if plan_file.exists() else None
        if rec.get("plan_sha256") != current:
            raise RangeError(f"approval for {rid} was given for plan {str(rec.get('plan_sha256'))[:12]}, "
                             f"but the current plan is {str(current)[:12]}. Re-approve after reviewing the new plan")
        if rec.get("code_sha256") != code_sha256(root):
            raise RangeError(f"approval for {rid} was given for different playbook/tool code; "
                             f"re-approve after reviewing the change")
    return rec


def revoke(root: Path, rid: str) -> bool:
    p = approval_path(root, rid)
    if p.exists():
        p.unlink()
        return True
    return False


def active_blackout(root: Path, at: _dt.datetime | None = None) -> dict | None:
    p = root / "config" / "blackout.json"
    if not p.exists():
        return None
    data = load_json(p)
    at = at or now_utc()
    for w in data.get("windows", []) or []:
        try:
            s = _dt.datetime.fromisoformat(w["start"])
            e = _dt.datetime.fromisoformat(w["end"])
        except (KeyError, ValueError) as exc:
            raise RangeError(f"config/blackout.json: bad window {json.dumps(w)}: {exc}") from exc
        if s <= at < e:
            return w
    return None


def check_blackout(root: Path) -> None:
    w = active_blackout(root)
    if w:
        raise RangeError(f"blackout window active until {w['end']}: {w.get('reason', 'no reason given')}")
