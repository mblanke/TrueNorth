#!/usr/bin/env python3
"""PreToolUse write gate for TAZ (rings 1-6). Vendored from TAZ's model-write-gate.py.

Installed after the 2026-08-19 incident, in which a Claude Code session whose main loop
ran on the local fleet alias `agent` (gpt-oss-120b) rewrote seed.py and main.py whole-file,
from post-compaction memory, and left the control-plane API serving zero routes. Nothing
in the repo or the harness stopped it.

Six independent rings, cheapest first. Rings 1-3 are the original gate; 4-6 contain a
local model working a task card (tools/taz/README.md):

  1. capability -- the SERVED model is not on the write allowlist (code files only)
  2. whole-file -- Write (not Edit) over a large existing tracked file (code files only)
  3. amnesia    -- the target was not re-Read since the last compaction (code files only)
  4. card       -- with TAZ_CARD set, writes outside the card's allowed_paths (any file)
  5. protected  -- oracle tests, baselines, CI, hooks, migrations, auth/tenancy,
                   provisioners, lockfiles... (policy `protected_paths`; any file)
  6. bash       -- a Bash command that writes files (redirects, tee, sed -i, cp, ...)

Rings 2 and 3 are model-agnostic on purpose: the incident's mechanism was context
amnesia plus a whole-file Write, which any model can reproduce. Rings 4-6 apply to every
model too.

Ring 6 is defence in depth, not a sandbox. It parses the command conservatively and
allows only a list of read-only commands and the test runners; anything it cannot
analyse is refused. It cannot see what an allowed test runner executes: pytest, Karma and
dod.sh run code the model may have written. Containing a hostile model is the job of the
container and of the CI taz-guard check; see "Limits" in tools/taz/README.md.

Why the model comes from the transcript and not $ANTHROPIC_MODEL: LiteLLM passes the
*backend's* name through, so a session launched with ANTHROPIC_MODEL=coder-fast that
silently fell back to gpt-oss-120b records `gpt-oss-120b` there. Reading the env var
would be fooled by exactly the silent downgrade this gate exists to catch.

Contract: exit 0, with hookSpecificOutput.permissionDecision "deny" to refuse and no
output for "no objection". PreToolUse fires before any permission-mode check, so this
holds under --dangerously-skip-permissions. Any input the gate cannot understand is
refused (fail closed).

Configuration: `--policy PATH` or $TAZ_GATE_POLICY, else policy.json beside this file.
A policy that is missing everywhere, or exists but cannot be read, refuses every call;
the built-in defaults are only the floor that the file is merged over. $TAZ_CARD names the task card (JSON); $TAZ_GATE_LOG overrides the policy's
`log_path`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

HOOK_DIR = os.path.dirname(os.path.realpath(__file__))

# What rings 1-3 guard. The gate exists so a non-coding model does not write CODE -- that
# is the failure it was built for (seed.py losing every import). Prose is not that.
CODE_SUFFIXES = {
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs",
    ".java", ".kt", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs",
    ".sh", ".bash", ".zsh", ".ps1", ".sql", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".tf", ".tfvars", ".gradle", ".xml", ".json",
    ".html", ".scss", ".css",
}
CODE_NAMES = {
    "Dockerfile", "Makefile", "Jenkinsfile", "Vagrantfile", "Procfile",
    "docker-compose.yml", "pyproject.toml", "requirements.txt", "package.json",
}

DEFAULT_POLICY: dict = {
    # Case-insensitive substring match against the served model name.
    "write_allowlist": ["claude-"],
    "rings": {"capability": True, "whole_file": True, "amnesia": True,
              "card": True, "protected": True, "bash": True},
    "whole_file_max_lines": 150,
    # Absolute directories that are always writable (scratch, plans), matched as exact
    # path prefixes after realpath. A path inside a work dir is never exempt.
    "exempt_roots": ["~/.claude/plans"],
    "mutating_tools": ["Write", "Edit", "MultiEdit", "NotebookEdit"],
    "unguarded_suffixes": [".md", ".markdown", ".txt", ".rst", ".adoc", ".org",
                           ".csv", ".tsv", ".log", ".patch", ".diff"],
    "extra_code_suffixes": [],
    "extra_code_names": [],
    "protected_paths": [
        ".dod-*baseline", "**/.dod-*baseline", ".git/**", "**/.git", "**/.git/**",
        ".github/**", ".claude/**", "tools/taz/hooks/**", "scripts/taz_guard.py",
        "scripts/dod.sh", "**/alembic/versions/**", "**/provisioners/**",
        "**/result_transfer/**", "**/classes/onboarding.py", "**/rbac.py",
        "**/tenancy.py", "**/package-lock.json", "**/*.lock",
    ],
    "bash": {"extra_readonly_commands": [], "allowed_scripts": ["scripts/dod.sh"], "npm_scripts": ["test"],
             "safe_env": ["CHROME_BIN", "DOD_WEB", "CI", "NO_COLOR"]},
    "log_path": None,
}

# Always protected, whatever the policy says: the gate's own config and git internals.
FIXED_PROTECTED = (".git/**", "**/.git", "**/.git/**", "tools/taz/hooks/**")


class GateError(Exception):
    """Input or configuration the gate cannot trust. Always becomes a deny."""


class DeniedError(Exception):
    def __init__(self, ring: str, reason: str, detail: str = ""):
        super().__init__(reason)
        self.ring, self.reason, self.detail = ring, reason, detail


# ── configuration ─────────────────────────────────────────────────────────


def _str_list(value, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise GateError(f"policy `{name}` must be a list of strings")
    return value


def load_policy(path: str | None, explicit: bool) -> dict:
    """Merge the policy file over the defaults. A file that exists but is unreadable,
    not JSON, or the wrong shape raises GateError: a broken policy must not open the gate.
    """
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    if path is None:
        return policy
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        # No policy at all is a broken install, not "use the defaults": the built-in
        # defaults are a floor, weaker than the shipped policy.json. Fail closed.
        raise GateError(f"policy file {path} not found") from None
    except Exception as exc:  # any failure to read is a deny
        raise GateError(f"policy file {path} unreadable: {exc}") from None
    if not isinstance(data, dict):
        raise GateError("policy must be a JSON object")
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if key in ("rings", "bash"):
            if not isinstance(value, dict):
                raise GateError(f"policy `{key}` must be an object")
            policy[key].update(value)
        else:
            policy[key] = value
    for key in ("write_allowlist", "exempt_roots", "mutating_tools", "unguarded_suffixes",
                "extra_code_suffixes", "extra_code_names", "protected_paths"):
        _str_list(policy[key], key)
    for key in ("extra_readonly_commands", "allowed_scripts", "npm_scripts", "safe_env"):
        _str_list(policy["bash"].get(key, []), f"bash.{key}")
    if not isinstance(policy["whole_file_max_lines"], int):
        raise GateError("policy `whole_file_max_lines` must be an integer")
    # An empty entry would match every model name.
    policy["write_allowlist"] = [t.lower() for t in policy["write_allowlist"] if t.strip()]
    return policy


def load_card(path: str | None) -> dict | None:
    """The task card: {"repo_root": abs path, "allowed_paths": [globs], "oracle_tests":
    [paths or globs], ...}. Unset means no card; set but unusable raises GateError."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            card = json.load(fh)
    except Exception as exc:
        raise GateError(f"task card {path} unreadable: {exc}") from None
    if not isinstance(card, dict):
        raise GateError("task card must be a JSON object")
    root = card.get("repo_root")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise GateError("task card needs an absolute `repo_root`")
    allowed = card.get("allowed_paths")
    if not isinstance(allowed, list) or not allowed or not all(isinstance(g, str) and g for g in allowed):
        raise GateError("task card needs a non-empty `allowed_paths` list")
    oracles = card.get("oracle_tests", [])
    if not isinstance(oracles, list) or not all(isinstance(g, str) and g for g in oracles):
        raise GateError("task card `oracle_tests` must be a list of paths")
    return {"repo_root": root, "allowed_paths": allowed, "oracle_tests": oracles}


