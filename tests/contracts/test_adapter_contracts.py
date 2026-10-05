"""Adapter contract suites — ADR 0001 rule 4.

One suite per adapter seam. Implementations are discovered from each registry dict at
collection time, so registering a new backend automatically enrols it in:

  (a) it is (or produces) a subclass of the seam's ABC,
  (b) it leaves no abstract method unimplemented, and every ABC method it overrides keeps
      the ABC's sync/async kind and accepts the ABC's parameters,
  (c) the seam's factory returns it for its registry key,

and the seam's null/mock/disabled implementation is driven end-to-end with the network
blocked (see conftest.py).

A backend that needs configuration just to be constructed declares the minimum in
``_CONSTRUCT_ENV`` — placeholders only, nothing is contacted.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Seam definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Seam:
    name: str
    abc: Callable[[], type]
    registry: Callable[[], Mapping[str, Any]]
    # (key, monkeypatch) -> instance, going through the production factory
    factory: Callable[[str, pytest.MonkeyPatch], object]
    null_key: str
    unknown_key_error: type[Exception]


def _auth_abc():
    from app.auth_backends import BaseAuthBackend

    return BaseAuthBackend


def _auth_registry():
    from app import auth_backends

    return auth_backends._REGISTRY


def _auth_factory(key, mp):
    from app import auth_backends

    mp.setenv("AUTH_DISABLED", "false")  # the legacy flag would short-circuit to 'disabled'
    mp.setenv("AUTH_BACKEND", key)
    auth_backends._reset_backend()
    try:
        return auth_backends.get_auth_backend()
    finally:
        auth_backends._reset_backend()


def _lms_abc():
    from app.lms import BaseLMSBackend

    return BaseLMSBackend


def _lms_registry():
    from app import lms

    return lms._REGISTRY


def _lms_factory(key, mp):
    from app import lms

    mp.setenv("LMS_BACKEND", key)
    lms._reset_backend()
    try:
        return lms.get_lms_backend()
    finally:
        lms._reset_backend()


def _search_abc():
    from app.search_backends import BaseSearchBackend

    return BaseSearchBackend


def _search_registry():
    from app import search_backends

    return search_backends._REGISTRY


def _search_factory(key, mp):
    from app import search_backends

    mp.setenv("SEARCH_BACKEND", key)
    search_backends._reset_backend()
    try:
        return search_backends.get_search_backend()
    finally:
        search_backends._reset_backend()


def _vector_abc():
    from app.vector_backends import BaseVectorStore

    return BaseVectorStore


def _vector_registry():
    from app import vector_backends

    return vector_backends._REGISTRY


def _vector_factory(key, mp):
    from app import vector_backends

    mp.setenv("VECTOR_BACKEND", key)
    vector_backends.reset_vector_store()
    try:
        return vector_backends.get_vector_store()
    finally:
        vector_backends.reset_vector_store()


def _notif_abc():
    # NB: app.notifications.NotificationChannel is the legacy *enum* from _service.py;
    # the channel ABC lives in notifications/base.py.
    from app.notifications.base import NotificationChannel

    return NotificationChannel


def _notif_registry():
    from app.notifications import registry

    return registry._registry


def _notif_factory(key, mp):
    from app.notifications.registry import get_channel

    return get_channel(key)


def _prov_abc():
    from worker.provisioners.base import BaseProvisioner

    return BaseProvisioner


def _prov_registry():
    from worker import provisioners

    return provisioners._REGISTRY


def _prov_factory(key, mp):
    from worker.provisioners import get_provisioner

    return get_provisioner(key)


def _ai_abc():
    from _ai_orch.backends import BaseAIBackend

    return BaseAIBackend


def _ai_registry():
    import _ai_orch.backends as ai

    return ai._REGISTRY


def _ai_factory(key, mp):
    import _ai_orch.backends as ai

    ai._reset_backends()
    try:
        return ai.get_cloud_backend(key)
    finally:
        ai._reset_backends()


def _moodle_abc():
    from app.moodle_backends import BaseMoodleBackend

    return BaseMoodleBackend


def _moodle_registry():
    from app import moodle_backends

    return moodle_backends._REGISTRY


def _moodle_factory(key, mp):
    from app.moodle_backends import get_moodle_backend

    return get_moodle_backend(key)


SEAMS: dict[str, Seam] = {
    s.name: s
    for s in (
        Seam("auth", _auth_abc, _auth_registry, _auth_factory, "disabled", ValueError),
        Seam("lms", _lms_abc, _lms_registry, _lms_factory, "null", ValueError),
        Seam("search", _search_abc, _search_registry, _search_factory, "null", ValueError),
        Seam("vector", _vector_abc, _vector_registry, _vector_factory, "null", ValueError),
        Seam("notifications", _notif_abc, _notif_registry, _notif_factory, "in_app", KeyError),
        Seam("provisioners", _prov_abc, _prov_registry, _prov_factory, "mock", ValueError),
        Seam("ai", _ai_abc, _ai_registry, _ai_factory, "mock", ValueError),
        Seam("moodle", _moodle_abc, _moodle_registry, _moodle_factory, "fake", ValueError),
    )
}

# Minimum configuration some backends refuse to construct without. Placeholders only.
_CONSTRUCT_ENV: dict[tuple[str, str], dict[str, str]] = {
    ("auth", "generic_oidc"): {"OIDC_JWKS_URL": "https://idp.invalid/.well-known/jwks.json"},
}


def _implementations() -> list:
    return [
        pytest.param(seam, key, id=f"{seam.name}:{key}") for seam in SEAMS.values() for key in sorted(seam.registry())
    ]


def _build(seam: Seam, key: str, mp: pytest.MonkeyPatch) -> object:
    for var, value in _CONSTRUCT_ENV.get((seam.name, key), {}).items():
        mp.setenv(var, value)
    return seam.factory(key, mp)


def _abc_methods(abc: type) -> dict[str, Callable]:
    """Public callables the ABC defines (abstract or concrete-with-default)."""
    return {
        name: member for name, member in vars(abc).items() if not name.startswith("_") and inspect.isfunction(member)
    }


# ---------------------------------------------------------------------------
# Every registered implementation, every seam
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seam", list(SEAMS.values()), ids=list(SEAMS))
def test_registry_is_not_empty_and_has_a_null_backend(seam):
    registry = seam.registry()
    assert registry, f"{seam.name} registry is empty"
    assert seam.null_key in registry, f"{seam.name} registry has no offline backend {seam.null_key!r}"


@pytest.mark.parametrize("seam", list(SEAMS.values()), ids=list(SEAMS))
def test_factory_rejects_unknown_key(seam, monkeypatch):
    with pytest.raises(seam.unknown_key_error):
        seam.factory("no-such-backend", monkeypatch)


@pytest.mark.parametrize(("seam", "key"), _implementations())
def test_registered_class_subclasses_abc(seam, key):
    """(a) Class entries subclass the ABC. Factory callables are checked via their product."""
    entry = seam.registry()[key]
    abc = seam.abc()
    if isinstance(entry, type):
        assert issubclass(entry, abc), f"{entry.__name__} is not a {abc.__name__}"
    else:
        assert callable(entry), f"{seam.name}:{key} registry entry is neither a class nor a factory"


@pytest.mark.parametrize(("seam", "key"), _implementations())
def test_factory_returns_the_registered_implementation(seam, key, monkeypatch):
    """(c) The production factory hands back this key's implementation."""
    entry = seam.registry()[key]
    instance = _build(seam, key, monkeypatch)
    assert isinstance(instance, seam.abc())
    if isinstance(entry, type):
        assert type(instance) is entry, f"factory returned {type(instance).__name__} for {key!r}"


