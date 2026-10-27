#!/usr/bin/env python3
"""taz-guard: check a TAZ pull request against its task card (layer 5 of the TAZ plan).

The CI job (.github/workflows/taz-guard.yml) runs this from the BASE branch's checkout and
reads the PR head only as git objects: nothing from the PR is checked out or executed.

    python scripts/taz_guard.py --event "$GITHUB_EVENT_PATH" --head refs/taz/head
    python scripts/taz_guard.py --base origin/main --head HEAD --card card.json   # locally

The card comes from the GitHub issue the PR body links (`taz-card: #123` or the issue URL).
That issue must carry the `taz-ready` label and be written by a trusted author, because
the TAZ bot cannot label issues; a card the PR author wrote is not a card. A card block
in the PR body itself is accepted only when it equals the issue's card, or with
--allow-inline-card (local use). Card format, in a fenced block tagged `taz-card`:

    {"allowed_paths": ["control-plane/web/src/app/features/x/**"],
     "oracle_tests": ["control-plane/web/src/app/features/x/x.component.spec.ts"],
     "diff_budget": 300, "base_ref": "taz-base/123"}

Checks, each reported separately:
  scope       every changed path matches allowed_paths and no protected path
              (tools/taz/hooks/policy.json `protected_paths`)
  budget      added + deleted lines <= diff_budget (default 300); no binary files
  oracle      oracle test files are byte-identical to base_ref (or untouched, without one)
  suppression no new skip / xfail / noqa / type: ignore / eslint-disable (and friends)
  baseline    no .dod-*baseline value raised (lowering is the ratchet working)
  lockfile    no lockfile changed
  mode        no symlinks or submodules added

Exit 0 when all pass, 1 when any fails, 2 on a usage or input error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "tools" / "taz" / "hooks" / "policy.json"
DEFAULT_BUDGET = 300
READY_LABEL = "taz-ready"

CARD_BLOCK = re.compile(r"```taz-card[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
CARD_LABELED = re.compile(r"taz-card:[ \t]*\r?\n[ \t]*```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
CARD_LINK = re.compile(
    r"taz-card:[ \t]*(?:#(\d+)|https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+))\b", re.I)

# Lines that switch a check off. Matched on ADDED lines of code files; a line that was
# only moved (the same text removed in the same file) does not count.
SUPPRESSIONS = [
    ("pytest skip/xfail", re.compile(
        r"@pytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.(?:skip|xfail|importorskip)\s*\(")),
    ("unittest skip", re.compile(r"\bunittest\.skip|@skip(?:If|Unless)?\b|\.skipTest\s*\(")),
    ("noqa", re.compile(r"#\s*noqa\b|#\s*ruff\s*:\s*noqa", re.I)),
    ("type: ignore", re.compile(r"#\s*type\s*:\s*ignore", re.I)),
    ("pragma: no cover", re.compile(r"#\s*pragma\s*:\s*no\s*cover", re.I)),
    ("eslint-disable", re.compile(r"eslint-disable")),
    ("ts-ignore", re.compile(r"@ts-(?:ignore|nocheck|expect-error)")),
    ("jasmine skip/focus", re.compile(r"(?<![\w.])(?:xit|xdescribe|fit|fdescribe|pending)\s*\(")),
]
PROSE_SUFFIXES = {".md", ".markdown", ".rst", ".txt", ".adoc"}


class GuardInputError(Exception):
    pass


# ── globs (same dialect as tools/taz/hooks/write_gate.py) ─────────────────


def glob_to_regex(glob: str, ignore_case: bool = False) -> re.Pattern:
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


# ── card ──────────────────────────────────────────────────────────────────


def parse_card_blocks(text: str) -> list[dict]:
    cards = []
    for m in [*CARD_BLOCK.finditer(text or ""), *CARD_LABELED.finditer(text or "")]:
        try:
            cards.append(json.loads(m.group(1)))
        except ValueError as exc:
            raise GuardInputError(f"taz-card block is not valid JSON: {exc}") from None
    return cards


def validate_card(card) -> dict:
    if not isinstance(card, dict):
        raise GuardInputError("taz-card must be a JSON object")
    allowed = card.get("allowed_paths")
    if not isinstance(allowed, list) or not allowed or not all(isinstance(g, str) and g for g in allowed):
        raise GuardInputError("taz-card needs a non-empty allowed_paths list")
    oracles = card.get("oracle_tests", [])
    if not isinstance(oracles, list) or not all(isinstance(g, str) and g for g in oracles):
        raise GuardInputError("taz-card oracle_tests must be a list of paths")
    budget = card.get("diff_budget", DEFAULT_BUDGET)
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        raise GuardInputError("taz-card diff_budget must be a positive integer")
    base_ref = card.get("base_ref")
    if base_ref is not None and (not isinstance(base_ref, str) or not re.fullmatch(r"[\w./-]+", base_ref)
                                 or ".." in base_ref):
        raise GuardInputError("taz-card base_ref must be a plain ref name")
    return {"allowed_paths": allowed, "oracle_tests": oracles, "diff_budget": budget, "base_ref": base_ref}


def linked_issue(pr_body: str, repo: str) -> int | None:
    links = list(CARD_LINK.finditer(pr_body or ""))
    if not links:
        return None
    numbers = set()
    for m in links:
        if m.group(1):
            numbers.add(int(m.group(1)))
        else:
            if m.group(2).lower() != repo.lower():
                raise GuardInputError(f"taz-card links an issue in another repository ({m.group(2)})")
            numbers.add(int(m.group(3)))
    if len(numbers) != 1:
        raise GuardInputError(f"the PR body links more than one taz-card issue: {sorted(numbers)}")
    return numbers.pop()


def fetch_issue(repo: str, number: int, token: str | None) -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues/{number}")
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_card(pr_body: str, repo: str, pr_author: str | None, trusted: set[str],
                 allow_inline: bool, fetch=fetch_issue, token: str | None = None) -> dict:
    """The authoritative card for this PR, or GuardInputError saying why there is none."""
    inline = parse_card_blocks(pr_body)
    if len(inline) > 1:
        raise GuardInputError("the PR body has more than one taz-card block")
    number = linked_issue(pr_body, repo)
    if number is None:
        if inline and allow_inline:
            return validate_card(inline[0])
        raise GuardInputError("no task card: link a taz-ready issue in the PR body (`taz-card: #N`)")
    issue = fetch(repo, number, token)
    if issue.get("pull_request"):
        raise GuardInputError(f"#{number} is a pull request, not a task card issue")
    labels = {(lbl.get("name") if isinstance(lbl, dict) else str(lbl)) for lbl in issue.get("labels", [])}
    if READY_LABEL not in labels:
        raise GuardInputError(f"issue #{number} does not carry the `{READY_LABEL}` label")
    author = ((issue.get("user") or {}).get("login") or "").lower()
    if author not in trusted:
        raise GuardInputError(f"issue #{number} was written by `{author}`, not a trusted card author "
                              f"({', '.join(sorted(trusted))})")
    if pr_author and author == pr_author.lower() and pr_author.lower() not in trusted:
        raise GuardInputError("the card's author opened this PR")
    cards = parse_card_blocks(issue.get("body") or "")
    if len(cards) != 1:
        raise GuardInputError(f"issue #{number} must hold exactly one taz-card block (found {len(cards)})")
    card = validate_card(cards[0])
    if inline and validate_card(inline[0]) != card:
        raise GuardInputError(f"the card in the PR body differs from the card on issue #{number}")
    card["issue"] = number
    return card


# ── git ───────────────────────────────────────────────────────────────────


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=off", *args],
                          capture_output=True, check=check)


def rev(repo: Path, ref: str) -> str:
    r = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if r.returncode != 0:
        raise GuardInputError(f"cannot resolve `{ref}`")
    return r.stdout.decode().strip()


def blob(repo: Path, commit: str, path: str) -> str | None:
    r = git(repo, "rev-parse", "--verify", "--quiet", f"{commit}:{path}", check=False)
    return r.stdout.decode().strip() if r.returncode == 0 else None


def tree_files(repo: Path, commit: str) -> list[str]:
    out = git(repo, "ls-tree", "-r", "-z", "--name-only", commit).stdout.decode("utf-8", "replace")
    return [p for p in out.split("\0") if p]


def show(repo: Path, commit: str, path: str) -> str | None:
    r = git(repo, "show", f"{commit}:{path}", check=False)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


@dataclass
class Change:
    path: str
    status: str
    old_mode: str
    new_mode: str
    added: int | None = None    # None: binary
    deleted: int | None = None


def changes(repo: Path, base: str, head: str) -> list[Change]:
    raw = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--raw", "-z",
              base, head).stdout.decode("utf-8", "replace").split("\0")
    out: dict[str, Change] = {}
    i = 0
    while i + 1 < len(raw):
        meta, path = raw[i], raw[i + 1]
        i += 2
        if not meta.startswith(":"):
            continue
        old_mode, new_mode, _o, _n, status = meta[1:].split(" ")[:5]
        out[path] = Change(path, status, old_mode, new_mode)
    num = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z",
              base, head).stdout.decode("utf-8", "replace").split("\0")
    for entry in num:
        parts = entry.split("\t", 2)
        if len(parts) == 3 and parts[2] in out:
            c = out[parts[2]]
            if parts[0] != "-":
                c.added, c.deleted = int(parts[0]), int(parts[1])
    return list(out.values())


def added_removed_lines(repo: Path, base: str, head: str, path: str) -> tuple[list[str], list[str]]:
    diff = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "-U0", base, head,
               "--", path).stdout.decode("utf-8", "replace")
    added, removed = [], []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


# ── checks ────────────────────────────────────────────────────────────────


@dataclass
class Report:
    failures: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, check: str, msg: str) -> None:
        self.failures.append((check, msg))


def baseline_values(text: str | None) -> dict[str, float] | None:
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        return {"": data}
    if isinstance(data, dict) and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                      for v in data.values()):
        return dict(data)
    return None


def check(repo: Path, base: str, head: str, card: dict, policy: dict) -> Report:
    rep = Report()
    base_c, head_c = rev(repo, base), rev(repo, head)
    mb = git(repo, "merge-base", base_c, head_c, check=False).stdout.decode().strip()
    if not mb:
        raise GuardInputError("base and head share no history")
    changed = changes(repo, mb, head_c)
    protected = policy.get("protected_paths", [])
    lockfiles = policy.get("lockfile_globs", [])
    counted: list[Change] = []

    # Oracle files: identical to base_ref, or untouched when the card names no base_ref.
    oracle_paths: set[str] = set()
    globs = card["oracle_tests"]
    if globs:
        ref_c = rev(repo, card["base_ref"]) if card.get("base_ref") else mb
        in_ref = [p for p in tree_files(repo, ref_c) if matches_any(p, globs)]
        if not in_ref:
            rep.fail("oracle", f"no oracle test file matching {globs} exists at "
                     f"{card.get('base_ref') or 'the merge base'}")
        in_head = {p for p in tree_files(repo, head_c) if matches_any(p, globs)}
        for p in sorted(set(in_ref) | in_head):
            oracle_paths.add(p)
            if blob(repo, head_c, p) != blob(repo, ref_c, p):
                rep.fail("oracle", f"`{p}` differs from {card.get('base_ref') or 'the merge base'}: "
                         "oracle tests are never edited")

    for c in changed:
        p = c.path
        if c.new_mode in ("120000", "160000") or (c.status == "T"):
            rep.fail("mode", f"`{p}`: symlinks, submodules and type changes are not allowed")
        elif c.old_mode == "000000" and c.new_mode not in ("100644", "000000"):
            rep.fail("mode", f"`{p}` is added with mode {c.new_mode}: new files must be 100644")
        elif "000000" not in (c.old_mode, c.new_mode) and c.old_mode != c.new_mode:
            rep.fail("mode", f"`{p}` changes mode {c.old_mode} -> {c.new_mode}")
        if matches_any(p, lockfiles, ignore_case=True):
            rep.fail("lockfile", f"`{p}` is a lockfile")
            continue
        if p in oracle_paths:
            continue  # judged above, against base_ref
        if matches_any(os.path.basename(p), [".dod-*baseline"]):
            old = baseline_values(show(repo, mb, p))
            new = baseline_values(show(repo, head_c, p))
            if old is None or new is None:
                rep.fail("baseline", f"`{p}` was added, deleted or is unreadable")
            else:
                raised = sorted(k or "value" for k in new if k not in old or new[k] > old[k])
                if raised:
                    rep.fail("baseline", f"`{p}` raised: {', '.join(raised)}")
                else:
                    rep.notes.append(f"{p}: lowered (the ratchet working)")
            continue
        if matches_any(p, protected, ignore_case=True):
            rep.fail("scope", f"`{p}` is a protected path")
        elif not matches_any(p, card["allowed_paths"]):
            rep.fail("scope", f"`{p}` is outside allowed_paths {card['allowed_paths']}")
        counted.append(c)

    total = 0
    for c in counted:
        if c.added is None:
            rep.fail("budget", f"`{c.path}` is a binary file")
            continue
        total += c.added + c.deleted
    if total > card["diff_budget"]:
        rep.fail("budget", f"{total} changed lines exceed the card's budget of {card['diff_budget']}")
    rep.notes.append(f"{total} changed lines counted against a budget of {card['diff_budget']}")

    for c in changed:
        if c.added is None or c.path in oracle_paths or Path(c.path).suffix.lower() in PROSE_SUFFIXES:
            continue  # oracle files are judged by hash above; prose may mention these words
        added, removed = added_removed_lines(repo, mb, head_c, c.path)
        removed_set = {r.strip() for r in removed}
        for line in added:
            if line.strip() in removed_set:
                continue
            for name, rx in SUPPRESSIONS:
                if rx.search(line):
                    rep.fail("suppression", f"`{c.path}` adds a {name} line: {line.strip()[:120]}")
    return rep


# ── CLI ───────────────────────────────────────────────────────────────────


def load_policy(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise GuardInputError("policy must be a JSON object")
    return data


def emit(rep: Report, card: dict) -> None:
    lines = [f"## taz-guard: {'FAIL' if rep.failures else 'PASS'}", ""]
    if card.get("issue"):
        lines.append(f"Card: #{card['issue']}")
    for check_name, msg in rep.failures:
        print(f"::error title=taz-guard {check_name}::{msg}")
        lines.append(f"- **{check_name}**: {msg}")
    for note in rep.notes:
        print(f"note: {note}")
        lines.append(f"- {note}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print("taz-guard: " + ("FAIL" if rep.failures else "PASS"))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo-dir", type=Path, default=Path.cwd())
    ap.add_argument("--base", help="base ref (default: the event's pull_request.base.sha)")
    ap.add_argument("--head", required=True, help="PR head ref, already fetched")
    ap.add_argument("--card", type=Path, help="card JSON file (local use)")
    ap.add_argument("--event", type=Path, help="GitHub event JSON ($GITHUB_EVENT_PATH)")
    ap.add_argument("--pr-body-file", type=Path, help="PR body as a file (local use)")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "mblanke/TrueNorth"))
    ap.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    ap.add_argument("--trusted-authors", default=os.environ.get("TAZ_CARD_AUTHORS", "mblanke"),
                    help="comma-separated GitHub logins whose issues are task cards")
    ap.add_argument("--allow-inline-card", action="store_true",
                    help="accept a card block in the PR body without an issue (never in CI)")
    return ap.parse_args(argv)


def main(argv=None, fetch=fetch_issue) -> int:
    args = parse_args(argv)
    try:
        policy = load_policy(args.policy)
        base, body, pr_author = args.base, "", None
        if args.event:
            event = json.loads(args.event.read_text(encoding="utf-8"))
            pr = event.get("pull_request") or {}
            body = pr.get("body") or ""
            pr_author = (pr.get("user") or {}).get("login")
            base = base or (pr.get("base") or {}).get("sha")
        if args.pr_body_file:
            body = args.pr_body_file.read_text(encoding="utf-8")
        if not base:
            raise GuardInputError("no base ref (--base or --event)")
        if args.card:
            card = validate_card(json.loads(args.card.read_text(encoding="utf-8")))
        else:
            trusted = {a.strip().lower() for a in args.trusted_authors.split(",") if a.strip()}
            card = resolve_card(body, args.repo, pr_author, trusted, args.allow_inline_card,
                                fetch=fetch, token=os.environ.get("GITHUB_TOKEN"))
        rep = check(args.repo_dir, base, args.head, card, policy)
    except (GuardInputError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"::error title=taz-guard input::{exc}")
        print("taz-guard: ERROR")
        return 2
    emit(rep, card)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