# ── paths ─────────────────────────────────────────────────────────────────


def glob_to_regex(glob: str, ignore_case: bool = False) -> re.Pattern:
    """``**`` crosses directories, ``*`` and ``?`` stay inside one path segment.
    Same dialect as tools/taz/bakeoff/runner.py and scripts/taz_guard.py."""
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.I if ignore_case else 0)


def matches_any(path: str, globs, ignore_case: bool = False) -> bool:
    return any(glob_to_regex(g, ignore_case).match(path) for g in globs)


def under(path: str, root: str) -> bool:
    """Exact path-prefix test on whole components: /data/scratch-x is not under /data/scratch."""
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root if root == "/" else root + "/")


def path_forms(path: str) -> list[str]:
    """The lexical and the symlink-resolved form; a rule must hold for both."""
    lexical = os.path.normpath(path)
    real = os.path.realpath(path)
    return [lexical] if real == lexical else [lexical, real]


def find_repo_root(path: str) -> str | None:
    """Nearest ancestor (or self) holding a `.git` entry, without running git."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def work_dirs(forms: list[str], cwd: str, card: dict | None) -> list[str]:
    """Every repository the target or the session lives in, plus the card's repo."""
    roots: set[str] = set()
    if card:
        roots.update(path_forms(card["repo_root"]))
    for p in [*forms, *path_forms(cwd)]:
        r = find_repo_root(p)
        if r:
            roots.update(path_forms(r))
    return sorted(roots)


def exempt_roots(policy: dict) -> list[str]:
    out = []
    for raw in policy["exempt_roots"]:
        root = os.path.expanduser(raw)
        if not os.path.isabs(root) or os.path.normpath(root) == "/":
            continue  # relative or "/" entries are ignored, never widened
        out.extend(path_forms(root))
    return out


def is_exempt(forms: list[str], roots: list[str], wdirs: list[str]) -> bool:
    """Every form under an exempt root, and none inside a work dir. The second clause is
    what keeps a repo cloned under a scratch root (or a repo path that merely contains
    "/tmp/") from being waved through."""
    if not roots:
        return False
    if any(under(f, w) for f in forms for w in wdirs):
        return False
    return all(any(under(f, r) for r in roots) for f in forms)


def is_code_path(path: str, policy: dict) -> bool:
    name = os.path.basename(path)
    if name in CODE_NAMES or name in set(policy["extra_code_names"]):
        return True
    ext = os.path.splitext(name)[1].lower()
    suffixes = (CODE_SUFFIXES | set(policy["extra_code_suffixes"])) - set(policy["unguarded_suffixes"])
    return ext in suffixes


# ── transcript (rings 1 and 3) ────────────────────────────────────────────


def scan_transcript(path) -> tuple[str | None, int, dict]:
    """(served_model, index of the last compact_boundary or -1, {realpath: last Read index})."""
    served, compact_idx, last_read = None, -1, {}
    if not isinstance(path, str) or not path:
        return served, compact_idx, last_read
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = list(fh)
    except OSError:
        return served, compact_idx, last_read
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("subtype") == "compact_boundary":
            compact_idx = i
            continue
        msg = rec.get("message")
        if rec.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        model = msg.get("model")
        if isinstance(model, str) and model and model != "<synthetic>":
            served = model
        content = msg.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "Read":
                continue
            fp = block.get("input", {}).get("file_path") if isinstance(block.get("input"), dict) else None
            if isinstance(fp, str) and fp:
                last_read[os.path.realpath(fp)] = i
    return served, compact_idx, last_read


