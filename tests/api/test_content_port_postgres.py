"""The content port on PostgreSQL (docs/content-port.md): the migrated schema, an export
and an import end to end, and two concurrent imports of the same bundle: one lands, the
other fails on the unique bundle id (the API answers it 409), never a second copy.

Runs only with TEST_POSTGRES_ADMIN_URL (CI's test-python job).
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime

import pytest
from _content_port_kit import DEV, proven_set
from app.auth import CurrentUser
from app.content_port import ContentPortImport, bundle, importer, service, signing
from app.content_port.schemas import PortItemRef
from app.models import Template, Tenant, User, UserRole
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.slow


def _admin(db) -> CurrentUser:
    db.add(Tenant(id=DEV, name="Default Org", slug="default"))
    u = User(id=uuid.uuid4(), email="port@example.test", display_name="Port", role=UserRole.admin, tenant_id=DEV,
             keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    db.add(u)
    db.flush()
    return CurrentUser(id=str(u.id), email=u.email, display_name=u.display_name, role=u.role, tenant_id=str(DEV),
                       keycloak_id=u.keycloak_id)


def test_concurrent_imports_of_one_bundle_land_once(postgres_engine, monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))
    monkeypatch.setenv(signing.TRUSTED_ENV, signing.public_info()["public_key"])
    site = uuid.uuid4()
    with Session(postgres_engine) as db:
        who = _admin(db)
        db.add(Tenant(id=site, name="New site", slug=f"new-{site.hex[:6]}"))
        _t, _s, c = proven_set(db)
        doc = service.export(db, who, [PortItemRef(item_type="course", item_id=c.id)])
        db.commit()
    raw = json.dumps(doc).encode()

    def plan_and_apply(db):
        parsed, b, kid = bundle.parse(raw, now=datetime.now(UTC))
        pl = importer.plan(db, parsed, b, kid, tenant_id=site, shared=False)
        assert pl.already_imported_at is None  # neither sees the other's uncommitted row
        importer.apply(db, None, pl, set(), now=datetime.now(UTC))

    first = Session(postgres_engine)
    plan_and_apply(first)  # flushed, not committed: holds the bundle id
    outcome: dict = {}

    def second():
        with Session(postgres_engine) as db:
            try:
                plan_and_apply(db)
                db.commit()
                outcome["result"] = "landed"
            except IntegrityError:
                db.rollback()
                outcome["result"] = "refused"

    t = threading.Thread(target=second)
    t.start()
    t.join(timeout=2)  # blocked on the unique index until the first commits
    assert t.is_alive()
    first.commit()
    first.close()
    t.join(timeout=30)
    assert outcome == {"result": "refused"}
    with Session(postgres_engine) as db:
        assert db.query(ContentPortImport).count() == 1
        assert db.query(Template).filter_by(tenant_id=site).count() == 1