@pytest.mark.parametrize(("seam", "key"), _implementations())
def test_implements_every_abstract_method_with_compatible_signature(seam, key, monkeypatch):
    """(b) Nothing left abstract; overrides keep the ABC's async-ness and parameters."""
    impl = type(_build(seam, key, monkeypatch))
    abc = seam.abc()

    assert not inspect.isabstract(impl), f"{impl.__name__} leaves abstract: {sorted(impl.__abstractmethods__)}"
    for name in abc.__abstractmethods__:
        assert getattr(impl, name) is not getattr(abc, name), f"{impl.__name__}.{name} is not implemented"

    for name, abc_fn in _abc_methods(abc).items():
        impl_fn = getattr(impl, name)
        assert inspect.iscoroutinefunction(impl_fn) == inspect.iscoroutinefunction(abc_fn), (
            f"{impl.__name__}.{name} must be {'async' if inspect.iscoroutinefunction(abc_fn) else 'sync'} like the ABC"
        )
        impl_sig = inspect.signature(impl_fn)
        takes_kwargs = any(p.kind is p.VAR_KEYWORD for p in impl_sig.parameters.values())
        for param in list(inspect.signature(abc_fn).parameters)[1:]:  # skip self
            assert param in impl_sig.parameters or takes_kwargs, (
                f"{impl.__name__}.{name} does not accept the ABC parameter {param!r}"
            )


