"""The telemetry search language: a small, closed grammar instead of raw query_string.

``GET /telemetry/{range_id}/search?q=`` used to hand ``q`` to OpenSearch's
``query_string`` verbatim. That parser accepts regular expressions, fuzzy and proximity
operators, leading wildcards and arbitrary fields (``_index``, ``_id``, ...), so one
request could pin a node on a pathological regex or read fields nobody meant to expose.
The index is fixed per range and ownership is checked before this runs; this module
bounds what a query may *do* inside that index.

The grammar, all terms ANDed together:

    field:value         exact phrase on one field          event_type:dns_query
    field:"two words"   quoted phrase                      hostname:"ws 01"
    field:prefix*       prefix on one field                process_name:power*
    field:*             the field exists                   mitre_technique:*
    free text           simple_query_string over all       powershell -notepad
    * (or empty)        everything

A bare ``AND`` is accepted and ignored; ``OR`` / ``NOT`` only make sense in free text and
are refused alongside field terms rather than silently ANDed. Field names are plain identifiers (letters, digits, ``_ . @ -``) and may not start with
``_``. Free text goes to ``simple_query_string`` with a fixed flag set (no fuzzy, no
slop/near). Anything outside the grammar is a ``QueryError``; the API answers 422.

Parsing is backend-agnostic; turning a ``ParsedQuery`` into a request body is each
backend's job (see ``opensearch.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_QUERY_LENGTH = 512
MAX_FIELD_TERMS = 16

_FIELD = re.compile(r"^[A-Za-z@][A-Za-z0-9_.@-]{0,127}$")
# One token: an optional `field:` prefix, then a double-quoted string or a bare word.
_TOKEN = re.compile(r'(?:(?P<field>[^\s:"]+):)?(?P<value>"(?:[^"\\]|\\.)*"|[^\s"]+|")')


class QueryError(ValueError):
    """The query is outside the telemetry search grammar."""


@dataclass(frozen=True)
class FieldTerm:
    field: str
    value: str
    kind: str  # "phrase" | "prefix" | "exists"


@dataclass(frozen=True)
class ParsedQuery:
    terms: tuple[FieldTerm, ...] = field(default_factory=tuple)
    text: str = ""  # free text for simple_query_string; "" when there is none

    @property
    def match_all(self) -> bool:
        return not self.terms and not self.text


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    if '"' in value:
        raise QueryError("unbalanced quote")
    return value


def parse_query(q: str | None) -> ParsedQuery:
    """Parse ``q`` into field terms and free text, or raise ``QueryError``."""
    q = (q or "").strip()
    if len(q) > MAX_QUERY_LENGTH:
        raise QueryError(f"query longer than {MAX_QUERY_LENGTH} characters")
    if q in ("", "*"):
        return ParsedQuery()
    if any(ord(c) < 32 for c in q.replace("\t", " ")):
        raise QueryError("control characters are not allowed")

    terms: list[FieldTerm] = []
    text: list[str] = []
    pos = text_end = 0
    for m in _TOKEN.finditer(q):
        if q[pos : m.start()].strip():
            raise QueryError("unparseable query")
        pos = m.end()
        name, raw = m.group("field"), m.group("value")
        if name is None:
            if text and m.start() == text_end:  # -"a b": one operand, not two
                text[-1] += raw
            elif raw in ("AND", "&&"):  # terms are ANDed anyway
                continue
            else:
                text.append(raw)
            text_end = m.end()
            continue
        if not _FIELD.match(name):
            raise QueryError(f"invalid field name {name!r}")
        value = _unquote(raw)
        if not value:
            raise QueryError(f"empty value for field {name!r}")
        quoted = raw.startswith('"')
        if not quoted and value == "*":
            terms.append(FieldTerm(name, "", "exists"))
        elif not quoted and value.endswith("*") and not any(c in value[:-1] for c in "*?"):
            terms.append(FieldTerm(name, value[:-1], "prefix"))
        elif not quoted and any(c in value for c in "*?"):
            raise QueryError("wildcards are only allowed at the end of a value")
        else:
            terms.append(FieldTerm(name, value, "phrase"))
    if q[pos:].strip():
        raise QueryError("unparseable query")
    if len(terms) > MAX_FIELD_TERMS:
        raise QueryError(f"more than {MAX_FIELD_TERMS} field terms")
    if terms and {"OR", "||", "NOT"}.intersection(text):
        # field:a OR field:b would silently become field:a AND field:b AND "or".
        raise QueryError("field terms are always ANDed; OR/NOT apply to free text only")
    free = " ".join(text)
    if free.count('"') % 2:
        raise QueryError("unbalanced quote")
    return ParsedQuery(terms=tuple(terms), text=free)
