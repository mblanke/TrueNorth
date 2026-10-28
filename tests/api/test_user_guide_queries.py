"""Every telemetry query the instructor guide shows parses in the search grammar.

The guide (docs/guides/04-instructor-guide.md, which replaced docs/user-guide.md) once listed Lucene queries (``AND NOT``, ``destination.ip:10.*``) that
the closed grammar in ``app/search_backends/query.py`` refuses with 422, so a Student
copying them got an error. These tests parse every example in its "Search Syntax" and
"Common Queries" tables, and check that what the guide says is refused is refused.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.search_backends.query import QueryError, parse_query

GUIDE = Path(__file__).resolve().parents[2] / "docs/guides/04-instructor-guide.md"


def _section(title: str) -> str:
    text = GUIDE.read_text()
    return text.split(f"### {title}\n", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]


def _last_column_examples(title: str) -> list[str]:
    rows = [ln for ln in _section(title).splitlines() if ln.startswith("|") and "`" in ln]
    return [re.findall(r"`([^`]*)`", ln.rstrip(" |").rsplit("|", 1)[1])[0] for ln in rows]


COMMON = _last_column_examples("Common Queries")
SYNTAX = _last_column_examples("Search Syntax")


def test_the_tables_have_examples():
    assert len(COMMON) >= 8 and len(SYNTAX) == 5


@pytest.mark.parametrize("q", COMMON + SYNTAX)
def test_every_documented_query_parses(q):
    parsed = parse_query(q)
    assert not parsed.match_all, q


@pytest.mark.parametrize(
    "q",
    [
        "event_type:dns_query AND NOT hostname:dc01",
        "event_type:dns_query OR event_type:http_request",
        "dns_query:*.corp.local",
        "dest_ip:10.*.1",
        "_index:range-x",
    ],
)
def test_what_the_guide_says_is_refused_is_refused(q):
    with pytest.raises(QueryError):
        parse_query(q)