def git_tracked(path: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", os.path.dirname(path) or ".", "ls-files", "--error-unmatch", "--", path],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


# ── ring 6: Bash ──────────────────────────────────────────────────────────

# Longest first. "\n" separates commands like ";".
_OPS = ("&&", "||", ";;", "|&", "&>>", "&>", ">>", ">|", ">&", "<<<", "<<-", "<<", "<>",
        "<&", "<(", ">(", ";", "&", "|", "(", ")", "<", ">", "\n")
_SEPARATORS = {"&&", "||", ";;", "|&", ";", "&", "|", "(", ")", "\n"}
_WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}


class Word:
    __slots__ = ("value", "quoted", "expands")

    def __init__(self):
        self.value = ""
        self.quoted = False   # any quoting or escaping in the word
        self.expands = False  # an unquoted or double-quoted `$`: value not known statically


class UnsupportedError(Exception):
    """Shell syntax ring 6 does not analyse; refused."""


def tokenize(cmd: str) -> list:
    """Split a command into Word objects and operator strings, the way bash would.

    Handles quoting, escapes, line continuations, comments and here-documents (whose
    bodies are skipped, after checking an unquoted body for command substitution).
    Raises UnsupportedError for command/process substitution and ANSI-C quoting.
    """
    tokens: list = []
    i, n = 0, len(cmd)
    word: Word | None = None
    want_delim: str | None = None  # the heredoc operator waiting for its delimiter
    heredocs: list[tuple[str, bool, bool]] = []  # (delimiter, strip tabs, quoted)

    def cur() -> Word:
        nonlocal word
        if word is None:
            word = Word()
        return word

    def flush():
        nonlocal word, want_delim
        if word is None:
            return
        if want_delim is not None:
            heredocs.append((word.value, want_delim == "<<-", word.quoted))
            want_delim = None
        tokens.append(word)
        word = None

    while i < n:
        c = cmd[i]
        if c == "\\":
            if i + 1 < n and cmd[i + 1] == "\n":
                i += 2
                continue
            if i + 1 >= n:
                raise UnsupportedError("trailing backslash")
            w = cur()
            w.value += cmd[i + 1]
            w.quoted = True
            i += 2
            continue
        if c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                raise UnsupportedError("unterminated single quote")
            w = cur()
            w.value += cmd[i + 1:j]
            w.quoted = True
            i = j + 1
            continue
        if c == '"':
            w = cur()
            w.quoted = True
            i += 1
            while True:
                if i >= n:
                    raise UnsupportedError("unterminated double quote")
                d = cmd[i]
                if d == '"':
                    i += 1
                    break
                if d == "\\" and i + 1 < n:
                    if cmd[i + 1] == "\n":
                        i += 2
                        continue
                    if cmd[i + 1] in '"\\$`':
                        w.value += cmd[i + 1]
                        i += 2
                        continue
                if d == "`" or cmd.startswith("$(", i):
                    raise UnsupportedError("command substitution")
                if d == "$":
                    w.expands = True
                w.value += d
                i += 1
            continue
        if c == "`":
            raise UnsupportedError("command substitution")
        if c == "$":
            if cmd.startswith("$(", i):
                raise UnsupportedError("command substitution")
            if cmd.startswith("$'", i) or cmd.startswith('$"', i):
                raise UnsupportedError("ANSI-C or locale quoting")
            w = cur()
            w.expands = True
            w.value += "$"
            i += 1
            continue
        if c in " \t\r":
            flush()
            i += 1
            continue
        if c == "#" and word is None:
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        op = next((o for o in _OPS if cmd.startswith(o, i)), None)
        if op is None:
            cur().value += c
            i += 1
            continue
        if op in ("<(", ">("):
            raise UnsupportedError("process substitution")
        if (op[0] in "<>" and word is not None and not word.quoted and not word.expands
                and word.value.isdigit() and want_delim is None):
            word = None  # `2>`: the digits name the fd; they are not an argument
        flush()
        if want_delim is not None:
            raise UnsupportedError("here-document without a delimiter")
        tokens.append(op)
        i += len(op)
        if op in ("<<", "<<-"):
            want_delim = op
        if op == "\n" and heredocs:
            i = _skip_heredoc_bodies(cmd, i, heredocs)
            heredocs = []
    flush()
    if want_delim is not None or heredocs:
        raise UnsupportedError("here-document without a body")
    return tokens


def _skip_heredoc_bodies(cmd: str, i: int, heredocs: list[tuple[str, bool, bool]]) -> int:
    lines = cmd[i:].split("\n")
    consumed = 0
    for delim, strip, quoted in heredocs:
        while True:
            if consumed >= len(lines):
                raise UnsupportedError(f"here-document `{delim}` is not terminated")
            line = lines[consumed]
            consumed += 1
            if (line.lstrip("\t") if strip else line) == delim:
                break
            if not quoted and ("$(" in line or "`" in line):
                raise UnsupportedError("command substitution in a here-document")
    rest = "\n".join(lines[consumed:])
    return len(cmd) - len(rest) if consumed < len(lines) else len(cmd)


