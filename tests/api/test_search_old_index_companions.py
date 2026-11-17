"""Companion oracles for ``tests/api/test_search_old_index.py`` (audit #443, card #454).

One companion per REAL strict-xfail oracle there, each pinning the same secure behaviour of
the same finding from a second input, and failing at the base for the same reason. Reuses the
``old_index`` fixture from the sibling module unchanged — the fixture is the oracle's world;
the companion only varies the query.
"""

from __future__ import annotations

import pytest
from test_search_old_index import ATTACK_DOMAIN  # sibling module; tests/ is not a package
from test_search_old_index import old_index as old_index  # noqa: F401 — pytest fixture, resolved by name


@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: index-migration-unproven (app/main.py:775) — the label guard checks "
    "only explicit field:value terms, free text reaches the indexed labels — companion",
)
def test_the_boundary_label_value_does_not_match_as_free_text(client, old_index):
    """``truenorth.inject`` is the boundary of the label guard: as an explicit term
    ``event.module:truenorth.inject`` it is refused to a Student (``is_label`` matches any
    ``event.module`` value starting with ``truenorth``, app/search_backends/detection_query.py),
    so the guard's own answer about this value is "not searchable". As bare free text the same
    value bypasses the term check, and on a pre-v1.0 index it exists in exactly one place: the
    labelled event's indexed ``event.module`` label. If the search counts or returns that
    event, free text matched an indexed label value the guard itself declared unsearchable."""
    guarded = client.get(
        f"/telemetry/{old_index}/search", params={"q": "event.module:truenorth.inject"}
    )
    assert guarded.status_code == 422, guarded.text

    r = client.get(f"/telemetry/{old_index}/search", params={"q": "truenorth.inject"})
    assert r.status_code == 200, r.text
    body = r.json()
    leak = [h for h in body["hits"]["hits"] if h["_source"].get("url.domain") == ATTACK_DOMAIN]
    assert body["hits"]["total"]["value"] == 0 and not leak, (
        "Companion to ROW 14 of the #435 limitation inventory (docs/release-notes/v1.0.0.md): "
        "on a pre-v1.0 dynamically-mapped index the Student's free-text query 'truenorth.inject' "
        "matched the indexed label event.module:truenorth.inject — a value the label guard "
        "rejects when written as the explicit term event.module:truenorth.inject — and singled "
        "out the inject event."
    )
