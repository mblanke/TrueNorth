"""The detection query language: what a Student may submit as a detection (ADR 0005).

Detection credit used to run a Student's query through OpenSearch ``query_string`` as
written. That parser reaches every field, including the labels the platform stamps on
inject telemetry (``inject_action``, ``exercise_id``, ``event.module:truenorth.inject``,
``threat.technique.id`` ...): a Student who queried a label matched exactly the inject's
events and took the credit without detecting anything.

This is a closed grammar, like the telemetry search one in ``query.py`` (same field-name
rule, same ``QueryError``), but with what a detection needs and the search grammar lacks:

    field:value            exact value                    process_name:rundll32.exe
    field:"a phrase"       quoted value                   attachment.name:"q1 brief.docm"
    field:*mid*dle?        wildcards anywhere             url.domain:*northwind*
    field:*                the field exists               CommandLine:*
    field:>N  >= < <=      numeric range                  bytes_out:>100000000
    field:(a OR b)         any of these values            EventID:(4728 OR 4732)
    a AND b, a OR b, NOT a, ( ... )                       boolean structure
    *                      everything

Every term names its field: there is no free text, which would search every field and so
the labels too. Field names may not start with ``_`` and may not be a ground-truth label
(``is_label``). No regex, fuzzy, proximity, boosts, field-name wildcards or ``_exists_``.
A query outside the grammar is a ``QueryError`` (the API answers 422, no attempt used).

``DetectionQuery.lucene()`` re-serialises the parsed query canonically, every value
escaped, so what reaches ``query_string`` is exactly what was checked here.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from .query import _FIELD, QueryError

MAX_DETECTION_LENGTH = 2000
MAX_DETECTION_TERMS = 32
MAX_DEPTH = 8

# Where the scenario engine puts inject labels (scenario_engine.injectors.GROUND_TRUTH_FIELD).
# Stored, never indexed (telemetry/pipelines/bootstrap.py maps it enabled: false).
GROUND_TRUTH_FIELD = "tn_ground_truth"

# Field-name prefixes that are platform labels, not observations. The flat names are what
# inject telemetry carried before the labels moved under GROUND_TRUTH_FIELD (still present
# in indices written before 2026-10-08).
_LABEL_PREFIXES = (
    GROUND_TRUTH_FIELD,
    "inject_",
    "inject.",
    "truenorth",
    "threat.technique",
    "threat.tactic",
)
_LABEL_NAMES = frozenset({"inject", "exercise_id"})
# Marks a quoted value inside the parser. Control characters are refused in input, so a
# Student's value can never start with it.
_PHRASE = "\x00"
_LABEL_MODULE = "truenorth.inject"

_NUMBER = re.compile(r"^-?\d{1,18}(?:\.\d{1,9})?$")
# Escaped wherever they appear in a value; + and - only at the start (Lucene term rules).
_ALWAYS_ESCAPE = set('\\():^[]"{}~/!&|<>= \t')


def is_label(field: str, value: str | None = None) -> bool:
    """Is ``field`` (with ``value``, for ``event.module``) a ground-truth label?"""
    name = field.lower()
    if name in _LABEL_NAMES or any(name.startswith(p) for p in _LABEL_PREFIXES):
        return True
    if name == "event.module":
        v = (value or "").lower()
        # truenorth*, and any pattern that would also match the label value
        return v == "" or v.startswith("truenorth") or fnmatch.fnmatchcase(_LABEL_MODULE, v)
    return False


@dataclass(frozen=True)
class Term:
    field: str
    kind: str  # "value" | "phrase" | "exists" | "range" | "any"
    values: tuple[str, ...] = ()
    op: str = ""  # range operator

    def lucene(self) -> str:
        if self.kind == "exists":
            return f"_exists_:{self.field}"
        if self.kind == "range":
            return f"{self.field}:{self.op}{self.values[0]}"
        if self.kind == "any":
            return f"{self.field}:(" + " OR ".join(_value(v) for v in self.values) + ")"
        return f"{self.field}:{_value(self.values[0])}"


@dataclass(frozen=True)
class Node:
    op: str  # "AND" | "OR" | "NOT" | "ALL"
    children: tuple = ()

    def lucene(self) -> str:
        if self.op == "ALL":
            return "*"
        if self.op == "NOT":
            return f"NOT ({self.children[0].lucene()})"
        return f" {self.op} ".join(f"({c.lucene()})" if isinstance(c, Node) else c.lucene() for c in self.children)


@dataclass(frozen=True)
class DetectionQuery:
    root: Node | Term

    def lucene(self) -> str:
        return self.root.lucene()


def _value(v: str) -> str:
    """A value as one query_string term; a quoted value is quoted again."""
    if v.startswith(_PHRASE):
        inner = v[1:]
        return '"' + inner.replace("\\", "\\\\").replace('"', '\\"') + '"'
    out = []
    for i, c in enumerate(v):
        if c in _ALWAYS_ESCAPE or (i == 0 and c in "+-"):
            out.append("\\" + c)
        else:
            out.append(c)
    return "".join(out)


# ── lexer ──────────────────────────────────────────────────────────────
_OPERATORS = {"AND": "AND", "&&": "AND", "OR": "OR", "||": "OR", "NOT": "NOT", "!": "NOT"}
_STOP = set(" \t()")


class _Lexer:
    def __init__(self, q: str) -> None:
        self.q, self.i = q, 0

    def skip(self) -> None:
        while self.i < len(self.q) and self.q[self.i] in " \t":
            self.i += 1

    def peek(self) -> str:
        self.skip()
        return self.q[self.i] if self.i < len(self.q) else ""

    def word(self) -> str:
        """A bare word, escapes resolved. Stops at whitespace and parentheses."""
        self.skip()
        out, q = [], self.q
        while self.i < len(q) and q[self.i] not in _STOP:
            c = q[self.i]
            if c == "\\":
                if self.i + 1 >= len(q):
                    raise QueryError("dangling escape")
                out.append(q[self.i + 1])
                self.i += 2
                continue
            if c in '"[]{}^~/':
                raise QueryError(f"'{c}' is not part of the detection language (no regex, fuzzy, boost or range brackets)")
            out.append(c)
            self.i += 1
        return "".join(out)

    def quoted(self) -> str:
        """A double-quoted string (the opening quote is at self.i). Returned with a leading
        ``_PHRASE`` marker so the serialiser knows to quote it again."""
        q, out = self.q, []
        self.i += 1
        while self.i < len(q):
            c = q[self.i]
            if c == "\\" and self.i + 1 < len(q):
                out.append(q[self.i + 1])
                self.i += 2
                continue
            if c == '"':
                self.i += 1
                if self.i < len(q) and q[self.i] in "~^":
                    raise QueryError("proximity and boosts are not part of the detection language")
                return _PHRASE + "".join(out)
            out.append(c)
            self.i += 1
        raise QueryError("unbalanced quote")


class _Parser:
    def __init__(self, q: str) -> None:
        self.lx = _Lexer(q)
        self.terms = 0

    def parse(self) -> Node | Term:
        node = self.expr(0)
        if self.lx.peek():
            raise QueryError("unparseable query")
        return node

    def _op(self) -> str | None:
        """The operator at the cursor, without consuming it."""
        lx = self.lx
        lx.skip()
        for tok, op in _OPERATORS.items():
            end = lx.i + len(tok)
            if lx.q.startswith(tok, lx.i) and (end >= len(lx.q) or lx.q[end] in " \t(" or tok in ("!",)):
                return op
        return None

    def _take(self, op: str) -> None:
        lx = self.lx
        for tok, o in _OPERATORS.items():
            if o == op and lx.q.startswith(tok, lx.i):
                lx.i += len(tok)
                return

    def expr(self, depth: int) -> Node | Term:
        if depth > MAX_DEPTH:
            raise QueryError("query nested too deeply")
        parts = [self.conj(depth)]
        while self._op() == "OR":
            self._take("OR")
            parts.append(self.conj(depth))
        return parts[0] if len(parts) == 1 else Node("OR", tuple(parts))

    def conj(self, depth: int) -> Node | Term:
        parts = [self.unary(depth)]
        while True:
            op = self._op()
            if op == "AND":
                self._take("AND")
            elif (op is not None and op != "NOT") or self.lx.peek() in ("", ")"):
                break  # OR belongs to the caller; ")" or the end closes this conjunction
            parts.append(self.unary(depth))
        return parts[0] if len(parts) == 1 else Node("AND", tuple(parts))

    def unary(self, depth: int) -> Node | Term:
        if self._op() == "NOT":
            self._take("NOT")
            return Node("NOT", (self.unary(depth + 1),))
        c = self.lx.peek()
        if c == "(":
            self.lx.i += 1
            inner = self.expr(depth + 1)
            if self.lx.peek() != ")":
                raise QueryError("unbalanced parenthesis")
            self.lx.i += 1
            return inner
        if c in ("", ")"):
            raise QueryError("expected a field:value term")
        return self.term()

    def term(self) -> Node | Term:
        lx = self.lx
        lx.skip()
        m = re.compile(r"([^\s:()\"\\]+):").match(lx.q, lx.i)
        if m is None:
            if lx.q.startswith("*", lx.i) and (lx.i + 1 == len(lx.q) or lx.q[lx.i + 1] in _STOP):
                lx.i += 1
                return Node("ALL")
            raise QueryError("every term must name its field (field:value); free text is not accepted")
        field = m.group(1)
        if not _FIELD.match(field):
            raise QueryError(f"invalid field name {field!r}")
        lx.i = m.end()
        self.terms += 1
        if self.terms > MAX_DETECTION_TERMS:
            raise QueryError(f"more than {MAX_DETECTION_TERMS} terms")
        term = self.value(field)
        for v in term.values or ("",):
            if is_label(field, v.lstrip(_PHRASE)):
                raise QueryError(f"{field!r} is a platform label, not something a sensor observed")
        return term

    def value(self, field: str) -> Term:
        lx = self.lx
        if lx.i < len(lx.q) and lx.q[lx.i] in " \t":
            raise QueryError(f"empty value for field {field!r}")
        c = lx.q[lx.i] if lx.i < len(lx.q) else ""
        if c == "":
            raise QueryError(f"empty value for field {field!r}")
        if c == '"':
            phrase = lx.quoted()
            if lx.i < len(lx.q) and lx.q[lx.i] not in _STOP:
                raise QueryError("unparseable query after a quoted value")
            return Term(field, "phrase", (phrase,))
        if c == "(":
            lx.i += 1
            values = [self._single(field)]
            while self._op() == "OR":
                self._take("OR")
                values.append(self._single(field))
            if lx.peek() != ")":
                raise QueryError("a value group is (a OR b ...)")
            lx.i += 1
            self.terms += len(values) - 1
            return Term(field, "any", tuple(values))
        for op in (">=", "<=", ">", "<"):
            if lx.q.startswith(op, lx.i):
                lx.i += len(op)
                num = lx.word()
                if not _NUMBER.match(num):
                    raise QueryError(f"a range compares numbers: {field}:{op}{num}")
                return Term(field, "range", (num,), op)
        word = lx.word()
        if not word:
            raise QueryError(f"empty value for field {field!r}")
        if word == "*":
            return Term(field, "exists")
        return Term(field, "value", (word,))

    def _single(self, field: str) -> str:
        if self.lx.peek() == '"':
            return self.lx.quoted()
        word = self.lx.word()
        if not word:
            raise QueryError(f"empty value in the group for {field!r}")
        return word


def parse_detection(q: str | None) -> DetectionQuery:
    """Parse a submitted detection, or raise ``QueryError``."""
    q = (q or "").strip()
    if len(q) > MAX_DETECTION_LENGTH:
        raise QueryError(f"query longer than {MAX_DETECTION_LENGTH} characters")
    if not q:
        raise QueryError("empty query")
    if any(ord(c) < 32 and c != "\t" for c in q):
        raise QueryError("control characters are not allowed")
    return DetectionQuery(_Parser(q).parse())


def hide_labels(result: dict) -> dict:
    """A search response with every ground-truth label removed from each hit's ``_source``
    (the namespaced object, and the flat labels of indices written before it existed)."""
    for hit in (result.get("hits") or {}).get("hits") or []:
        src = hit.get("_source") if isinstance(hit, dict) else None
        if isinstance(src, dict):
            for key in [k for k, v in src.items() if is_label(k, v if isinstance(v, str) else None)]:
                if key != "event.module" or is_label(key, str(src[key])):
                    del src[key]
            event = src.get("event")
            if isinstance(event, dict) and is_label("event.module", str(event.get("module", ""))):
                event.pop("module", None)
    return result


_LABEL_FIELD_REF = re.compile(
    r"(?<![\w.@\\-])(?:"
    + "|".join(re.escape(p) + r"[\w.]*" for p in _LABEL_PREFIXES)
    + r"|exercise_id|inject)\s*:",
    re.IGNORECASE,
)
_LABEL_MODULE_REF = re.compile(r"(?<![\w.@\\-])event\.module\s*:\s*\(?\s*\"?\*?truenorth", re.IGNORECASE)


def references_labels(lucene: str) -> bool:
    """Does a (scenario-authored) Lucene query name a ground-truth label? Such a key can
    never match: labels are stored, not indexed."""
    text = lucene.replace("truenorth.ingested_at", "")  # the window field, not a label
    return bool(_LABEL_FIELD_REF.search(text) or _LABEL_MODULE_REF.search(text))