# Commands that cannot write files whatever their arguments (output redirects are
# checked separately). Keep this list boring.
READONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "wc", "cut", "tr", "nl",
    "column", "diff", "cmp", "comm", "stat", "du", "df", "pwd", "echo", "printf",
    "which", "type", "true", "false", "test", "[", "[[", "]]", "basename", "dirname",
    "realpath", "readlink", "jq", "sha256sum", "sha1sum", "sha512sum", "md5sum", "shasum",
    "od", "hexdump", "seq", "sleep", "uname", "id", "whoami", "cd", ":", "exit", "read",
}
_KEYWORDS_STRIP = {"!", "{", "}", "then", "do", "else", "elif", "if", "while", "until"}
_KEYWORDS_END = {"fi", "done", "}"}
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=")
_PYTHON = re.compile(r"^python(\d+(\.\d+)?)?$")
_PATH_OK = re.compile(r"^(python(\d+(\.\d+)?)?|pytest|py\.test|ruff|ng|npm|npx|bash|sh)$")


class BashContext:
    def __init__(self, policy: dict, cwd: str, repo_root: str | None):
        bash = policy["bash"]
        self.readonly = READONLY_COMMANDS | set(bash.get("extra_readonly_commands", []))
        self.allowed_scripts = bash.get("allowed_scripts", [])
        self.npm_scripts = set(bash.get("npm_scripts", ["test"]))
        self.safe_env = set(bash.get("safe_env", []))
        self.cwd = cwd
        self.repo_root = repo_root
        self.saw_cd = False


def check_bash(cmd: str, ctx: BashContext) -> None:
    """Raise Denied if `cmd` could write a file (as far as this parser can tell)."""
    if "\x00" in cmd:
        raise DeniedError("bash", "NUL byte in command")
    try:
        tokens = tokenize(cmd)
    except UnsupportedError as exc:
        raise DeniedError("bash", f"unsupported shell syntax: {exc}") from None
    segment: list = []
    for tok in [*tokens, ";"]:
        if isinstance(tok, str) and tok in _SEPARATORS:
            _check_segment(segment, ctx)
            segment = []
        else:
            segment.append(tok)


def _check_segment(seg: list, ctx: BashContext) -> None:
    words: list[Word] = []
    i = 0
    while i < len(seg):
        tok = seg[i]
        if isinstance(tok, Word):
            words.append(tok)
            i += 1
            continue
        target = seg[i + 1] if i + 1 < len(seg) and isinstance(seg[i + 1], Word) else None
        if target is None:
            raise DeniedError("bash", f"redirect `{tok}` without a target")
        if tok in _WRITE_REDIRECTS:
            if target.value != "/dev/null" or target.expands:
                raise DeniedError("bash", f"output redirect `{tok} {target.value}` writes a file")
        elif tok in (">&", "<&"):
            if not (target.value.isdigit() or target.value == "-") or target.expands:
                raise DeniedError("bash", f"redirect `{tok}{target.value}` writes a file")
        elif tok == "<>":
            raise DeniedError("bash", "read-write redirect `<>` can create a file")
        i += 2  # "<", "<<", "<<-", "<<<": input only
    _check_argv(words, ctx)


def _check_argv(words: list[Word], ctx: BashContext) -> None:
    # Leading keywords and assignments.
    while words and not words[0].quoted and words[0].value in _KEYWORDS_STRIP:
        words = words[1:]
    if not words:
        return
    if not words[0].quoted and words[0].value in _KEYWORDS_END and len(words) == 1:
        return
    if not words[0].quoted and words[0].value == "for":
        return  # `for NAME in WORDS`: binds a variable; restricted commands refuse `$` args
    while words and (m := _ASSIGN.match(words[0].value)) and not words[0].quoted:
        if m.group(1) not in ctx.safe_env:
            raise DeniedError("bash", f"assignment to `{m.group(1)}` (only {sorted(ctx.safe_env)} may be set)")
        words = words[1:]
    if not words:
        return
    # Wrappers that run the rest of the line.
    name = words[0].value
    if name in ("time", "nice", "timeout", "env", "command"):
        return _check_wrapper(words, ctx)
    argv = [w.value for w in words]
    if words[0].expands or (name not in ("[", "[[") and any(ch in name for ch in "$*?[{~")):
        raise DeniedError("bash", f"command name `{name}` is not static")
    if "/" in name:
        base = os.path.basename(name)
        if ctx.allowed_scripts and _is_allowed_script(name, ctx) and len(argv) == 1:
            return
        if not _PATH_OK.match(base):
            raise DeniedError("bash", f"`{name}`: only the test runners may be run by path")
        name = base
    if name == "cd":
        ctx.saw_cd = True
        return
    if name in ctx.readonly:
        return _check_readonly(name, words)
    checker = _CHECKERS.get(name) or (_check_python if _PYTHON.match(name) else None)
    if checker is None:
        raise DeniedError("bash", f"`{name}` is not on the read-only command list")
    checker(words, ctx)


def _check_wrapper(words: list[Word], ctx: BashContext) -> None:
    name, rest = words[0].value, words[1:]
    if name == "time":
        rest = rest[1:] if rest and rest[0].value == "-p" else rest
    elif name == "nice":
        if rest and rest[0].value == "-n":
            rest = rest[2:]
    elif name == "timeout":
        while rest and rest[0].value.startswith("-"):
            rest = rest[1:]
        rest = rest[1:]  # the duration
    elif name == "env":
        if rest and rest[0].value.startswith("-"):
            raise DeniedError("bash", "`env` options are not allowed")
        if not rest:
            return
    elif name == "command":
        if rest and rest[0].value in ("-v", "-V"):
            return
        if rest and rest[0].value.startswith("-"):
            raise DeniedError("bash", "`command` options are not allowed")
    if not rest:
        return
    _check_argv(rest, ctx)


def _no_expansion(words: list[Word], name: str) -> None:
    for w in words[1:]:
        if w.expands:
            raise DeniedError("bash", f"`{name}` with a `$` argument: its value cannot be checked")


