"""The worker's Greyspace seam (worker/greyspace.py, ADR 0007) against the API's own schema.

On the mock backend a block is recorded as deployed; on any backend without a Greyspace
host deployer (vSphere today) it is pending infrastructure. A template's ``greyspace:``
block attaches itself. Nothing here may ever fail a range build.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import app.greyspace.models  # noqa: F401 — registers range_greyspace
import pytest
from app import models as m
from app.db import Base
from app.greyspace.models import RangeGreyspace
from app.greyspace.schemas import GreyspaceBlock
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

tasks = pytest.importorskip("worker.tasks")
from worker import greyspace  # noqa: E402


@pytest.fixture
def factory(monkeypatch):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    make = sessionmaker(bind=eng, class_=Session, expire_on_commit=False)

    @contextmanager
    def _session():
        s = make()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(tasks, "_db_session", _session)
    yield make
    eng.dispose()


@pytest.fixture
def rng(factory):
    db = factory()
    tenant = m.Tenant(name="gs", slug=f"gs-{uuid.uuid4().hex[:6]}")
    db.add(tenant)
    db.commit()
    tpl = m.Template(name="tpl", yaml="name: tpl\n", tenant_id=tenant.id)
    db.add(tpl)
    db.commit()
    r = m.Range(name="r", template_id=tpl.id, tenant_id=tenant.id, provisioner_backend="mock", state=m.RangeState.ready)
    db.add(r)
    db.commit()
    db.close()
    return r


def _attach(factory, rng, **block):
    db = factory()
    db.add(RangeGreyspace(range_id=rng.id, tenant_id=rng.tenant_id, block=GreyspaceBlock(**block).model_dump(),
                          corpus_tier="t0", status="configured"))
    db.commit()
    db.close()


def _row(factory, rng) -> RangeGreyspace | None:
    db = factory()
    try:
        return db.get(RangeGreyspace, rng.id)
    finally:
        db.close()


def test_no_block_means_nothing_happens(factory, rng):
    assert greyspace.after_provision(str(rng.id), "mock", {"name": "plain"}) is None
    assert _row(factory, rng) is None


def test_mock_backend_records_the_block_as_deployed(factory, rng):
    _attach(factory, rng, threat_infra=False)
    assert greyspace.after_provision(str(rng.id), "mock", {}) == "deployed"
    row = _row(factory, rng)
    assert row.status == "deployed" and row.deployed_at is not None
    assert "webfarm" in row.detail["services"] and "threat" not in row.detail["services"]


def test_vsphere_is_pending_infrastructure_with_the_todo(factory, rng):
    _attach(factory, rng)
    assert greyspace.after_provision(str(rng.id), "vsphere_api", {}) == "pending_infrastructure"
    row = _row(factory, rng)
    assert row.status == "pending_infrastructure" and "gs-core" in row.detail["todo"]
    assert row.deployed_at is None


def test_a_templates_block_attaches_itself(factory, rng):
    template = {"name": "t", "greyspace": {"site_packs": ["news"], "bogus": 1}}
    assert greyspace.after_provision(str(rng.id), "mock", template) == "deployed"
    row = _row(factory, rng)
    assert row.block["site_packs"] == ["news"] and "bogus" not in row.block
    assert row.tenant_id == rng.tenant_id
    GreyspaceBlock.model_validate(row.block)  # what the worker writes, the API can read


def test_an_attached_block_wins_over_the_template(factory, rng):
    _attach(factory, rng, site_packs=["dev"])
    greyspace.after_provision(str(rng.id), "mock", {"greyspace": {"site_packs": ["news"]}})
    assert _row(factory, rng).block["site_packs"] == ["dev"]


def test_destroy_returns_it_to_configured(factory, rng):
    _attach(factory, rng)
    greyspace.after_provision(str(rng.id), "mock", {})
    greyspace.after_destroy(str(rng.id))
    row = _row(factory, rng)
    assert row.status == "configured" and row.deployed_at is None


def test_never_raises_into_the_range_task(monkeypatch, rng):
    @contextmanager
    def _broken():
        raise RuntimeError("database down")
        yield  # pragma: no cover

    monkeypatch.setattr(tasks, "_db_session", _broken)
    assert greyspace.after_provision(str(rng.id), "mock", {"greyspace": {}}) is None
    greyspace.after_destroy(str(rng.id))  # no exception


def test_unknown_range_with_a_template_block_is_ignored(factory):
    assert greyspace.after_provision(str(uuid.uuid4()), "mock", {"greyspace": {}}) is None


# -- Through the real range tasks (tasks.provision_range / destroy_range call the seam) --
def _fake_backend(**methods):
    from unittest.mock import AsyncMock, MagicMock

    backend = MagicMock()
    for name, value in methods.items():
        setattr(backend, name, AsyncMock(return_value=value))
    return backend


def _set_state(factory, rng, state):
    db = factory()
    db.get(m.Range, rng.id).state = state
    db.commit()
    db.close()


def test_provision_range_deploys_the_block_and_destroy_range_undeploys_it(factory, rng, monkeypatch):
    from unittest.mock import MagicMock

    from worker.provisioners.results import DestroyResult, ProvisionResult

    monkeypatch.setattr(tasks, "_notify_api", MagicMock())
    backend = _fake_backend(
        provision=ProvisionResult(status="ok", vms=[], networks=[]),
        destroy=DestroyResult(status="ok", resources_removed=0),
    )
    monkeypatch.setattr(tasks, "_get_backend", lambda name=None: backend)
    _attach(factory, rng)
    _set_state(factory, rng, m.RangeState.provisioning)

    assert tasks.provision_range(str(rng.id))["status"] == "ready"
    row = _row(factory, rng)
    assert row.status == "deployed" and row.detail["backend"] == "mock"

    _set_state(factory, rng, m.RangeState.destroying)
    assert tasks.destroy_range(str(rng.id))["status"] == "destroyed"
    assert _row(factory, rng).status == "configured"


def test_provision_range_without_a_block_leaves_no_row(factory, rng, monkeypatch):
    from unittest.mock import MagicMock

    from worker.provisioners.results import ProvisionResult

    monkeypatch.setattr(tasks, "_notify_api", MagicMock())
    monkeypatch.setattr(tasks, "_get_backend", lambda name=None: _fake_backend(
        provision=ProvisionResult(status="ok", vms=[], networks=[])))
    _set_state(factory, rng, m.RangeState.provisioning)
    assert tasks.provision_range(str(rng.id))["status"] == "ready"
    assert _row(factory, rng) is None


def test_template_block_defaults():
    assert greyspace.template_block(None) is None
    assert greyspace.template_block({"greyspace": "yes"}) is None
    assert greyspace.template_block({"greyspace": {}}) == greyspace.DEFAULT_BLOCK
    # The worker's defaults are the API's defaults.
    assert GreyspaceBlock().model_dump() == greyspace.DEFAULT_BLOCK
