#!/usr/bin/env python3
"""Write OpenSearch's internal_users.yml from passwords given on stdin (never argv).

stdin: {"path": "<file>", "uid": 1000, "gid": 1000,
        "users": {"<name>": {"password": "...", "backend_roles": [...], "description": "..."}}}

The file is JSON, which is YAML, so the security plugin reads it as is. Idempotent for
any operator: a user's existing bcrypt hash is kept while bcrypt still verifies the
password against it (a fresh hash is salted, so comparing hashes would always differ),
and nothing is written when the document is unchanged ("unchanged"); otherwise the file
is replaced atomically and "changed" is printed. Ownership and mode (uid/gid, 0600) are
enforced either way, and reported as a change when they were wrong.

A file that exists but cannot be read or parsed is an error, never "start from empty":
that turned a permissions problem into a silent re-hash (and a reported change) on every
run. Run it as root (the installer does, with become). Needs python3-bcrypt (tn_base).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import bcrypt


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read {path}: {exc} (run as root; fix or remove the file)")


def _fix_owner(path: str, uid: int | None, gid: int | None) -> bool:
    st = os.stat(path)
    changed = False
    if uid is not None and gid is not None and (st.st_uid, st.st_gid) != (uid, gid):
        os.chown(path, uid, gid)
        changed = True
    if st.st_mode & 0o777 != 0o600:
        os.chmod(path, 0o600)
        changed = True
    return changed


def main() -> int:
    req = json.load(sys.stdin)
    path = req["path"]
    uid, gid = req.get("uid"), req.get("gid")
    old = _load(path)
    doc: dict = {"_meta": {"type": "internalusers", "config_version": 2}}
    for name, spec in req["users"].items():
        password = spec["password"].encode()
        if not password:
            sys.exit(f"empty password for {name}")
        prev = (old.get(name) or {}).get("hash", "")
        try:
            keep = bool(prev) and bcrypt.checkpw(password, prev.encode())
        except ValueError:  # not a bcrypt hash
            keep = False
        doc[name] = {
            "hash": prev if keep else bcrypt.hashpw(password, bcrypt.gensalt(12)).decode(),
            "reserved": True,
            "backend_roles": list(spec.get("backend_roles") or []),
            "description": spec.get("description", ""),
        }
    if doc == old:
        print("changed" if _fix_owner(path, uid, gid) else "unchanged")
        return 0
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    _fix_owner(tmp, uid, gid)
    os.replace(tmp, path)
    print("changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