def _check_readonly(name: str, words: list[Word]) -> None:
    args = [w.value for w in words[1:]]
    if name == "date" and any(a in ("-s", "--set") or a.startswith("--set=") for a in args):
        raise DeniedError("bash", "`date --set`")


def _check_sort(words, ctx):
    _no_expansion(words, "sort")
    for w in words[1:]:
        a = w.value
        if a.startswith(("--output", "--compress-program", "-o")) or (
                a.startswith("-") and not a.startswith("--") and "o" in a[1:]):
            raise DeniedError("bash", "`sort -o` writes a file")


def _check_uniq(words, ctx):
    _no_expansion(words, "uniq")
    if len([w for w in words[1:] if not w.value.startswith("-")]) > 1:
        raise DeniedError("bash", "`uniq IN OUT` writes a file")


def _check_tree(words, ctx):
    _no_expansion(words, "tree")
    if any(w.value == "-o" or w.value.startswith("-o") for w in words[1:]):
        raise DeniedError("bash", "`tree -o` writes a file")


def _check_rg(words, ctx):
    _no_expansion(words, "rg")
    if any(w.value == "--pre" or w.value.startswith("--pre=") for w in words[1:]):
        raise DeniedError("bash", "`rg --pre` runs a command")


def _check_file(words, ctx):
    if any(w.value in ("-C", "--compile") for w in words[1:]):
        raise DeniedError("bash", "`file -C` writes a file")


_FIND_WRITES = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0",
                "-fprintf", "-fls"}


def _check_find(words, ctx):
    _no_expansion(words, "find")
    bad = sorted({w.value for w in words[1:]} & _FIND_WRITES)
    if bad:
        raise DeniedError("bash", f"`find {bad[0]}` runs a command or writes")


def sed_script_ok(script: str) -> bool:
    """False when a sed script could write (`w`/`W` command or flag) or execute (`e`)."""
    residue, i, n = [], 0, len(script)
    while i < n:
        c = script[i]
        if c in "sy" and i + 1 < n and not script[i + 1].isalnum() and script[i + 1] not in " \t\n\\;}":
            delim = script[i + 1]
            i += 2
            for _ in range(2):
                while i < n and script[i] != delim:
                    i += 2 if script[i] == "\\" else 1
                if i >= n:
                    return False
                i += 1
            flags = ""
            while c == "s" and i < n and script[i].isalnum():
                flags += script[i]
                i += 1
            if any(f in flags for f in "wWe"):
                return False
            continue
        if c == "/" or (c == "\\" and i + 1 < n):
            delim = "/" if c == "/" else script[i + 1]
            i += 1 if c == "/" else 2
            while i < n and script[i] != delim:
                i += 2 if script[i] == "\\" else 1
            if i >= n:
                return False
            i += 1
            continue
        residue.append(c)
        i += 1
    return not any(ch in "wWe" for ch in residue)


def _check_sed(words, ctx):
    _no_expansion(words, "sed")
    scripts, i, args = [], 1, words
    positional_seen = False
    while i < len(args):
        a = args[i].value
        if a in ("-e", "--expression"):
            if i + 1 >= len(args):
                raise DeniedError("bash", "`sed -e` without a script")
            scripts.append(args[i + 1].value)
            i += 2
            continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("--in-place") or a.startswith(("--file", "--follow")):
            raise DeniedError("bash", f"`sed {a}` edits or reads a script file")
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            if "i" in a or "f" in a:
                raise DeniedError("bash", f"`sed {a}` edits in place or reads a script file")
            if a.endswith("e"):
                if i + 1 >= len(args):
                    raise DeniedError("bash", "`sed -e` without a script")
                scripts.append(args[i + 1].value)
                i += 2
                continue
        elif not a.startswith("-") and not scripts and not positional_seen:
            scripts.append(a)
            positional_seen = True
        i += 1
    for s in scripts:
        if not sed_script_ok(s):
            raise DeniedError("bash", "sed script with a `w`, `W` or `e` command can write or execute")


_AWK_BAD = re.compile(r">|\||\bsystem\b|\bgetline\b|@")


def _check_awk(words, ctx):
    _no_expansion(words, "awk")
    for w in words[1:]:
        a = w.value
        if a in ("-f", "-i", "--file", "--include", "-E", "--exec", "-l", "--load") or a.startswith(
                ("--file=", "--include=", "--exec=", "--load=")):
            raise DeniedError("bash", f"`awk {a}` loads program code from a file")
        if not a.startswith("-") and _AWK_BAD.search(a):
            raise DeniedError("bash", "awk program with `>`, `|`, system() or getline can write or execute")


_GIT_READ = {"status", "diff", "log", "show", "grep", "ls-files", "ls-tree", "rev-parse", "blame",
             "cat-file", "describe", "shortlog", "merge-base", "rev-list", "show-ref", "name-rev",
             "diff-tree", "diff-files", "diff-index", "for-each-ref", "version", "whatchanged"}


def _check_git(words, ctx):
    _no_expansion(words, "git")
    args = [w.value for w in words[1:]]
    while args and args[0].startswith("-"):
        if args[0] == "-C" and len(args) > 1:
            args = args[2:]
        elif args[0] in ("--no-pager", "-P"):
            args = args[1:]
        else:
            raise DeniedError("bash", f"`git {args[0]}` global option is not allowed")
    if not args or args[0] not in _GIT_READ:
        sub = args[0] if args else ""
        raise DeniedError("bash", f"`git {sub}` is not a read-only git command")
    for a in args[1:]:
        if a.startswith("--output") or a in ("-O", "--open-files-in-pager", "--ext-diff") or (
                args[0] == "grep" and a.startswith("-O")):
            raise DeniedError("bash", f"`git {args[0]} {a}` writes a file or runs a command")