# ---------------------------------------------------------------------------
# Null / mock / disabled implementations, end to end, offline
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_null_seams_are_all_exercised():
    # Adding a seam to SEAMS without an end-to-end test below must fail loudly.
    exercised = {name.removeprefix("test_null_") for name in globals() if name.startswith("test_null_")}
    exercised.discard("seams_are_all_exercised")
    assert exercised == set(SEAMS), f"seams without an end-to-end null test: {set(SEAMS) - exercised}"


def test_null_auth(monkeypatch):
    from fastapi import HTTPException

    backend = _build(SEAMS["auth"], SEAMS["auth"].null_key, monkeypatch)
    assert _run(backend.health_check()) is True
    # ABC contract: validation failures surface as HTTPException, never a bare error.
    with pytest.raises(HTTPException):
        _run(backend.validate_token("any.jwt.value"))


def test_null_lms(monkeypatch):
    from app.xapi import build_statement

    backend = _build(SEAMS["lms"], SEAMS["lms"].null_key, monkeypatch)
    stmt = build_statement("launched", "s@example.mil", "Student", "exercise", "ex-1", "Drill")

    assert _run(backend.emit_statement(stmt)) is True
    accepted = _run(backend.emit_statements([stmt, stmt]))
    assert isinstance(accepted, int) and 0 <= accepted <= 2
    assert _run(backend.emit_statements([])) == 0
    assert backend.emit_statement_sync(stmt) is True
    assert backend.emit_statement_sync(stmt, timeout=0.1) is True
    assert _run(backend.health_check()) is True


def test_null_search(monkeypatch):
    backend = _build(SEAMS["search"], SEAMS["search"].null_key, monkeypatch)

    accepted = _run(backend.ingest("telemetry-test", [{"msg": "a"}, {"msg": "b"}]))
    assert isinstance(accepted, int) and 0 <= accepted <= 2
    assert _run(backend.ingest("telemetry-test", [])) == 0
    result = _run(backend.search("telemetry-test", "msg:a", size=5))
    assert isinstance(result, dict)
    assert result["hits"]["hits"] == []
    assert result["hits"]["total"]["value"] == 0
    assert _run(backend.health_check()) is True


def test_null_vector(monkeypatch):
    store = _build(SEAMS["vector"], SEAMS["vector"].null_key, monkeypatch)
    idx = "curriculum-test"

    assert _run(store.index_dim(idx)) is None
    assert _run(store.search(idx, text="anything", vector=None, k=3)) == []  # missing index: [] not raise
    assert _run(store.create_index(idx, 384)) is True
    assert _run(store.create_index(idx, 384)) is False
    assert _run(store.index_dim(idx)) == 384
    docs = [{"text": "lateral movement via smb", "filename": "a.pdf", "chunk_ordinal": 0}]
    assert _run(store.bulk_index(idx, docs)) == 1
    hits = _run(store.search(idx, text="smb", vector=None, k=3))
    assert [set(h) for h in hits] == [{"text", "filename", "chunk_ordinal", "score"}]
    _run(store.delete_index(idx))
    _run(store.delete_index(idx))  # missing is not an error
    assert _run(store.index_dim(idx)) is None


def test_null_notifications(monkeypatch):
    # in_app with no DB session factory and no WS manager does no I/O at all.
    channel = _build(SEAMS["notifications"], SEAMS["notifications"].null_key, monkeypatch)

    assert _run(channel.send("s@example.mil", "Subject", "Body")) is True
    assert _run(channel.send("s@example.mil", "Subject", "Body", metadata={"k": "v"})) is True
    results = _run(channel.send_batch(["a@example.mil", "b@example.mil"], "Subject", "Body"))
    assert results == {"a@example.mil": True, "b@example.mil": True}
    assert _run(channel.send_batch([], "Subject", "Body")) == {}


