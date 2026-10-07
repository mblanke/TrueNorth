"""The telemetry search grammar (app/search_backends/query.py) and its OpenSearch DSL.

``q`` used to reach OpenSearch ``query_string`` verbatim. These pin what a query may
still express, and that nothing a caller types becomes a regex, fuzzy, proximity,
leading-wildcard or ``_``-field query.
"""

from __future__ import annotations

import pytest
from app.search_backends.opensearch import SIMPLE_QUERY_FLAGS, build_query
from app.search_backends.query import (
    MAX_FIELD_TERMS,
    MAX_QUERY_LENGTH,
    FieldTerm,
    QueryError,
    parse_query,
)


class TestGrammar:
    @pytest.mark.parametrize("q", [None, "", "   ", "*"])
    def test_empty_and_star_match_everything(self, q):
        assert parse_query(q).match_all
        assert build_query(parse_query(q)) == {"match_all": {}}

    def test_field_value_is_a_phrase(self):
        p = parse_query("event_id:2b1c-77aa")
        assert p.terms == (FieldTerm("event_id", "2b1c-77aa", "phrase"),)
        assert p.text == ""

    def test_quoted_value_keeps_spaces_and_escapes(self):
        p = parse_query(r'hostname:"ws 01 \"lab\""')
        assert p.terms == (FieldTerm("hostname", 'ws 01 "lab"', "phrase"),)

    def test_trailing_star_is_a_prefix(self):
        assert parse_query("process_name:power*").terms == (FieldTerm("process_name", "power", "prefix"),)

    def test_bare_star_value_is_exists(self):
        assert parse_query("mitre_technique:*").terms == (FieldTerm("mitre_technique", "", "exists"),)

    def test_dotted_and_at_fields(self):
        p = parse_query("data.batch_tag:abc @timestamp:2026-10-07")
        assert [t.field for t in p.terms] == ["data.batch_tag", "@timestamp"]

    def test_value_may_contain_colons(self):
        assert parse_query("url:http://x/y").terms == (FieldTerm("url", "http://x/y", "phrase"),)

    def test_free_text_and_fields_combine(self):
        p = parse_query('event_type:process_exec powershell -"notepad.exe"')
        assert p.terms == (FieldTerm("event_type", "process_exec", "phrase"),)
        assert p.text == 'powershell -"notepad.exe"'

    def test_bare_and_is_dropped(self):
        p = parse_query("event_type:a AND mitre_technique:T1059")
        assert len(p.terms) == 2 and p.text == ""

    def test_quoted_star_is_a_literal_phrase(self):
        assert parse_query('cmd:"a*b"').terms == (FieldTerm("cmd", "a*b", "phrase"),)


class TestRejected:
    """Each of these was legal (and some expensive or revealing) under query_string."""

    @pytest.mark.parametrize(
        "q",
        [
            "_index:range-other",  # metadata fields
            "_id:abc",
            "_source:*",
            "a b c:d e (f:g)",  # "(f" is not a field name
            "1abc:x",  # field must start with a letter or @
            "a$b:x",
        ],
    )
    def test_bad_field_names(self, q):
        with pytest.raises(QueryError):
            parse_query(q)

    @pytest.mark.parametrize("q", ["cmd:*powershell", "cmd:pow?rshell", "cmd:a*b*", "cmd:**"])
    def test_wildcards_other_than_trailing(self, q):
        with pytest.raises(QueryError, match="wildcard"):
            parse_query(q)

    @pytest.mark.parametrize("q", ['cmd:"unterminated', '"unterminated', 'a "b c'])
    def test_unbalanced_quotes(self, q):
        with pytest.raises(QueryError, match="quote"):
            parse_query(q)

    @pytest.mark.parametrize("q", ["a:x OR a:y", "a:x NOT b:y", "a:x || a:y"])
    def test_or_not_with_field_terms(self, q):
        with pytest.raises(QueryError, match="ANDed"):
            parse_query(q)

    def test_empty_quoted_value(self):
        with pytest.raises(QueryError, match="empty"):
            parse_query('a:""')

    def test_control_characters(self):
        with pytest.raises(QueryError, match="control"):
            parse_query("a:b\x00c")

    def test_length_cap(self):
        with pytest.raises(QueryError, match="longer"):
            parse_query("x" * (MAX_QUERY_LENGTH + 1))

    def test_field_term_cap(self):
        with pytest.raises(QueryError, match="field terms"):
            parse_query(" ".join(f"f{i}:v" for i in range(MAX_FIELD_TERMS + 1)))


def _keys(node) -> set[str]:
    """Every dict key in a DSL tree: the query types and field names it uses."""
    if isinstance(node, dict):
        return set(node) | set().union(*(_keys(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_keys(v) for v in node))
    return set()


class TestOpenSearchDsl:
    @pytest.mark.parametrize(
        "q",
        [
            "/.*(a+)+$/",  # regex: ReDoS shape under query_string
            "powershel~2",  # fuzzy
            '"a b"~10',  # proximity
            "*admin",  # leading wildcard
            "event_type:x _index:y",
            "a:b",
            "free text",
        ],
    )
    def test_never_emits_query_string_or_regex(self, q):
        try:
            dsl = build_query(parse_query(q))
        except QueryError:
            return  # refused outright is fine too
        keys = _keys(dsl)
        for banned in ("query_string", "regexp", "fuzzy", "wildcard", "_index", "script"):
            assert banned not in keys, (q, banned, dsl)

    def test_free_text_goes_to_simple_query_string_without_fuzzy_or_slop(self):
        dsl = build_query(parse_query("/.*(a+)+$/ powershel~2"))
        sqs = dsl["bool"]["must"][0]["simple_query_string"]
        assert sqs["query"] == "/.*(a+)+$/ powershel~2"
        assert sqs["flags"] == SIMPLE_QUERY_FLAGS
        assert not {"FUZZY", "NEAR", "SLOP", "ALL"}.intersection(SIMPLE_QUERY_FLAGS.split("|"))
        assert sqs["default_operator"] == "and" and sqs["lenient"] is True

    def test_terms_become_typed_clauses(self):
        dsl = build_query(parse_query("event_type:dns_query process_name:power* mitre_technique:*"))
        assert dsl == {
            "bool": {
                "must": [
                    {"match_phrase": {"event_type": "dns_query"}},
                    {"prefix": {"process_name": {"value": "power"}}},
                    {"exists": {"field": "mitre_technique"}},
                ]
            }
        }
