"""A fresh host has no deployment record, and messages must still render.

ansible-core templates an assert's fail_msg/success_msg even when the assertion passes, so
a message that reads a field of the previous deployment's state (app.json, which a fresh
host does not have) crashed the first release install on staging (2026-10-09):
"object of type 'dict' has no attribute 'release'". Every such read needs a default.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"

# Facts built from state a fresh host does not have yet: empty dicts on a first install.
FRESH_HOST_EMPTY = ("tn_app_prev_state",)
_READ = re.compile(r"\b(" + "|".join(FRESH_HOST_EMPTY) + r")\.(\w+)(?![\w(])((?:\s*\|\s*\w+(?:\([^)]*\))?)*)")


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks"):
            if key in node:
                yield from _tasks(node[key])


def test_messages_default_every_read_of_fresh_host_state() -> None:
    offenders = []
    for path in sorted(INSTALL.rglob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        for task in _tasks(doc):
            module = task.get("ansible.builtin.assert") or task.get("assert")
            if not isinstance(module, dict):
                continue
            for key in ("fail_msg", "success_msg", "msg"):
                text = str(module.get(key, ""))
                for match in _READ.finditer(text):
                    if "default(" not in match.group(3):
                        offenders.append(f"{path.relative_to(ROOT)}: {task.get('name')}: {match.group(0)}")
    assert not offenders, "\n".join(offenders)