_PYTEST_DENY = ("--junitxml", "--junit-xml", "--basetemp", "--result-log", "--resultlog",
                "--report-log", "--cov-report", "--html", "--json-report-file", "--pastebin",
                "--config-file", "--rootdir", "--confcutdir", "--override-ini", "--debug",
                "--log-file", "--import-mode", "--plugins")
_PYTEST_VALUE_SHORT = set("kmnWr")


def _check_pytest_args(args: list[str]) -> None:
    for a in args:
        if a.startswith("--"):
            if any(a == d or a.startswith(d + "=") for d in _PYTEST_DENY):
                raise DeniedError("bash", f"pytest `{a}` writes a file or loads code from a path")
            continue
        if a.startswith("-") and len(a) > 1:
            for ch in a[1:]:
                if ch in "pco":
                    raise DeniedError("bash", f"pytest `-{ch}` loads plugins/config or overrides ini options")
                if ch in _PYTEST_VALUE_SHORT:
                    break


def _check_pytest(words, ctx):
    _no_expansion(words, "pytest")
    _check_pytest_args([w.value for w in words[1:]])


def _check_ruff_args(args: list[str]) -> None:
    if not args:
        raise DeniedError("bash", "bare `ruff`")
    sub, rest = args[0], args[1:]
    if sub in ("--version", "-V", "version", "rule", "linter", "--help", "help"):
        return
    bad = ("--fix", "--unsafe-fixes", "--add-noqa", "--output-file", "-o", "--cache-dir",
           "--fix-only", "--watch", "-w", "--config")
    if sub == "check":
        for a in rest:
            if any(a == b or a.startswith(b + "=") for b in bad):
                raise DeniedError("bash", f"`ruff check {a}` writes files")
        return
    if sub == "format" and ("--check" in rest or "--diff" in rest):
        if any(a in ("--config", "--cache-dir") or a.startswith(("--config=", "--cache-dir=")) for a in rest):
            raise DeniedError("bash", "`ruff format` option writes or loads config")
        return
    raise DeniedError("bash", f"`ruff {sub}` may write files (only `check` and `format --check/--diff`)")


def _check_ruff(words, ctx):
    _no_expansion(words, "ruff")
    _check_ruff_args([w.value for w in words[1:]])


# Write, execute or escape indicators in `python -c` code. A heuristic: it catches the
# direct forms (open(..., 'w'), Path.write_text, shutil, subprocess, os.remove, exec)
# and refuses the usual obfuscations (getattr, __import__, __builtins__, importlib).
_PY_BAD = re.compile(
    r"write|\bremove\b|unlink|\brmdir\b|rmtree|\brename\b|\bshutil\b|"
    r"\bsubprocess\b|\bsystem\b|\bpopen\b|\bspawn|\bexec|\beval\b|\bcompile\b|__import__|"
    r"\bimportlib\b|__builtins__|__dict__|\bgetattr\b|\bsetattr\b|\bglobals\b|\blocals\b|"
    r"\bvars\b|\bchmod\b|\bchown\b|\bmk\w*|link|\btruncate\b|"
    r"\btouch\b|\bctypes\b|\bsocket\b|\bsqlite3\b|\btempfile\b|\bextract|\bsave|\bto_csv\b|"
    r"\bto_json\b|\bto_parquet\b|\bto_pickle\b|\bdump\b|\bos\.open\b|\bos\.replace\b|\bpty\b|"
    r"\bbreakpoint\b|\bfdopen\b|\bmmap\b|\bwebbrowser\b|\bcode\b|\bpdb\b|\brunpy\b|\bsite\b"
)
# Builtin open(): read-only only with a literal name and a literal r/b/t mode.
_PY_OPEN = re.compile(r"(?<![\w.])open\s*\(")
_PY_OPEN_RO = re.compile(r"""(?<![\w.])open\s*\(\s*['"][^'"]*['"]\s*(?:,\s*['"][rbt]*['"]\s*)?\)""")
# Method .open() (Path.open, io.open, codecs.open): read-only only with no args or a literal r/b/t mode.
_PY_MOPEN = re.compile(r"\.\s*open\s*\(")
_PY_MOPEN_RO = re.compile(r"""\.\s*open\s*\(\s*(?:['"][rbt]*['"]\s*)?\)""")


def python_code_ok(code: str) -> bool:
    """False when `python -c` code could write, execute or hide what it calls (heuristic)."""
    if _PY_BAD.search(code):
        return False
    if re.search(r"\bPath\b|pathlib", code) and re.search(r"\.replace\s*\(", code):
        return False  # Path.replace renames; str.replace alone is fine
    if len(_PY_OPEN.findall(code)) != len(_PY_OPEN_RO.findall(code)):
        return False
    return len(_PY_MOPEN.findall(code)) == len(_PY_MOPEN_RO.findall(code))


def _check_python(words, ctx):
    _no_expansion(words, "python")
    args = [w.value for w in words[1:]]
    while args and args[0] in ("-B", "-u", "-I", "-P", "-E", "-s", "-S", "-O", "-OO", "-q", "-b", "-bb"):
        args = args[1:]
    if not args:
        raise DeniedError("bash", "`python` reading its program from stdin cannot be checked")
    first = args[0]
    if first in ("--version", "-V", "-VV"):
        return
    if first == "-m" and len(args) > 1:
        mod, rest = args[1], args[2:]
        if mod == "pytest":
            return _check_pytest_args(rest)
        if mod == "ruff":
            return _check_ruff_args(rest)
        if mod == "pip" and rest and rest[0] in ("list", "show", "freeze", "--version"):
            return
        raise DeniedError("bash", f"`python -m {mod}` is not an allowed module (pytest, ruff, pip list/show)")
    if first == "-c" and len(args) > 1:
        if python_code_ok(args[1]):
            return
        raise DeniedError("bash", "`python -c` code writes files, runs commands or hides what it calls")
    if not first.startswith("-") and _is_allowed_script(first, ctx):
        return  # a repo script the policy names (bash.allowed_scripts), e.g. scripts/export_openapi.py
    raise DeniedError("bash", "running a Python script is not allowed (only -m pytest/ruff, read-only -c "
                      f"and {ctx.allowed_scripts})")


