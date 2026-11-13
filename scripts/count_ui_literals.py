#!/usr/bin/env python3
"""UI literal counter: user-facing hard-coded strings on the two critical journeys.

Turns the #436 literal inventory (TN10X-F6-literal-inventory) into a re-runnable
ratchet, same contract as scripts/mosa_check.py: a count above the recorded
baseline fails; a count below it lowers the baseline, so cleanup sticks.

Classes (per the #436 method):
  A - template text segments (inline templates; every journey component has one)
  B - literal attribute values (placeholder/title/aria-label/matTooltip/alt and
      their [attr.x]/[x] binding forms)
  C - UI-reaching TypeScript literals (outside template and styles spans)

The full policy, including the judgement calls carried over from #436, is in
POLICY below and in the module docstring section "Classification policy".

    python scripts/count_ui_literals.py                 # counts for the journeys
    python scripts/count_ui_literals.py --git eec53906  # counts at a past commit
    python scripts/count_ui_literals.py --check         # ratchet vs the baseline
    python scripts/count_ui_literals.py --report        # per-file lines too

--check and --git are mutually exclusive: a past-state count must never move
the live baseline.

Stdlib only; safe under ``python3 -I``. Default mode reads the working tree;
``--git REV`` reads blobs with ``git show`` so past figures stay reproducible.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_APP = "control-plane/web/src/app"
BASELINE_FILE = ROOT / ".dod-ui-literals-baseline"

# The two critical journeys fixed by the #436 inventory, plus the shared
# components embedded in them. console-state.ts has no template (C only).
STUDENT_JOURNEY = (
    "features/login/login.component.ts",
    "features/classes/my-classes.component.ts",
    "features/exercise-detail/exercise-detail.component.ts",
    "features/exercise-detail/detection-panel.component.ts",
    "features/labs/lab-page.component.ts",
    "shared/components/vm-console/tn-vm-console.component.ts",
    "shared/components/vm-console/console-state.ts",
)
INSTRUCTOR_JOURNEY = (
    "features/schedule/schedule.component.ts",
    "features/schedule/approval-queue.component.ts",
    "features/classes/classes.component.ts",
    "features/classes/roll-call-domains-card.component.ts",
    "features/classes/class-live.component.ts",
    "features/classes/class-closeout.component.ts",
    "features/scoring/scoring.component.ts",
)
# Embedded in both journeys (credential handout on lab-page and class-live).
SHARED_JOURNEY = (
    "shared/components/range-credentials/range-credentials.component.ts",
)
JOURNEYS = (
    ("student", STUDENT_JOURNEY),
    ("instructor", INSTRUCTOR_JOURNEY),
    ("shared", SHARED_JOURNEY),
)

# ---------------------------------------------------------------------------
# Classification policy
# ---------------------------------------------------------------------------
# Class A/B are mechanical (the #436 scanner behaviour, restated in code):
#   A: text nodes of the inline template. Tags, <mat-icon> and <style> bodies,
#      <!-- --> comments, @if/@else/@for/@switch/@case/@default/@empty headers
#      and @let declarations are skipped. {{...}} interpolations separate text;
#      quoted fragments before the first top-level | pipe count (date/number
#      format args after a pipe do not). Entities dropped, whitespace collapsed,
#      a segment must contain [A-Za-z].
#   B: regex over the raw template for static placeholder/title/aria-label/
#      matTooltip/alt values (single- or double-quoted) and quoted fragments
#      inside their binding forms.
# Class C extraction is mechanical (string/template literals outside template
# and styles spans, // and /* */ comments skipped, backticks split on ${...});
# what reaches the UI then needs the judgement calls #436 declared. They are
# encoded below as content/context rules, each traceable to a #436 decision.
#
# Known extraction limits (a new line that hits one makes the count drift,
# not crash — except the first):
#   * a quote character inside a regex literal (e.g. /['"]/) is read as a
#     string opener and raises ValueError — the scan fails loudly instead of
#     silently skipping a literal;
#   * a template literal nested inside ${...} contributes nothing (its text is
#     masked like a call argument). This DOES occur today: classes.component.ts
#     :112 and :114 nest `${x.email} (${x.reason})` in a .map callback — the
#     masked runs there are letterless, so no fragment is lost; a nested run
#     with letters would be dropped silently.
#
# Limits that existed earlier and were fixed (kept here so the next calibration
# knows the form is covered): single-quoted attribute values were invisible to
# the static-B scan until this fix round; detection-panel.component.ts:68
# `placeholder='process.name:"powershell.exe" AND ...'` was the one occurrence
# in the journey files and now counts as B.

# A C fragment counts when it matches PROSE (multi-word text) or one of the
# context rules; the table after it carries the single-word exceptions #436
# declared (label-map values, state words, the 'forfeited' seat key).
PROSE = re.compile(r"[A-Za-z]\s[A-Za-z]")

# Single-word C fragments the #436 inventory classified as user-facing.
# Everything not prose and not here (import specifiers, selectors, rxjs/role/
# permission keys, gsap eases, router urls, css calc()/media queries, ...) is
# not counted. Entries are exact cleaned fragments, case-sensitive ('Closed'
# the tn-vm-console stateLine value is kept; a lowercase status key is not).
# Each entry carries its source so the next calibration can re-verify it.
KEEP_WORDS = frozenset({
    # my-classes.component.ts — the 'forfeited' seat key (#436: counted)
    "forfeited",
    # tn-vm-console.component.ts — stateLine values rendered in the console header
    "Connecting", "Connected", "Ended", "Closed",
    # classes.component.ts — STATUS_LABEL / RANGE_STATUS_LABEL map values and
    # the saved/created snackbar fragments inside `Class ${c.name} ${...}`
    "Invited", "Removed", "Building", "Ready", "Failed",
    "saved", "created", "Class", "Added", "rejected:", "refused:",
    # exercise-detail.component.ts — fmtDuration() units: '${min} min',
    # '${d} day${...} (${h} h)', '${h} h', '${...}h ${...}m'. The day/h branch
    # reads as one text run between interpolations; the bare plural 's' is not
    # kept as a word (too generic a fragment class).
    "min", "day ( h)", "h",
    # class-closeout.component.ts — the results download's file name stem
    "-results.csv",
    # range-credentials.component.ts — title fallback: staff() ? 'Credentials'
    "Credentials",
    # class-closeout.component.ts — ATTENDANCE_LABEL / OUTCOME_LABEL values and
    # the graded-result line's single-word cases ('Passed')
    "Present", "Absent", "Excused", "Pass", "Fail", "Incomplete", "Passed",
    # schedule.component.ts — already-booked / booking snackbar / capacity panel
    # label words and label-function fallbacks (templateName/personName/stateNote)
    "Range", "Build", "Maintenance:", "Booked", "Scheduled:", "Cancelled:", "OK",
    # stateNote() suffixes — entries are post-_clean forms (leading space stripped)
    "vCPU", "RAM", "Disk", "Instructor", "template", "· draft", "· rejected",
    # schedule.component.ts — booking note `` for '${b.name}' · ${b.vm_count} VMs ``
    "for ' ' · VMs",
})


def _read(rel: str, git_rev: str | None) -> str:
    if git_rev is None:
        return (ROOT / WEB_APP / rel).read_text(encoding="utf-8")
    out = subprocess.run(
        ["git", "show", f"{git_rev}:{WEB_APP}/{rel}"],
        capture_output=True, cwd=ROOT, check=True,
    )
    return out.stdout.decode("utf-8")


def _find_backtick(src: str, start: int) -> int:
    """Index of the backtick closing the one that opens before *start*."""
    i = start
    while i < len(src):
        if src[i] == "\\":
            i += 2
        elif src[i] == "`":
            return i
        else:
            i += 1
    raise ValueError("unterminated template literal")


def template_span(src: str) -> tuple[int, int] | None:
    """(start, end) of the inline template body, excludes the backticks."""
    m = re.search(r"template\s*:\s*`", src)
    if not m:
        return None
    start = m.end()
    return start, _find_backtick(src, start)


def _skip_spans(src: str) -> list[tuple[int, int]]:
    """Spans C scanning must ignore: the template and the styles."""
    spans = []
    for m in re.finditer(r"(?:template|styles)\s*:\s*(?:\[\s*)?`", src):
        spans.append((m.end(), _find_backtick(src, m.end())))
    return spans


# ---------------------------------------------------------------------------
# Class A/B: the template
# ---------------------------------------------------------------------------

_SKIP_TAG_BODIES = ("style", "mat-icon")
_CTRL_HEADER = re.compile(r"@(if|else\s+if|for|switch|case|let)\b")
_CTRL_WORD = re.compile(r"@(else|default|empty)\b")
_INTERP = re.compile(r"\{\{")
_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def _clean(seg: str) -> str:
    seg = re.sub(r"&[A-Za-z][A-Za-z0-9]*;|&#\d+;", " ", seg)
    return re.sub(r"\s+", " ", seg).strip()


def _has_text(seg: str) -> bool:
    return bool(re.search(r"[A-Za-z]", _clean(seg)))


def _quoted_fragments(expr: str) -> list[str]:
    return _QUOTED.findall(expr)


def count_ab(template: str) -> tuple[int, int]:
    """(A, B) for one inline template."""
    a = 0
    seg: list[str] = []

    def flush() -> None:
        nonlocal a
        if seg and _has_text("".join(seg)):
            a += 1
        seg.clear()

    i, n = 0, len(template)
    while i < n:
        ch = template[i]
        if template.startswith("<!--", i):
            flush()
            end = template.find("-->", i)
            i = n if end < 0 else end + 3
        elif ch == "<":
            flush()
            m = re.match(r"<\s*([A-Za-z][A-Za-z0-9-]*)", template[i:])
            if not m:
                if template[i : i + 2] == "</":  # closing tag
                    i = _skip_tag(template, i)
                    continue
                seg.append(ch)  # stray '<' in text
                i += 1
                continue
            tag = m.group(1).lower()
            if tag in _SKIP_TAG_BODIES:
                close = re.compile(rf"</\s*{tag}\s*>", re.I)
                mm = close.search(template, i)
                i = n if not mm else mm.end()
            else:
                i = _skip_tag(template, i)
        elif ch == "@" and (m := _CTRL_HEADER.match(template, i)):
            flush()
            kw = m.group(1)
            i = m.end()
            if kw == "let":
                end = template.find(";", i)
                i = n if end < 0 else end + 1
            elif kw == "case" and template[i : i + 1].lstrip()[:1] != "(":
                # bare @case 'value' { — only the label, no parens to skip
                pass
            else:
                i = _skip_parens(template, i)
        elif ch == "@" and (m := _CTRL_WORD.match(template, i)):
            flush()
            i = m.end()
        elif template.startswith("{{", i):
            flush()
            end = template.find("}}", i)
            if end < 0:
                i = n
            else:
                expr = template[i + 2 : end]
                pre_pipe = _split_pipes(expr)[0] if _split_pipes(expr) else expr
                for frag in _quoted_fragments(pre_pipe):
                    if _has_text(frag):
                        a += 1
                i = end + 2
        else:
            seg.append(ch)
            i += 1
    flush()

    b = 0
    # Attribute values come single- or double-quoted; the other quote form may
    # appear inside the value (detection-panel's placeholder='..."..."' query).
    for m in re.finditer(
        r"(?<![\w\[-])(placeholder|title|aria-label|matTooltip|alt)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
        template,
    ):
        if _has_text(_attr_value(m)):
            b += 1
    for m in re.finditer(
        r"\[\s*((?:attr\.)[a-z-]+|matTooltip|placeholder|title|aria-label|alt)\s*\]\s*=\s*"
        r"(?:\"([^\"]*)\"|'([^']*)')",
        template,
    ):
        for frag in _quoted_fragments(_attr_value(m)):
            if _has_text(frag):
                b += 1
    return a, b


def _attr_value(m: re.Match) -> str:
    """The matched attribute value, whichever quote form wrapped it."""
    return m.group(2) if m.group(2) is not None else m.group(3)


def _skip_tag(template: str, i: int) -> int:
    """Skip a tag's attributes to its '>', quote-aware."""
    j = i + 1
    n = len(template)
    quote = ""
    while j < n:
        ch = template[j]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return j + 1
        j += 1
    return n


