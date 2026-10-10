"""The greyspace_breadcrumb injector and validator (ADR 0007, plan slice 4)."""

from __future__ import annotations

import base64

import pytest
from scenario_engine import injectors as engine
from scenario_engine.injectors.greyspace_breadcrumb import GreyspaceBreadcrumbInjector, breadcrumb_token
from scenario_engine.validators.greyspace_breadcrumb import GreyspaceBreadcrumbValidator

CTX = engine.RangeContext(range_id="r-1", tenant_id="t-1", exercise_id="ex-1")
CRUMBS = [
    {"id": "note", "kind": "web", "site": "pastebox.io", "path": "/p/7f3k2.txt", "content": "token {{ token }} for {{ crumb_id }}"},
    {"id": "txt", "kind": "dns", "name": "cdn.update-cdn-sync.net", "type": "txt", "value": "{{ token }}"},
    {"id": "ioc", "kind": "threat_feed", "type": "domain", "indicator": "update-cdn-sync.net", "note": "C2 {{ exercise_id }}"},
]


def test_plant_renders_per_exercise_values_into_a_payload():
    res = engine.run_inject("inject.greyspace_breadcrumb", {"crumbs": CRUMBS, "salt": "s"}, CTX)
    assert res.success and res.execution_mode == "live"
    op = res.raw["greyspace"]
    assert op["operation"] == "plant" and op["exercise"] == "ex-1"
    web, dns, feed = op["payload"]["crumbs"]
    assert base64.b64decode(web["content_b64"]).decode() == f"token {breadcrumb_token('ex-1', 'note', 's')} for note"
    assert dns == {"id": "txt", "kind": "dns", "name": "cdn.update-cdn-sync.net", "type": "TXT",
                   "value": breadcrumb_token("ex-1", "txt", "s")}
    assert feed["note"] == "C2 ex-1"


def test_it_runs_on_the_mock_backend_and_says_nothing_observable():
    """Delivered by the worker's Greyspace seam (recorded on mock), so never skipped; and no
    event says where the breadcrumb is: Students search those."""
    res = engine.run_inject("greyspace_breadcrumb", {"crumbs": CRUMBS[:1]}, CTX, allow_host_effects=False)
    assert res.success and not res.skipped
    [event] = res.telemetry
    assert "pastebox" not in str({k: v for k, v in event.items() if k != engine.GROUND_TRUTH_FIELD})


def test_tokens_differ_per_exercise_and_salt():
    assert breadcrumb_token("ex-1", "note", "") != breadcrumb_token("ex-2", "note", "")
    assert breadcrumb_token("ex-1", "note", "a") != breadcrumb_token("ex-1", "note", "b")
    assert breadcrumb_token("ex-1", "note", "a").startswith("GS-")


def test_salt_from_the_environment(monkeypatch):
    monkeypatch.setenv("GREYSPACE_CRUMB_SALT", "envsalt")
    assert breadcrumb_token("e", "c") == breadcrumb_token("e", "c", "envsalt")


@pytest.mark.parametrize(
    "params",
    [
        {"crumbs": []},
        {"crumbs": [{"id": "bad id!", "kind": "web", "site": "a.com", "path": "/x", "content": "y"}]},
        {"crumbs": [{"id": "a", "kind": "smtp"}]},
        {"crumbs": [{"id": "a", "kind": "web", "site": "a.com", "path": "x", "content": "y"}]},
        {"crumbs": [{"id": "a", "kind": "dns", "name": "a.com", "type": "MX", "value": "y"}]},
        {"crumbs": [{"id": "a", "kind": "threat_feed", "type": "email", "indicator": "x"}]},
        {"crumbs": [CRUMBS[0], CRUMBS[0]]},
        {"operation": "wipe"},
    ],
)
def test_bad_params_fail_the_inject(params):
    res = engine.run_inject("greyspace_breadcrumb", params, CTX)
    assert not res.success and res.detail.startswith("Invalid params")


def test_remove_names_the_exercise_and_optional_ids():
    op = GreyspaceBreadcrumbInjector({"operation": "remove", "ids": ["note"]}).execute({"exercise_id": "ex-9"})
    assert op["greyspace"] == {"operation": "remove", "exercise": "ex-9", "ids": ["note"]}


def test_validator_recomputes_the_token():
    v = GreyspaceBreadcrumbValidator({"crumb_id": "note", "salt": "s"})
    token = breadcrumb_token("ex-1", "note", "s")
    assert v.validate({"exercise_id": "ex-1", "answer": f"found {token.lower()} in the paste"})
    assert not v.validate({"exercise_id": "ex-2", "answer": f"found {token}"})  # another exercise's value
    assert not v.validate({"exercise_id": "ex-1", "answer": ""})
    assert not GreyspaceBreadcrumbValidator({}).validate({"exercise_id": "ex-1", "answer": token})