def _check_npm(words, ctx):
    _no_expansion(words, "npm")
    args = [w.value for w in words[1:]]
    for a in args:
        if a.startswith(("--prefix", "--userconfig", "--globalconfig", "--cache", "--script-shell",
                         "--node-options", "--workspace", "-w", "--ignore-scripts=false")):
            raise DeniedError("bash", f"npm option `{a}` is not allowed")
        if any(a == d or a.startswith(d + "=") for d in _NG_DENY):  # forwarded to ng after `--`
            raise DeniedError("bash", f"`npm ... {a}` writes files or loads config from a path")
    if not args:
        raise DeniedError("bash", "bare `npm`")
    if args[0] in ("--version", "-v", "ls", "list"):
        return
    if args[0] in ("test", "t", "tst"):
        return
    if args[0] in ("run", "run-script") and len(args) > 1 and args[1] in ctx.npm_scripts:
        return
    raise DeniedError("bash", f"`npm {args[0]}` is not allowed (npm test and `npm run` of bash.npm_scripts; installs change lockfiles)")


_NG_DENY = ("--output-path", "--outputPath", "--fix", "--output-file", "--outputFile",
            "--karma-config", "--karmaConfig")


def _check_ng_args(args: list[str]) -> None:
    if not args:
        raise DeniedError("bash", "bare `ng`")
    if args[0] in ("version", "v", "--version"):
        return
    if args[0] not in ("test", "t", "lint", "build", "b"):
        raise DeniedError("bash", f"`ng {args[0]}` is not allowed (test, lint, build)")
    for a in args[1:]:
        if any(a == d or a.startswith(d + "=") for d in _NG_DENY):
            raise DeniedError("bash", f"`ng {args[0]} {a}` writes files or loads config from a path")


def _check_ng(words, ctx):
    _no_expansion(words, "ng")
    _check_ng_args([w.value for w in words[1:]])


def _check_npx(words, ctx):
    _no_expansion(words, "npx")
    args = [w.value for w in words[1:]]
    if not args or args[0] != "ng":
        raise DeniedError("bash", "`npx` may only run `ng`")
    _check_ng_args(args[1:])


def _check_node(words, ctx):
    if [w.value for w in words[1:]] not in (["--version"], ["-v"]):
        raise DeniedError("bash", "`node` may only print its version")


def _is_allowed_script(path: str, ctx: BashContext) -> bool:
    if ctx.saw_cd or not ctx.repo_root:
        return False
    full = os.path.realpath(os.path.join(ctx.cwd, path))
    return any(full == os.path.realpath(os.path.join(ctx.repo_root, s)) for s in ctx.allowed_scripts)


def _check_shell(words, ctx):
    _no_expansion(words, "bash")
    args = [w.value for w in words[1:]]
    if len(args) == 1 and _is_allowed_script(args[0], ctx):
        return
    raise DeniedError("bash", f"`{words[0].value}` may only run {ctx.allowed_scripts} from the repo root")


_CHECKERS = {
    "sort": _check_sort, "uniq": _check_uniq, "tree": _check_tree, "rg": _check_rg,
    "file": _check_file, "find": _check_find, "sed": _check_sed, "awk": _check_awk,
    "gawk": _check_awk, "mawk": _check_awk, "nawk": _check_awk, "git": _check_git,
    "pytest": _check_pytest, "py.test": _check_pytest, "ruff": _check_ruff,
    "npm": _check_npm, "npx": _check_npx, "ng": _check_ng, "node": _check_node,
    "bash": _check_shell, "sh": _check_shell, "date": lambda w, c: _check_readonly("date", w),
}


# ── the decision ──────────────────────────────────────────────────────────

NO_WORKAROUND = (
    "Do NOT work around this with Bash, a shell redirect, or an MCP tool. Stop and report "
    "the change you would make (with a task card: comment BLOCKED on the issue)."
)


def evaluate(payload, env: dict, policy: dict) -> tuple[str, str, str]:
    """Return ("allow" | "deny", ring, reason). Raises nothing: any surprise is a deny."""
    try:
        return _evaluate(payload, env, policy)
    except DeniedError as d:
        return "deny", d.ring, d.reason
    except GateError as exc:
        return "deny", "input", f"{exc}. Failing closed."
    except Exception as exc:  # a crashing gate must not wave writes through
        return "deny", "error", f"write gate error ({type(exc).__name__}: {exc}). Failing closed."


