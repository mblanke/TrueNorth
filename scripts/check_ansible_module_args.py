#!/usr/bin/env python3
"""Fail if an installer task passes a module an argument the module does not take.

ansible-lint and --syntax-check both accepted `dest:` on
community.crypto.x509_certificate_convert, whose argument is `dest_path`; the install
on TN-MGMT01 then stopped at that task (2026-10-05). This checks every task in install/
against the argument spec in each module's own documentation, from the collections
installed for the run (ANSIBLE_COLLECTIONS_PATH, or the defaults).

    cd install && python ../scripts/check_ansible_module_args.py
"""

from __future__ import annotations

import glob
import os
import sys

import yaml
from ansible.plugins import loader
from ansible.utils.plugin_docs import get_docstring

# Task keywords, not module arguments.
KEYWORDS = {
    "name", "when", "register", "become", "become_user", "loop", "loop_control", "notify", "changed_when",
    "failed_when", "no_log", "until", "retries", "delay", "vars", "environment", "check_mode", "delegate_to",
    "tags", "block", "rescue", "always", "ignore_errors", "listen", "run_once", "args",
}
# Modules whose arguments are free-form.
FREE_FORM = {"ansible.builtin.set_fact", "ansible.builtin.command", "ansible.builtin.shell", "ansible.builtin.include_tasks",
             "ansible.builtin.include_role", "ansible.builtin.import_playbook", "ansible.builtin.meta"}


def tasks(items):
    for t in items or []:
        if isinstance(t, dict):
            for section in ("block", "rescue", "always"):
                if section in t:
                    yield from tasks(t[section])
            if not any(s in t for s in ("block", "rescue", "always")):
                yield t


def main() -> int:
    paths = [p for p in os.getenv("ANSIBLE_COLLECTIONS_PATH", "").split(":") if p]
    loader.init_plugin_loader(paths or None)
    problems = 0
    files = glob.glob("roles/*/tasks/*.yml") + glob.glob("roles/*/handlers/*.yml") + glob.glob("playbooks/*.yml")
    for f in sorted(files):
        with open(f) as fh:
            doc = yaml.safe_load(fh) or []
        plays = [p for p in doc if isinstance(p, dict) and "hosts" in p]
        items = [t for p in plays for t in (p.get("tasks") or []) + (p.get("handlers") or [])] if plays else doc
        for t in tasks(items):
            mods = [k for k in t if k not in KEYWORDS]
            if len(mods) != 1 or mods[0] in FREE_FORM or not isinstance(t[mods[0]], dict):
                continue
            ctx = loader.module_loader.find_plugin_with_context(mods[0])
            if not ctx.resolved:
                print(f"{f}: {t.get('name')}: module {mods[0]} not found (collections installed?)")
                problems += 1
                continue
            spec, _, _, _ = get_docstring(ctx.plugin_resolved_path, fragment_loader=loader.fragment_loader)
            known = set()
            for opt, info in (spec.get("options") or {}).items():
                known |= {opt, *(info.get("aliases") or [])}
            if unknown := sorted(a for a in t[mods[0]] if a not in known):
                print(f"{f}: {t.get('name')}: {mods[0]} does not take {unknown}")
                problems += 1
    print(f"module arguments checked; {problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