def _skip_parens(template: str, i: int) -> int:
    """Skip a control-flow header's parenthesised expression, quote-aware."""
    n = len(template)
    while i < n and template[i] != "(":
        if template[i : i + 2] == "{{":
            # Resume the main loop past the interpolation, never on the '}}'
            # itself (re-scanning it would count it as text; -1 would restart
            # the scan at template[-1] and double-count the whole template).
            end = template.find("}}", i)
            return n if end < 0 else end + 2
        i += 1
    depth = 0
    quote = ""
    while i < n:
        ch = template[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _split_pipes(expr: str) -> list[str]:
    """Split an interpolation on pipes (outside quotes), parens transparent.

    Every pipe splits, including inside parentheses: ``{{ (ended | date:'HH:mm') }}``
    keeps nothing, which is the #436 policy for date/number format args.
    """
    parts, buf, quote = [], [], ""
    for ch in expr:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
            buf.append(ch)
        elif ch == "|":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


# ---------------------------------------------------------------------------
# Class C: TypeScript literals
# ---------------------------------------------------------------------------

def count_c(src: str) -> list[str]:
    """Every candidate C fragment in one TS file, in source order."""
    spans = _skip_spans(src)
    frags: list[str] = []
    i, n = 0, len(src)

    def skipped(idx: int) -> bool:
        return any(a <= idx < b for a, b in spans)

    while i < n:
        if skipped(i):
            i = next((b + 1 for a, b in spans if a <= i < b), i + 1)
            continue
        ch = src[i]
        if src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end < 0 else end + 1
        elif src.startswith("/*", i):
            end = src.find("*/", i)
            i = n if end < 0 else end + 2
        elif ch in "\"'":
            end = _string_end(src, i)
            piece = [src[i + 1 : end]]
            j = end + 1
            while True:  # 'a' + 'b' concatenation reads as one sentence
                while j < n and src[j] in " \t\n\r":
                    j += 1
                if j < n and src[j] == "+":
                    j += 1
                    while j < n and src[j] in " \t\n\r":
                        j += 1
                    if j < n and src[j] in "\"'":
                        e2 = _string_end(src, j)
                        piece.append(src[j + 1 : e2])
                        j = e2 + 1
                        continue
                break
            frags.append("".join(piece))
            i = j
        elif ch == "`":
            # The opening backtick of a template/styles span is not a literal.
            if any(a - 1 == i for a, _ in spans):
                i = next(b for a, b in spans if a - 1 == i) + 1
                continue
            frags.extend(_backtick_joined(src, i))
            i = _backtick_end(src, i) + 1
        else:
            i += 1
    return frags


def _string_end(src: str, i: int) -> int:
    quote = src[i]
    j = i + 1
    n = len(src)
    while j < n:
        if src[j] == "\\":
            j += 2
        elif src[j] == quote:
            return j
        else:
            j += 1
    raise ValueError("unterminated string")


def _backtick_joined(src: str, i: int) -> list[str]:
    """One template literal → its fragments: all text runs merged into ONE
    fragment (an interpolation may split a sentence, but it is one sentence),
    plus each quoted fragment inside a top-level interpolation expression as
    its own fragment (quotes stripped). Quoted fragments inside parenthesised
    groups (toLocaleString format args, .join(', ')) are not UI words and are
    masked out."""
    text: list[str] = []
    frags: list[str] = []
    j = i + 1
    n = len(src)
    while j < n:
        ch = src[j]
        if ch == "\\":
            text.append(src[j + 1])
            j += 2
        elif ch == "`":
            return frags + ["".join(text)]
        elif src.startswith("${", j):
            end = _interp_end(src, j + 2)
            text.append(" ")
            expr = _mask_groups(src[j + 2 : end])
            for m in _QUOTED.finditer(expr):
                frags.append(m.group(0)[1:-1])
            j = end + 1
        else:
            text.append(ch)
            j += 1
    raise ValueError("unterminated template literal")


def _mask_groups(expr: str) -> str:
    """Blank out (...) / [...] / {...} groups (quote-aware): quoted literals
    inside them are call arguments, not words the interpolation shows."""
    out: list[str] = []
    depth = 0
    quote = ""
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch == "\\":
            out.append("  " if depth else expr[i : i + 2])
            i += 2
            continue
        if quote:
            out.append(ch if depth == 0 else " ")
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
            out.append(ch if depth == 0 else " ")
        elif ch in "([{":
            depth += 1
            out.append(" ")
        elif ch in ")]}":
            depth -= 1
            out.append(" ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _backtick_end(src: str, i: int) -> int:
    j = i + 1
    n = len(src)
    while j < n:
        if src[j] == "\\":
            j += 2
        elif src[j] == "`":
            return j
        elif src.startswith("${", j):
            j = _interp_end(src, j + 2) + 1
        else:
            j += 1
    raise ValueError("unterminated template literal")


def _interp_end(src: str, i: int) -> int:
    """End index of the '}' closing a ${...} interpolation, string-aware."""
    depth = 1
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in "\"'`":
            i = _string_end(src, i) if ch != "`" else _backtick_end(src, i)
            i += 1
        elif ch == "{":
            depth += 1
            i += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
            i += 1
        else:
            i += 1
    raise ValueError("unterminated interpolation")


def _classify_one(frag: str) -> bool:
    text = _clean(frag)
    if not re.search(r"[A-Za-z]", text):
        return False
    if PROSE.search(text):
        return True
    return text in KEEP_WORDS


def classify(frags: list[str]) -> int:
    """How many raw C fragments are user-facing under the policy."""
    return sum(1 for frag in frags if _classify_one(frag))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def counts(git_rev: str | None = None) -> dict[str, dict[str, object]]:
    """Per journey file: {'A': n, 'B': n, 'C': n}."""
    out: dict[str, dict[str, object]] = {}
    for journey, files in JOURNEYS:
        for rel in files:
            src = _read(rel, git_rev)
            a = b = 0
            tspan = template_span(src)
            if tspan:
                a, b = count_ab(src[tspan[0] : tspan[1]])
            c = classify(count_c(src))
            out[f"{journey}/{rel}"] = {"A": a, "B": b, "C": c}
    return out


def _totals(per_file: dict[str, dict[str, object]]) -> dict[str, int]:
    tot = {"A": 0, "B": 0, "C": 0}
    for v in per_file.values():
        for k in tot:
            tot[k] += v[k]  # type: ignore[operator]
    return tot


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--git", metavar="REV", help="count at a commit instead of the working tree")
    ap.add_argument("--check", action="store_true", help="ratchet against .dod-ui-literals-baseline")
    ap.add_argument("--report", action="store_true", help="also print per-file counts")
    args = ap.parse_args(argv)

    if args.check and args.git:
        ap.error(
            "--check ratchets the working tree and may lower .dod-ui-literals-baseline; "
            "--git counts a past state, which must never move the live baseline"
        )

    per_file = counts(args.git)
    tot = _totals(per_file)
    total = sum(tot.values())

    if args.report or not args.check:
        for journey, _ in JOURNEYS:
            for key, v in per_file.items():
                if key.startswith(journey + "/"):
                    print(f"  {key}: A={v['A']} B={v['B']} C={v['C']}")
    print(f"total: A={tot['A']} B={tot['B']} C={tot['C']} sum={total}")

    if not args.check:
        return 0
    try:
        base = int(BASELINE_FILE.read_text().strip())
    except (OSError, ValueError):
        base = total
    if total > base:
        print(
            f"UI-LITERALS FAIL: {total} > baseline {base}. Move journey words into the "
            "counter's owned templates or lower the baseline by cleaning literals.",
            file=sys.stderr,
        )
        return 1
    if total < base:
        BASELINE_FILE.write_text(f"{total}\n")
        print(f"  v ui_literals: {total} (baseline {base}) — baseline lowered")
    else:
        print(f"  ui_literals: {total} (baseline {base})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
