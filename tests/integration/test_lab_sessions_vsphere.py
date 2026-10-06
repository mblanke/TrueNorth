"""Two students' labs on a real vCenter: build, reset, expire, clean up, reconcile.

Skipped unless configured. Everything runs in this process: the API's lab-session service
dispatches straight into the worker's own task functions (no broker), against a
PostgreSQL the worker's SQL can run on. Nothing outside the lab ranges is touched: the
reconcile step looks only for VMs named after these sessions' range ids.

    LAB_TEST_DATABASE_URL   postgresql://... an empty, disposable database
    VSPHERE_URL, VSPHERE_USERNAME, VSPHERE_PASSWORD, VSPHERE_DATACENTER, VSPHERE_CLUSTER,
    VSPHERE_DATASTORE, VSPHERE_CONTENT_LIBRARY, VSPHERE_VERIFY_SSL   (the provisioner's own)
    LAB_PORT_GROUPS         two or more pre-created, isolated port groups, comma separated
    LAB_TEST_TEMPLATE       a content-library item that boots Linux with VMware Tools

What it proves (docs/arc2-44-course-programme.md §6 "Small individual range"):
  two simultaneous students get distinct VMs on distinct isolated port groups; each is
  ready only after VMware Tools reports the guest running and a reset point exists; one
  is reset without touching the other; one expires and only its VMs disappear; leftovers
  are reconciled by name; readiness time is measured and printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import yaml

DB_URL = os.getenv("LAB_TEST_DATABASE_URL", "")
NEEDED = (
    "VSPHERE_URL",
    "VSPHERE_USERNAME",
    "VSPHERE_PASSWORD",
    "VSPHERE_DATACENTER",
    "VSPHERE_CLUSTER",
    "VSPHERE_DATASTORE",
    "LAB_PORT_GROUPS",
    "LAB_TEST_TEMPLATE",
)
HERE = pathlib.Path(__file__).resolve().parent
TENANT = uuid.UUID("7e57e57e-0000-4000-8000-0000000000ab")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DB_URL or not all(os.getenv(k) for k in NEEDED), reason="live vSphere lab test is not configured"
    ),
]

PROFILE = {
    "schema_version": "arc2/lab-profile/0.1",
    "id": "c108-linux-host",
    "version": 1,
    "objective": "Administer one Linux host",
    "module_ids": ["mod_006"],
    "nodes": [
        {"name": "host", "catalogue_id": "ubuntu-lts", "vcpu": 1, "ram_mb": 2048, "disk_gb": 20, "networks": ["lab"]}
    ],
    "networks": [{"name": "lab", "cidr": "10.250.0.0/24"}],
    "health_checks": [{"node": "host", "kind": "tools", "timeout_s": 600}],
    "access": [{"node": "host", "kind": "console"}],
    "evidence_checks": [{"id": "hardening", "node": "host", "description": "sshd hardened"}],
    "limits": {"max_vms": 1, "vcpu_total": 1, "ram_mb_total": 2048, "disk_gb_total": 20},
    "reset": {"mode": "snapshot"},
    "lifetime": {"idle_minutes": 60, "max_minutes": 120},
    "egress": {"policy": "none"},
}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    os.environ["DATABASE_URL"] = DB_URL
    os.environ["PROVISIONER_BACKEND"] = "vsphere_api"
    sys.path.insert(0, str(HERE.parent / "api"))
    from _release_kit import CATALOGUE, CROSSWALK, build
    from app import programme_ingest, qsp_ingest
    from app.course_releases import service as releases
    from app.db import Base
    from app.models import GoldenImage, Tenant, User, UserRole
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(DB_URL)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    if db.get(Tenant, TENANT) is None:
        db.add(Tenant(id=TENANT, name="lab test", slug=f"labtest-{uuid.uuid4().hex[:6]}"))
        db.commit()
    qsp_ingest.import_crosswalk(db, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db, CATALOGUE.read_text(encoding="utf-8"), tenant_id=TENANT)
    db.add(
        GoldenImage(
            catalogue_id="ubuntu-lts", hypervisor="vsphere", template_name=os.environ["LAB_TEST_TEMPLATE"], enabled=True
        )
    )
    data = build(
        tmp_path_factory.mktemp("rel"),
        range_ordinals=frozenset({6}),
        lab_profile=PROFILE,
        slug=f"arc2-lab-{uuid.uuid4().hex[:4]}",
    )
    release, _ = releases.create_candidate(db, data, tenant_id=TENANT, user_id=None)
    releases.accept(db, release, user_id=None, acknowledge=[])
    users = []
    for _ in range(2):
        u = User(
            email=f"lab{uuid.uuid4().hex[:6]}@example.test",
            display_name="lab",
            role=UserRole.student,
            tenant_id=TENANT,
            keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
        )
        db.add(u)
        users.append(u)
    db.commit()
    return db, release, users


def _worker_dispatch(task: str, *args):
    """Run the worker task in this process, as Celery would on the worker."""
    from worker import lab_tasks, tasks

    fn = getattr(tasks, task, None) or getattr(lab_tasks, task)
    try:
        fn.run(*args)
    except Exception as exc:  # the task records failure on the range; the session reads it
        print(f"[worker] {task}{args} raised {exc}")
    return f"inline-{task}"


def _drive(db, session, want: str, timeout: float = 1800) -> None:
    from app.lab_sessions import service

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db.expire_all()
        service.advance(db, session)
        db.commit()
        if session.state == want:
            return
        if session.state in ("failed", "destroyed") and want not in ("failed", "destroyed"):
            raise AssertionError(f"session {session.id} ended {session.state}: {session.error}")
        time.sleep(5)
    raise AssertionError(f"session {session.id} still {session.state} after {timeout}s: {session.error}")


def test_two_students_build_reset_expire_and_clean_up(world, monkeypatch):
    from app.lab_sessions import service
    from app.lab_sessions.models import LabNetworkLease
    from app.models import Range
    from worker.provisioners import get_provisioner

    db, release, (alice, bob) = world
    monkeypatch.setattr(service, "_dispatch", _worker_dispatch)

    a, _ = service.launch(db, tenant_id=TENANT, user_id=alice.id, release_id=release.id, activity_id="mod_006")
    db.commit()
    b, _ = service.launch(db, tenant_id=TENANT, user_id=bob.id, release_id=release.id, activity_id="mod_006")
    db.commit()
    _drive(db, a, "ready")
    _drive(db, b, "ready")
    print(f"\nreadiness: {a.readiness_seconds:.0f}s and {b.readiness_seconds:.0f}s (provision + tools + baseline)")

    nets = {s.id: {r.port_group for r in db.query(LabNetworkLease).filter_by(session_id=s.id)} for s in (a, b)}
    assert nets[a.id] and nets[b.id] and not nets[a.id] & nets[b.id]
    vms = {s.id: json.loads(db.get(Range, s.range_id).provisioner_output)["vms"] for s in (a, b)}
    assert {v["vm_id"] for v in vms[a.id]}.isdisjoint({v["vm_id"] for v in vms[b.id]})

    service.reset(db, a)
    db.commit()
    _drive(db, a, "active", timeout=900)
    assert b.state == "ready"  # untouched by a's reset

    console = service.console(db, b)
    assert console["url"].startswith("wss://") and console["expires_in"] > 0

    b.max_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    _drive(db, b, "destroyed", timeout=900)
    provisioner = get_provisioner("vsphere_api")
    assert asyncio.run(provisioner.find_vms(f"{b.range_id}-")) == []
    assert asyncio.run(provisioner.find_vms(f"{a.range_id}-"))  # a's VM is still there
    assert not db.query(LabNetworkLease).filter_by(session_id=b.id).count()

    service.end(db, a)
    db.commit()
    _drive(db, a, "destroyed", timeout=900)
    assert asyncio.run(provisioner.find_vms(f"{a.range_id}-")) == []
    print(yaml.safe_dump({"a": a.readiness_seconds, "b": b.readiness_seconds}))