def test_null_provisioners(monkeypatch):
    from worker.provisioners import mock as mock_mod
    from worker.provisioners.results import (
        DestroyResult,
        HealthResult,
        ProvisionResult,
        RestoreResult,
        SnapshotDeleteResult,
        SnapshotResult,
        StartResult,
        StopResult,
    )

    monkeypatch.setattr(mock_mod, "MOCK_PROVISION_DELAY", 0.0)
    monkeypatch.setattr(mock_mod, "MOCK_FAILURE_RATE", 0.0)
    prov = _build(SEAMS["provisioners"], SEAMS["provisioners"].null_key, monkeypatch)
    rid = "range-contract-1"
    template = {"vms": [{"name": "dc01"}, {"name": "ws01", "cpu": 4}]}

    provisioned = _run(prov.provision(rid, template, {}))
    assert isinstance(provisioned, ProvisionResult) and provisioned.status == "ok"
    assert [vm["name"] for vm in provisioned.vms] == ["dc01", "ws01"]
    output = {"vms": provisioned.vms, "networks": provisioned.networks}

    health = _run(prov.health_check(rid, output))
    assert isinstance(health, HealthResult) and health.healthy is True

    stopped = _run(prov.stop(rid, output))
    assert isinstance(stopped, StopResult) and stopped.vms_stopped == 2
    assert _run(prov.health_check(rid, output)).healthy is False

    started = _run(prov.start(rid, output))
    assert isinstance(started, StartResult) and started.vms_started == 2

    snap = _run(prov.snapshot(rid, output, "baseline"))
    assert isinstance(snap, SnapshotResult) and (snap.status, snap.snapshot_name, snap.vms_snapped) == (
        "ok",
        "baseline",
        2,
    )

    restored = _run(prov.restore(rid, output, "baseline", power_on=True))
    assert isinstance(restored, RestoreResult) and restored.status == "ok" and restored.vms_restored == 2

    deleted = _run(prov.delete_snapshot(rid, output, "baseline"))
    assert isinstance(deleted, SnapshotDeleteResult) and deleted.status == "ok" and deleted.vms_cleaned == 2

    destroyed = _run(prov.destroy(rid, output))
    assert isinstance(destroyed, DestroyResult) and destroyed.status == "ok"
    assert destroyed.resources_removed == len(provisioned.vms) + len(provisioned.networks)
    assert _run(prov.health_check(rid, output)).healthy is False


def test_null_ai(monkeypatch):
    backend = _build(SEAMS["ai"], SEAMS["ai"].null_key, monkeypatch)

    text, model, usage = _run(backend.generate("Summarise the AAR", max_tokens=50))
    assert isinstance(text, str) and text
    assert isinstance(model, str) and model
    assert isinstance(usage, dict)
    _, named_model, _ = _run(backend.generate("x", model="explicit-model", system_prompt="be brief"))
    assert named_model == "explicit-model"
    # Empty prompt is the zero path; it must still return the documented triple.
    empty = _run(backend.generate(""))
    assert isinstance(empty, tuple) and len(empty) == 3
    assert _run(backend.health_check()) is True


def test_null_moodle(monkeypatch):
    from types import SimpleNamespace

    from app.moodle_backends import MoodleError, fake

    fake.reset()
    backend = _build(SEAMS["moodle"], SEAMS["moodle"].null_key, monkeypatch)
    site = SimpleNamespace(lti_issuer="http://moodle.invalid", base_url="http://moodle.invalid")
    quiz = {"idnumber": "tn:m1:quiz:h", "type": "quiz", "name": "Q", "questions": [{"text": "t"}]}
    payload = {
        "idnumber": "tn-stage:r1",
        "fullname": "C1",
        "shortname": "C1",
        "visible": False,
        "category": {"idnumber": "tn-catalogue", "name": "x"},
        "sections": [{"name": "1", "activities": [quiz]}],
    }
    first = backend.upsert_course(site, payload)
    assert first["created"] is True and set(first["activities"]) == {"tn:m1:quiz:h"}
    assert backend.upsert_course(site, payload)["created"] is False
    described = backend.describe_course(site, "tn-stage:r1")
    assert described["activities"]["tn:m1:quiz:h"]["questions"] == 1 and described["visible"] == 0
    assert backend.set_visible(site, "tn-stage:r1", True)["courseid"] == first["courseid"]
    with pytest.raises(MoodleError):
        backend.delete_stage(site, "a-live-course")
    assert backend.delete_stage(site, "tn-stage:r1") == {"deleted": True}
    assert backend.describe_course(site, "tn-stage:r1") == {"exists": False}
