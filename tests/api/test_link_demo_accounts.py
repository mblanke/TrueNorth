"""app.link_demo_accounts: demo roster rows get their Keycloak subject, and nothing else does."""

from __future__ import annotations

import uuid

import pytest
from app import link_demo_accounts as lda
from app.models import Base, Tenant, User, UserRole
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    tenant = Tenant(id=uuid.uuid4(), name="T", slug="t")
    session.add(tenant)
    session.add(
        User(
            id=uuid.uuid4(),
            keycloak_id="placeholder-1",
            email="trainee1.demo@truenorth.test",
            display_name="Demo Trainee 1",
            role=UserRole.student,
            tenant_id=tenant.id,
        )
    )
    session.commit()
    yield session
    session.close()
    eng.dispose()


def test_links_the_row_and_keeps_its_role(db) -> None:
    assert lda.link(db, "trainee1.demo@truenorth.test", "kc-sub-1") == "linked"
    user = db.query(User).filter(User.email == "trainee1.demo@truenorth.test").one()
    assert user.keycloak_id == "kc-sub-1" and user.role == UserRole.student


def test_rerun_changes_nothing(db) -> None:
    lda.link(db, "trainee1.demo@truenorth.test", "kc-sub-1")
    assert lda.link(db, "trainee1.demo@truenorth.test", "kc-sub-1") == "unchanged"


def test_missing_row_is_reported_not_created(db) -> None:
    assert lda.link(db, "trainee2.demo@truenorth.test", "kc-sub-2") == "no roster row"
    assert db.query(User).filter(User.email == "trainee2.demo@truenorth.test").first() is None


def test_a_subject_already_in_use_is_refused(db) -> None:
    db.add(
        User(
            id=uuid.uuid4(),
            keycloak_id="kc-sub-1",
            email="someone@example.test",
            display_name="Someone",
            tenant_id=db.query(Tenant).one().id,
        )
    )
    db.commit()
    assert lda.link(db, "trainee1.demo@truenorth.test", "kc-sub-1") == "subject in use"


@pytest.mark.parametrize(
    ("email", "ok"),
    [
        ("instructor.demo@truenorth.test", True),
        ("observer.demo@truenorth.test", True),
        ("maj.smith@corp.tnrange.lab", False),
        (".demo@truenorth.test", False),
        ("x.demo@truenorth.test.evil", False),
    ],
)
def test_only_demo_addresses(email: str, ok: bool) -> None:
    assert lda.is_demo_email(email) is ok


def test_main_refuses_a_non_demo_address(monkeypatch) -> None:
    monkeypatch.setenv("TN_DEMO_EMAILS", "trainee1.demo@truenorth.test,maj.smith@corp.tnrange.lab")
    monkeypatch.setattr(lda, "_keycloak_admin_token", lambda *a: pytest.fail("must not reach Keycloak"))
    assert lda.main() == 2
