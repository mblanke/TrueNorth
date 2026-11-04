#!/usr/bin/env python3
"""TrueNorth Claude Code guard hook (PreToolUse).

Turns the range safety rails into hard blocks for Claude. Exit code 2 blocks the tool
call and the stderr message goes back to Claude. Any internal error also blocks
(fail closed).

This is defence in depth, not a sandbox. The CLI (tools/range) and the playbooks
(range_load: approval-check, plan integrity) enforce the same gates independently.

Rules
  R1  secrets: no vault decrypt/view/edit, no printing env or credential variables,
      no reading vault or secret files
  R2  govc: read-only verbs only
  R3  ansible-playbook: exactly one playbook from playbooks/; --check is allowed; a real
      run needs a single range_id, a live human approval and no blackout. Refused:
      --skip-tags/--tags/--start-at-task/--step, -e JSON, -e @file (except @vault/),
      -e overrides of tn_*, plan or ansible_* variables
  R4  tools/range: build/reset/teardown need a live approval; approve/revoke/release
      are human-only
  R5  hooks, settings, approvals, blackout, build/ and the slot registry cannot be
      modified by Claude
  R6  the sensitive tools above must be called directly: not through sudo, bash -c,
      eval, xargs, python, pipes into a shell, command substitution, or wrappers
      other than timeout/nice/time/env
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
APPROVALS = ROOT / ".claude" / "approvals"
BLACKOUT = ROOT / "config" / "blackout.json"
RANGE_ID = re.compile(r"^[A-Z][A-Z0-9]{1,11}(-[0-9]{1,4})?$")

PROTECTED_PATHS = (".claude/hooks", ".claude/settings.json", ".claude/settings.local.json",
                   ".claude/approvals", "config/blackout.json", "config/security.json", "build/",
                   "ranges/.allocations.json", "vault/", "/etc/truenorth")

GOVC_READ = {"about", "ls", "find", "tree", "datacenter.info", "host.info", "vm.info",
             "datastore.info", "dvs.portgroup.info", "cluster.info", "pool.info",
             "tags.ls", "tags.category.ls", "tags.attached.ls", "tags.info", "tags.category.info",
             "object.collect", "events", "session.ls", "version", "library.ls", "library.info",
             "device.info", "vm.ip", "snapshot.tree", "host.portgroup.info", "metric.ls",
             "role.ls", "permissions.ls", "folder.info", "logs.ls"}

PLAYBOOK_ACTIONS = {"range_build.yml": "build", "range_reset.yml": "reset",
                    "range_teardown.yml": "teardown", "template_promote.yml": "promote"}
READONLY_PLAYBOOKS = {"range_validate.yml", "range_drift_collect.yml", "discovery_network.yml"}

# Words that mean "this command touches vCenter or the gates".
SENSITIVE = re.compile(r"ansible-playbook|\bgovc\b|tools/range|(^|[\s;&|(`'\"])\.?/?range\s|\btnrange\b|"
                       r"gates\.|community\.vmware|vmware\.vmware|\bansible\s+\S|Connect-VIServer|\bpwsh\b")
# Ways of running something indirectly. Combined with SENSITIVE, these are refused.
INDIRECT = re.compile(r"\b(bash|sh|zsh|dash|ksh|fish)\s+(-[a-z]*c|-s|-)\b|\beval\b|\bxargs\b|\bsource\b|"
                      r"(^|[\s;&|])\.\s+\S|\bpython[0-9.]*\b|\bperl\b|\bruby\b|\bnode\b|\bexec\b|\bsudo\b|"
                      r"\bsu\b|\bdoas\b|\bwatch\b|\bnohup\b|\bsetsid\b|\bscreen\b|\btmux\b|\bparallel\b|"
                      r"-exec\b|`|\$\(|\|\s*(bash|sh|zsh|dash|ksh|python[0-9.]*|perl)\b|\bat\s|\bcrontab\b|\bsystemd-run\b|"
                      r"\bscript\b|\bexpect\b|\bunbuffer\b|\bsocat\b|\bssh\b|\bflock\b|\bstdbuf\b|\bbusybox\b")
# Ansible/TrueNorth environment that changes what runs or where.
DANGEROUS_ENV = re.compile(r"\b(TNRANGE_ROOT|ANSIBLE_CONFIG|ANSIBLE_ROLES_PATH|ANSIBLE_LIBRARY|"
                           r"ANSIBLE_COLLECTIONS_PATHS?|ANSIBLE_[A-Z_]*PLUGINS?|ANSIBLE_INVENTORY|"
                           r"ANSIBLE_VARS_ENABLED|ANSIBLE_SKIP_TAGS|ANSIBLE_RUN_TAGS|ANSIBLE_CHECK_MODE_MARKERS)\s*=")

WRAPPERS = {"time", "command", "nice", "ionice", "timeout", "env"}
SHELL_KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "(", ")"}
READONLY_TOOLS = {"grep", "rg", "egrep", "fgrep", "cat", "head", "tail", "less", "more", "wc", "ls",
                  "git", "diff", "jq", "yq", "echo", "printf", "awk", "sed", "sort", "uniq", "cut",
                  "ansible-doc", "ansible-lint", "ansible-galaxy", "ansible-inventory", "ansible-config",
                  "stat", "file", "tree", "realpath", "dirname", "basename", "test", "["}

SECRET_PATTERNS = [
    (r"\bansible-vault\s+(view|decrypt|edit|rekey)\b", "ansible-vault view/decrypt/edit/rekey reveals or alters secrets"),
    (r"(^|[;&|]\s*)(env|printenv|set|export|declare\s+-x)\s*($|[;&|>])", "printing the environment can expose credentials"),
    (r"\bprintenv\b", "printenv can expose credentials"),
    (r"\bgovc\s+env\b", "govc env prints credentials"),
    (r"\$\{?[A-Z_]*(PASS|PASSWORD|TOKEN|SECRET|KEY)[A-Z_]*\}?", "referencing credential variables in a shell command"),
    (r"\bGOVC_URL=\S*:[^@\s]+@", "credentials embedded in GOVC_URL"),
    (r"/proc/\S*/environ", "reading process environments can expose credentials"),
    (r"(cat|less|more|head|tail|bat|strings|xxd|od|grep|rg|awk|sed|cp|scp|base64)\b[^|;&]*"
     r"(\bvault/\S+|vault[^/\s]*\.ya?ml|\.vault_pass|vault[-_]?pass|secrets?\.ya?ml|\.pem\b|id_rsa|\.kdbx)",
     "reading vault, key or secret files"),
    (r"\b-e\s+['\"]?[^ ]*(password|token|secret)\s*=", "passing a secret on the command line"),
    (r"\bdebug\b.*\bvar=\s*(vcenter_password|guest_creds)", "printing a credential variable"),
]


class Blocked(Exception):
    pass


def block(msg: str) -> None:
    raise Blocked(msg)


# --------------------------------------------------------------------- gates

def active_blackout() -> str | None:
    if not BLACKOUT.exists():
        return None
    data = json.loads(BLACKOUT.read_text())
    now = dt.datetime.now(dt.timezone.utc)
    for w in data.get("windows", []) or []:
        s, e = dt.datetime.fromisoformat(w["start"]), dt.datetime.fromisoformat(w["end"])
        if s.tzinfo is None or e.tzinfo is None:
            return f"config/blackout.json window {w} has no UTC offset (failing closed)"
        if s <= now < e:
            return f"blackout until {w['end']}: {w.get('reason', '')}"
    return None


def current_plan_sha(rid: str) -> str | None:
    p = ROOT / "build" / rid / "plan.json"
    try:
        return json.loads(p.read_text()).get("plan_sha256")
    except (OSError, ValueError):
        return None


def approval_ok(rid: str, action: str) -> str | None:
    p = APPROVALS / f"{rid}.json"
    if not p.exists():
        return (f"no human approval for {rid}. Ask Marc-Andre (or the range owner) to run "
                f"in their own terminal: tools/range approve {rid} --action {action}")
    rec = json.loads(p.read_text())
    exp = dt.datetime.fromisoformat(rec["expires"])
    if rec.get("range_id") != rid:
        return f"approval file is for {rec.get('range_id')}, not {rid}"
    if action not in rec.get("actions", []):
        return f"approval for {rid} does not cover '{action}' (covers {rec.get('actions')})"
    if dt.datetime.now(dt.timezone.utc) >= exp:
        return f"approval for {rid} expired at {rec['expires']}"
    if action in ("build", "reset", "teardown") and rec.get("plan_sha256") != current_plan_sha(rid):
        return f"approval for {rid} was for a different plan; ask for a new approval after review"
    return None


def require_gate(rid: str, action: str) -> None:
    b = active_blackout()
    if b:
        block(b)
    err = approval_ok(rid, action)
    if err:
        block(err)


# ------------------------------------------------------------------ parsing

def segments(cmd: str) -> list[list[str]]:
    """Quote-aware split on shell control operators and grouping characters."""
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        block("could not parse the command (unbalanced quotes); simplify it")
    out, cur = [], []
    for tok in tokens:
        if tok and set(tok) <= set(";&|()") or tok in ("{", "}"):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def strip_prefix(tokens: list[str]) -> list[str]:
    """Remove shell keywords, env assignments and harmless wrappers from the front."""
    t = list(tokens)
    while t:
        head = t[0]
        if head in SHELL_KEYWORDS:
            t = t[1:]
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            t = t[1:]
        elif head == "env":
            t = t[1:]
            while t and (t[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t[0])):
                if t[0] in ("-u", "--unset") and len(t) > 1:
                    t = t[1:]
                t = t[1:]
        elif head == "timeout":
            t = t[1:]
            while t and t[0].startswith("-"):
                t = t[2:] if t[0] in ("-s", "--signal", "-k", "--kill-after") else t[1:]
            t = t[1:]  # duration
        elif head in ("nice", "ionice"):
            t = t[1:]
            while t and t[0].startswith("-"):
                t = t[2:] if t[0] in ("-n", "-c", "--adjustment") else t[1:]
        elif head in ("time", "command"):
            t = t[1:]
        else:
            break
    return t


def check_operator_args(value: str) -> None:
    """TNRANGE_ANSIBLE_ARGS may only carry vault options and verbosity."""
    try:
        args = shlex.split(value)
    except ValueError:
        block("TNRANGE_ANSIBLE_ARGS could not be parsed")
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-e", "--extra-vars") and i + 1 < len(args) and re.match(r"^@vault/[\w.-]+$", args[i + 1]):
            i += 2
        elif a in ("--vault-id", "--vault-password-file") and i + 1 < len(args):
            i += 2
        elif re.match(r"^-v{1,4}$", a) or a == "--ask-vault-pass":
            i += 1
        else:
            block(f"TNRANGE_ANSIBLE_ARGS may only contain '-e @vault/<file>', vault options and -v (found '{a}')")


def parse_extra_vars(tokens: list[str]) -> dict[str, list[str]]:
    ev: dict[str, list[str]] = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        val = None
        if t in ("-e", "--extra-vars"):
            val = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 1
        elif t.startswith("--extra-vars="):
            val = t.split("=", 1)[1]
        elif t.startswith("--") and "extra" in t:
            block(f"'{t}' looks like an abbreviated --extra-vars; spell options in full")
        elif t.startswith("-e") and len(t) > 2:
            val = t[2:]
        if val is not None:
            v = val.strip()
            if v.startswith("@"):
                vp = (ROOT / v[1:]).resolve()
                if not re.match(r"^@vault/[\w.-]+$", v) or vp.parent != (ROOT / "vault").resolve():
                    block("-e @file is only allowed for vault files (@vault/<file>)")
            elif v.startswith(("{", "[")):
                block("-e with JSON/YAML values is not allowed; use key=value")
            else:
                for kv in shlex.split(v):
                    if "=" not in kv:
                        block(f"-e value '{kv}' is not key=value")
                    k, x = kv.split("=", 1)
                    ev.setdefault(k, []).append(x)
        i += 1
    return ev


PLAYBOOK_FLAGS_WITH_VALUE = {"-e", "--extra-vars", "-i", "--inventory", "-c", "--connection",
                             "--vault-id", "--vault-password-file"}
PLAYBOOK_FLAGS = {"--check", "-C", "--diff", "-D", "--syntax-check", "--list-tasks", "--list-hosts",
                  "--list-tags", "-v", "-vv", "-vvv", "-vvvv", "--ask-vault-pass"}


def check_ansible_playbook(tokens: list[str]) -> None:
    args = tokens[1:]
    # Exact-match allowlist. ansible-playbook accepts unique prefixes of long options
    # (--extra-var, --skip-tag), so anything not spelled exactly as listed is refused.
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            name = a.split("=", 1)[0]
            if name in PLAYBOOK_FLAGS_WITH_VALUE:
                i += 1 if "=" in a else 2
                continue
            if a in PLAYBOOK_FLAGS:
                i += 1
                continue
            if re.match(r"^-e.+", a):
                i += 1
                continue
            block(f"ansible-playbook option '{a}' is not allowed for Claude (allowed: "
                  f"{', '.join(sorted(PLAYBOOK_FLAGS | PLAYBOOK_FLAGS_WITH_VALUE))})")
        i += 1
    ev = parse_extra_vars(args)
    for k, vals in ev.items():
        if k.startswith(("tn_", "ansible_")) or k in ("plan", "tn_plan_file", "vcenter_hostname"):
            if not (k == "tn_no_log" and all(v.lower() in ("true", "1", "yes") for v in vals)):
                block(f"overriding '{k}' with -e is not allowed")
        if len(set(vals)) > 1:
            block(f"'{k}' is given more than once with different values")
    value_flags = {"-e", "--extra-vars", "-i", "--inventory", "--vault-id", "--vault-password-file",
                   "-c", "--connection", "-u", "--user", "-l", "--limit", "-f", "--forks", "-T", "--timeout"}
    positional, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in value_flags:
            skip = True
            continue
        if not a.startswith("-"):
            positional.append(a)
    if len(positional) != 1:
        block(f"exactly one playbook per run is allowed (found {len(positional)}: {positional})")
    pb = Path(positional[0])
    name = pb.name
    resolved = (ROOT / pb).resolve() if not pb.is_absolute() else pb.resolve()
    if resolved.parent != (ROOT / "playbooks").resolve():
        block(f"playbooks must come from {ROOT / 'playbooks'} (got {positional[0]})")
    if any(a in args for a in ("--syntax-check", "--list-tasks", "--list-hosts", "--list-tags")):
        return
    rid_vals = ev.get("range_id", [])
    if "--check" in args or "-C" in args:
        return
    if name in READONLY_PLAYBOOKS:
        if name != "discovery_network.yml" and (len(rid_vals) != 1 or not RANGE_ID.match(rid_vals[0])):
            block("pass exactly one range: -e range_id=<ID>")
        return
    action = PLAYBOOK_ACTIONS.get(name)
    if action is None:
        block(f"real (non --check) run of '{name}' is not a known range playbook; ask a human")
    rid = "TEMPLATES" if action == "promote" else (rid_vals[0] if len(rid_vals) == 1 else "")
    if not RANGE_ID.match(rid):
        block("pass exactly one range: -e range_id=<ID>. 'all' and wildcards are refused")
    require_gate(rid, action)
    if action == "teardown" and ev.get("confirm") != [rid]:
        block(f"teardown needs -e confirm={rid}")


def check_range_cli(tokens: list[str]) -> None:
    args = tokens[1:]
    if not args:
        return
    sub = args[0]
    if sub in ("approve", "revoke", "release"):
        block(f"'range {sub}' is human-only. Ask Marc-Andre or the range owner to run it")
    if sub in ("build", "reset", "teardown", "validate-live"):
        if len(args) < 2 or not RANGE_ID.match(args[1]):
            block(f"'range {sub}' needs exactly one range ID")
        if sub == "validate-live":
            b = active_blackout()
            if b:
                block(b)
            return
        require_gate(args[1], sub)
        if sub == "teardown":
            conf = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--confirm"]
            conf += [a.split("=", 1)[1] for a in args if a.startswith("--confirm=")]
            if conf != [args[1]]:
                block(f"teardown needs --confirm {args[1]}")


def check_adhoc_ansible(tokens: list[str]) -> None:
    mod = ""
    for i, a in enumerate(tokens):
        if a in ("-m", "--module-name") and i + 1 < len(tokens):
            mod = tokens[i + 1]
        elif a.startswith("--module-name="):
            mod = a.split("=", 1)[1]
        elif a.startswith("-m") and len(a) > 2:
            mod = a[2:]
    base = mod.split(".")[-1]
    if not (base.endswith("_info") or base in ("ping", "debug", "setup", "")):
        block(f"ad-hoc 'ansible -m {mod}' can change systems; use the range playbooks")


def check_bash(cmd: str) -> None:
    for pat, why in SECRET_PATTERNS:
        if re.search(pat, cmd, flags=re.IGNORECASE):
            block(why)
    for p in PROTECTED_PATHS:
        if p in cmd and re.search(r"(>|\btee\b|\bsed\s+-i|\bmv\b|\bcp\b|\brm\b|\bchmod\b|\bchown\b|\btouch\b|"
                                  r"\btruncate\b|\bln\b|\bpython[0-9.]*\b|\bperl\b|\bdd\b|\binstall\b|"
                                  r"\bmkdir\b|\brsync\b|\btar\b|\bunzip\b|\bgit\s+(checkout|restore|reset|stash|clean)\b)", cmd):
            block(f"{p} is protected; only a human or the range CLI may change it")
    if DANGEROUS_ENV.search(cmd):
        block("setting TNRANGE_ROOT or Ansible path/plugin/tag variables is not allowed")
    for m in re.finditer(r"TNRANGE_ANSIBLE_ARGS=(\"[^\"]*\"|'[^']*'|\S*)", cmd):
        check_operator_args(m.group(1).strip("'\""))
    sensitive = bool(SENSITIVE.search(cmd))
    if sensitive and INDIRECT.search(cmd):
        block("run ansible-playbook, govc and tools/range directly, not through sudo, a shell, "
              "an interpreter, xargs, eval, pipes or command substitution")
    if re.search(r"\bapprove\b", cmd) and re.search(r"(^|[/\s])range\b|tnrange|gates", cmd):
        block("approvals are human-only; ask Marc-Andre or the range owner to approve")
    if re.search(r"(^|[\s;&|/])ci/\S+\.sh\b", cmd):
        block("CI scripts approve and change ranges themselves; they run under the CI account, not Claude")
    if re.search(r"\bgit\b.*(\s-c\s|\balias\.|config\s+.*alias)", cmd):
        block("git -c / aliases can run arbitrary commands; not allowed")
    if "$'" in cmd or '$"' in cmd:
        if sensitive:
            block("ANSI-C or locale quoting around commands is not allowed")
    for seg in segments(cmd):
        t = strip_prefix(seg)
        if not t:
            continue
        if re.search(r"[$*?\[\]{}~]", t[0]):
            block(f"the command word '{t[0]}' uses a variable, glob or expansion; spell the command out")
        exe = Path(t[0]).name
        if exe == "govc":
            verb = t[1] if len(t) > 1 else ""
            if verb not in GOVC_READ and not verb.endswith((".info", ".ls")):
                block(f"govc '{verb}' is not a read-only verb; changes go through the range playbooks")
        elif exe == "ansible-playbook":
            check_ansible_playbook(t)
        elif exe == "range":
            check_range_cli(t)
        elif exe == "ansible":
            check_adhoc_ansible(t)
        elif exe in ("pwsh", "powershell"):
            block("PowerCLI is not allowed; use the range playbooks")
        elif exe == "export":
            continue  # values were checked above (DANGEROUS_ENV, TNRANGE_ANSIBLE_ARGS)
        elif exe not in READONLY_TOOLS:
            # A sensitive tool appearing as an argument of something that is not a
            # read-only viewer means it is being launched indirectly.
            for a in t[1:]:
                if Path(a).name in ("ansible-playbook", "govc", "range", "ansible") or "tnrange" in a:
                    block(f"'{exe}' would launch {Path(a).name}; call it directly instead")


def check_write(path: str) -> None:
    rel = path
    try:
        rel = str(Path(path).resolve().relative_to(ROOT.resolve()))
    except ValueError:
        pass
    for p in PROTECTED_PATHS:
        pp = p.rstrip("/")
        if rel == pp or rel.startswith(pp + "/") or f"/{pp}/" in path or path.endswith("/" + pp):
            block(f"{p} is protected; only a human or the range CLI may change it")
    if re.search(r"(^|/)(vault|secrets?)(/|[-_.])", rel) or rel.endswith((".vault_pass", ".pem", ".key")):
        block("writing vault, secret or key files is not allowed for Claude")


def main() -> None:
    event = json.load(sys.stdin)
    tool = event.get("tool_name", "")
    ti = event.get("tool_input", {}) or {}
    if tool == "Bash":
        check_bash(ti.get("command", ""))
    elif tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        check_write(ti.get("file_path") or ti.get("notebook_path") or "")
    elif tool == "Read":
        fp = ti.get("file_path", "")
        if re.search(r"(/vault/|vault[^/]*\.ya?ml$|\.vault_pass|secrets?\.ya?ml$|\.pem$|id_rsa)", fp):
            block("reading vault, key or secret files is not allowed for Claude")


if __name__ == "__main__":
    try:
        main()
    except Blocked as exc:
        print(f"BLOCKED by TrueNorth guard: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # fail closed on anything unexpected
        print(f"BLOCKED by TrueNorth guard: internal error ({type(exc).__name__}: {exc}); failing closed",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(0)