def _evaluate(payload, env: dict, policy: dict) -> tuple[str, str, str]:
    if not isinstance(payload, dict):
        raise GateError("hook input is not a JSON object")
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        raise GateError("hook input has no tool_name")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        raise GateError("tool_input is not an object")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        cwd = os.getcwd()
    rings = policy["rings"]
    card = load_card(env.get("TAZ_CARD")) if rings.get("card", True) else None

    if tool == "Bash":
        if not rings.get("bash", True):
            return "allow", "bash-off", ""
        cmd = tool_input.get("command")
        if not isinstance(cmd, str):
            raise GateError("Bash tool_input has no command string")
        repo = card["repo_root"] if card else find_repo_root(cwd)
        check_bash(cmd, BashContext(policy, cwd, repo))
        return "allow", "bash-readonly", ""

    if tool not in policy["mutating_tools"]:
        return "allow", "non-mutating", ""

    path = tool_input.get("notebook_path") if tool == "NotebookEdit" else tool_input.get("file_path")
    if path is None and tool == "NotebookEdit":
        path = tool_input.get("file_path")
    if not isinstance(path, str) or not path or "\x00" in path:
        raise GateError(f"{tool} input has no usable file path")
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    forms = path_forms(full)
    real = os.path.realpath(full)
    wdirs = work_dirs(forms, cwd, card)

    if is_exempt(forms, exempt_roots(policy), wdirs):
        return "allow", "exempt-path", ""

    # Ring 5 -- protected paths, relative to every work dir the target is in.
    if rings.get("protected", True):
        protected = list(policy["protected_paths"]) + list(FIXED_PROTECTED)
        oracles = card["oracle_tests"] if card else []
        for f in forms:
            for w in wdirs:
                if not under(f, w) or f == w:
                    continue
                rel = os.path.relpath(f, w)
                if matches_any(rel, oracles):
                    raise DeniedError("protected", f"`{rel}` is an oracle test from the task card. "
                                 "The card's tests define done; they are never edited. " + NO_WORKAROUND)
                if matches_any(rel, protected, ignore_case=True):
                    raise DeniedError("protected", f"`{rel}` is a protected path (policy protected_paths). "
                                 + NO_WORKAROUND)

    # Ring 4 -- the task card's allowed paths. Applies to prose too.
    if card is not None:
        roots = path_forms(card["repo_root"])
        for f in forms:
            inside = [r for r in roots if under(f, r) and f != r]
            if not inside:
                raise DeniedError("card", f"`{f}` is outside the task's repository {card['repo_root']}. "
                             + NO_WORKAROUND)
            if not any(matches_any(os.path.relpath(f, r), card["allowed_paths"]) for r in inside):
                raise DeniedError("card", f"`{os.path.relpath(f, inside[0])}` is not in the task card's "
                             f"allowed_paths {card['allowed_paths']}. " + NO_WORKAROUND)

    if not any(is_code_path(f, policy) for f in forms):
        return "allow", "prose", ""

    served, compact_idx, last_read = scan_transcript(payload.get("transcript_path"))
    requested = env.get("ANTHROPIC_MODEL", "")
    model = served or requested

    # Ring 1 -- capability. Unknown model on a gateway fails closed; a session with no
    # gateway at all is a normal Anthropic session.
    if rings.get("capability", True):
        if not model:
            if env.get("ANTHROPIC_BASE_URL"):
                raise DeniedError("capability", "Write gate could not determine which model is serving "
                             "this session, and ANTHROPIC_BASE_URL points at a gateway. Failing closed.")
        elif not any(tag in model.lower() for tag in policy["write_allowlist"]):
            raise DeniedError("capability", f"This session is served by `{model}`, which is not on the "
                         f"code-write allowlist ({', '.join(policy['write_allowlist']) or 'empty'}). "
                         + NO_WORKAROUND + " Without a task card you may write a unified diff to "
                         ".agent-patches/<name>.patch instead.")

    # Ring 2 -- whole-file rewrite of a substantial tracked file.
    if rings.get("whole_file", True) and tool == "Write" and os.path.isfile(real):
        try:
            with open(real, encoding="utf-8", errors="replace") as fh:
                existing = sum(1 for _ in fh)
        except OSError:
            existing = 0
        if existing > policy["whole_file_max_lines"] and git_tracked(real):
            raise DeniedError("whole-file", f"`{os.path.basename(real)}` is a tracked file of {existing} "
                         "lines and this is a whole-file Write, which replaces every line not in your "
                         "output. That is how seed.py lost all of its imports on 2026-08-19. Use Edit.")

    # Ring 3 -- post-compaction amnesia.
    if (rings.get("amnesia", True) and compact_idx >= 0 and os.path.isfile(real)
            and last_read.get(real, -1) < compact_idx):
            raise DeniedError("amnesia", f"The context was compacted since `{os.path.basename(real)}` was "
                         "last read, so its contents are no longer reliably in view. Read the file "
                         "again before editing it.")

    return "allow", "passed", ""


# ── hook entry point ──────────────────────────────────────────────────────


def log(path: str | None, entry: dict) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # telemetry never decides
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = dict(os.environ)
    payload = policy = None
    try:
        if argv and (len(argv) != 2 or argv[0] != "--policy"):
            raise GateError("usage: write_gate.py [--policy PATH]")
        explicit = argv[1] if argv else env.get("TAZ_GATE_POLICY") or None
        policy = load_policy(explicit or os.path.join(HOOK_DIR, "policy.json"), bool(explicit))
    except GateError as exc:
        decision, ring, reason = "deny", "policy", f"{exc}. Failing closed."
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        decision, ring, reason = "deny", "input", "Write gate could not parse its hook input. Failing closed."
    else:
        if policy is not None:
            decision, ring, reason = evaluate(payload, env, policy)
    log_path = env.get("TAZ_GATE_LOG") or (policy or {}).get("log_path")
    p = payload if isinstance(payload, dict) else {}
    ti = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
    log(log_path, {
        "ts": int(time.time()), "session": p.get("session_id"), "tool": p.get("tool_name"),
        "path": ti.get("file_path") or ti.get("notebook_path"),
        "command": ti.get("command") if isinstance(ti.get("command"), str) else None,
        "card": env.get("TAZ_CARD"), "decision": decision, "ring": ring, "reason": reason,
    })
    if decision == "deny":
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"[write gate, ring {ring}] {reason}",
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
