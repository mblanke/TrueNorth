#!/usr/bin/env python3
"""Write OpenSearch's internal_users.yml from passwords given on stdin (never argv).

stdin: {"path": "<file>", "users": {"<name>": {"password": "...", "backend_roles": [...],
        "description": "..."}}}

The file is JSON, which is YAML, so the security plugin reads it as is. A user's existing
bcrypt hash is kept while it still matches the password, so a re-run changes nothing and
prints "unchanged"; otherwise the file is rewritten and "changed" is printed. Needs
python3-bcrypt (roles/tn_base).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import bcrypt


def main() -> int:
    req = json.load(sys.stdin)
    path = req["path"]
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        old = {}
    doc: dict = {"_meta": {"type": "internalusers", "config_version": 2}}
    for name, spec in req["users"].items():
        password = spec["password"].encode()
        if not password:
            sys.exit(f"empty password for {name}")
        prev = (old.get(name) or {}).get("hash", "")
        keep = bool(prev) and bcrypt.checkpw(password, prev.encode())
        doc[name] = {
            "hash": prev if keep else bcrypt.hashpw(password, bcrypt.gensalt(12)).decode(),
            "reserved": True,
            "backend_roles": list(spec.get("backend_roles") or []),
            "description": spec.get("description", ""),
        }
    if doc == old:
        print("unchanged")
        return 0
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    print("changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
