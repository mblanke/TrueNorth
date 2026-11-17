"""What a Student's free-text search returns on a pre-v1.0 index (row 14 of #435, "index-migration-unproven").

Fresh v1.0 installs store the inject labels under ``tn_ground_truth`` with ``enabled: false``
(stored, never indexed) and carry no flat labels, so no free-text term can match a label. An
index written before that (dynamic mapping) indexed every field its documents carried — the
flat labels ``inject_action``, ``exercise_id``, ``truenorth_simulated``, ``event.module``,
``threat.technique.id`` included. On such an index ``build_query``'s free text
(``simple_query_string`` with no field restriction, app/search_backends/opensearch.py) matches
label values, and the endpoint checks ``is_label`` only on explicit ``field:value`` terms
(app/main.py). docs/release-notes/v1.0.0.md documents exactly this limitation.

Pinned here: a Student's free-text search succeeds and does not single out the labelled event
for a term that exists only in its labels. Today the leak is real, so the second test FAILS,
naming row 14; when free text is restricted to indexed observability fields (or old indices
are reindexed), it passes and stays as the regression pin. The OpenSearch side is faked the
way tests/api/test_search_backends.py does it (respx in front of ``OpenSearchBackend``), with
the pre-v1.0 dynamic mapping modelled as: every field a document carries is indexed.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from _shared import STARTED, _at, act_as, real_exercise, real_tenant, real_user
from app import main as app_main
from app.models import ExerciseState, UserRole
from app.search_backends import OpenSearchBackend

OS_URL = "http://mock-os:9200"
ATTACK_DOMAIN = "cdn.northwind-update.example"


def _labelled_event() -> dict:
    """The attack as pre-v1.0 ingest wrote it: observables plus the flat labels (the names
    ``is_label`` matches) — all of them indexed by the old dynamic mapping."""
    return {
        "@timestamp": _at(5),
        "event_type": "http",
        "url.domain": ATTACK_DOMAIN,
        "inject_action": "c2_beacon",
        "exercise_id": "old-range-exercise",
        "truenorth_simulated": True,
        "event.module": "truenorth.inject",
        "threat.technique.id": "T1071.001",
        "truenorth": {"ingested_at": _at(5)},
    }


def _noise_event(i: int) -> dict:
    return {"@timestamp": _at(6), "event_type": "http", "url.domain": f"site{i}.example"}


def _get(event: dict, dotted: str):
    if dotted in event:
        return event[dotted]
    cur = event
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class _PreV1Index:
    """The part of OpenSearch ``build_query`` exercises, on an index whose dynamic mapping
    indexed every field these documents carry — flat labels included. Only the clauses
    ``build_query`` emits are modelled; ``default_operator: and`` means every token of the
    free text must match somewhere in the document."""

    def __init__(self, events: list[dict]) -> None:
        self.events = events

    @staticmethod
    def _indexed_text(event: dict) -> str:
        leaves: list[str] = []

        def walk(value) -> None:
            if isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)
            elif value is not None:
                leaves.append(str(value).lower())

        walk(event)
        return " ".join(leaves)

    def _matches(self, event: dict, clause: dict) -> bool:
        if "match_all" in clause:
            return True
        if "simple_query_string" in clause:
            tokens = clause["simple_query_string"]["query"].lower().split()
            text = self._indexed_text(event)
            return all(t in text for t in tokens)
        if "match_phrase" in clause:
            [(field, value)] = clause["match_phrase"].items()
            return _get(event, field) == value
        if "prefix" in clause:
            [(field, body)] = clause["prefix"].items()
            value = _get(event, field)
            return value is not None and str(value).startswith(body["value"])
        if "exists" in clause:
            return _get(event, clause["exists"]["field"]) is not None
        raise AssertionError(f"clause the fake does not model: {clause}")

    def search(self, dsl: dict) -> list[dict]:
        must = dsl.get("bool", {}).get("must", []) if "bool" in dsl else [dsl]
        if not must:
            must = [dsl]
        return [e for e in self.events if all(self._matches(e, c) for c in must)]


@pytest.fixture
def old_index(client, db_session, monkeypatch, respx_mock):
    """A Student of a running exercise with no roster (readable per app/telemetry_access.py),
    and the pre-v1.0 index ``range-{id}`` holding one labelled event and noise, served by a
    fake OpenSearch behind the real ``OpenSearchBackend``."""
    tenant = real_tenant(db_session, f"old-index-{uuid.uuid4().hex[:6]}")
    ex = real_exercise(db_session, tenant.id, state=ExerciseState.running, started_at=STARTED)
    db_session.commit()
    act_as(real_user(db_session, UserRole.student, tenant.id))
    index = f"range-{ex.range_id}"
    idx = _PreV1Index([_labelled_event(), _noise_event(1), _noise_event(2)])

    def search(request):
        body = json.loads(request.content)
        found = idx.search(body["query"])
        return httpx.Response(
            200,
            json={"hits": {"total": {"value": len(found)},
                           "hits": [{"_index": index, "_id": str(i), "_source": e} for i, e in enumerate(found)]}},
        )

    respx_mock.post(f"{OS_URL}/{index}/_search").mock(side_effect=search)
    monkeypatch.setattr(app_main, "get_search_backend", lambda: OpenSearchBackend(url=OS_URL))
    return ex.range_id


def test_the_student_can_search_the_old_index_and_find_events(client, old_index):
    """Control: the range is readable and the index answers — so a zero below is a real
    guard, not a broken fixture. The attack is findable by an observable field."""
    r = client.get(f"/telemetry/{old_index}/search", params={"q": f"url.domain:{ATTACK_DOMAIN}"})
    assert r.status_code == 200, r.text
    hits = r.json()["hits"]["hits"]
    assert [h["_source"]["url.domain"] for h in hits] == [ATTACK_DOMAIN]


@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: index-migration-unproven (app/main.py:775) — the label guard checks "
    "only explicit field:value terms, free text reaches the indexed labels",
)
def test_a_students_free_text_does_not_match_indexed_label_values(client, old_index):
    """``c2_beacon`` exists in exactly one place in this index: the labelled event's
    ``inject_action``. If the search counts or returns that event, free text matched an
    indexed label value and the Student has picked out the inject."""
    r = client.get(f"/telemetry/{old_index}/search", params={"q": "c2_beacon"})
    assert r.status_code == 200, r.text
    body = r.json()
    leak = [h for h in body["hits"]["hits"] if h["_source"].get("url.domain") == ATTACK_DOMAIN]
    assert body["hits"]["total"]["value"] == 0 and not leak, (
        "ROW 14 of the #435 limitation inventory LEAKS (docs/release-notes/v1.0.0.md): on a "
        "pre-v1.0 dynamically-mapped index the Student's free-text query 'c2_beacon' matched "
        "the indexed label inject_action:c2_beacon and singled out the inject event."
    )
