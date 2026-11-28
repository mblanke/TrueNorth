# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""One spelling per validator (app/detections/names.py) and ``{{ name }}`` rendering
(app/detections/templating.py).

Rows have said ``opensearch_query``, ``validate.opensearch_query``,
``validate.opensearch.query`` and ``validate_opensearch_query`` for the same validator; a
scorer that knew one spelling silently skipped the others. And an objective query that
still says ``{{ c2_domain }}`` can never match an event, so placeholders are rendered from
the scenario's ``variables:`` and any left over makes the objective unscorable.
"""

from __future__ import annotations

import pytest
from app.detections.names import CANONICAL, DELIVERABLE, MANUAL, QUERY, canonical_validator
from app.detections.templating import PLACEHOLDER, render, unresolved


class TestCanonicalValidator:
    @pytest.mark.parametrize(
        ("spelling", "expected"),
        [
            ("opensearch_query", "opensearch_query"),
            ("validate.opensearch_query", "opensearch_query"),
            ("validate_opensearch_query", "opensearch_query"),
            ("validate.opensearch.query", "opensearch_query"),
            ("  VALIDATE.OPENSEARCH.QUERY  ", "opensearch_query"),  # case and padding
            ("validate.manual_ack", MANUAL),
            ("validate.deliverable_check", DELIVERABLE),
            ("validate.greyspace_breadcrumb", "greyspace_breadcrumb"),
            ("validate.user_disabled", "user_disabled"),
            ("my_custom_validator", "my_custom_validator"),  # custom validators stay visible
            ("custom.check", "custom_check"),  # normalised, never silently renamed away
        ],
    )
    def test_every_spelling_canonicalises_to_one_name(self, spelling, expected):
        assert canonical_validator(spelling) == expected

    @pytest.mark.parametrize("empty", [None, "", "   "])
    def test_an_empty_name_is_the_default(self, empty):
        assert canonical_validator(empty) == MANUAL
        assert canonical_validator(empty, default=QUERY) == QUERY

    def test_the_canonical_set_is_exactly_these_validators(self):
        assert CANONICAL == (QUERY, MANUAL, DELIVERABLE, "user_disabled", "greyspace_breadcrumb")


class TestTemplating:
    def test_a_placeholder_with_a_value_is_filled(self):
        assert render("url.domain:*{{ c2_domain }}*", {"c2_domain": "northwind-update.example"}) == (
            "url.domain:*northwind-update.example*"
        )

    def test_padded_placeholders_render_too(self):
        assert render("{{ c2_domain }}", {"c2_domain": "x.example"}) == "x.example"
        assert render("{{c2_domain}}", {"c2_domain": "x.example"}) == "x.example"

    def test_an_unknown_placeholder_is_kept_so_the_objective_is_unscorable(self):
        text = "src:{{ attacker_ip }}"
        assert render(text, {"c2_domain": "x.example"}) == text
        assert unresolved(text) == ["attacker_ip"]

    def test_values_become_strings(self):
        assert render("port:{{ port }}", {"port": 445}) == "port:445"

    def test_no_variables_means_nothing_is_rendered(self):
        text = "q:{{ a }}"
        assert render(text, None) == text
        assert render(text, {}) == text

    def test_unresolved_of_empty_text_is_empty(self):
        assert unresolved("") == []
        assert unresolved(None) == []

    def test_a_placeholder_name_is_a_python_identifier(self):
        # ``{{ 1bad }}`` and ``{{ a-b }}`` are not placeholders; only [A-Za-z_]\w* is.
        assert unresolved("{{ 1bad }} {{ a-b }} {{ good_1 }}") == ["good_1"]
        assert PLACEHOLDER.sub("X", "{{ good_1 }} stays {{ not-a-name }}") == "X stays {{ not-a-name }}"
