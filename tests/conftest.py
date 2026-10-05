"""Shared pytest fixtures for TrueNorth Range tests."""

import os
import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

# ---- environment overrides MUST come before any app imports ----
os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("DATABASE_URL", "sqlite://")
# Never a real Redis: a developer's machine usually runs the dev stack's on 6379, and the
# suite used to publish real Celery messages into it (db 15). Port 1 refuses at once.
# Tests that need a real Redis name their own with TEST_REDIS_URL.
UNREACHABLE_REDIS = "redis://127.0.0.1:1/15"
os.environ["REDIS_URL"] = UNREACHABLE_REDIS
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("RANGE_OP_REDISPATCH_SECONDS", "0")  # tests call range_ops.redispatch_pending
os.environ.setdefault("WS_EVENTS_ENABLED", "false")  # no Redis subscription from the test process

from app.db import Base, get_db
from app.main import app as fastapi_app


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: mark test as slow-running")
    config.addinivalue_line("markers", "integration: mark test as integration (needs live services)")


@pytest.fixture(scope="session")
def engine():
    """In-memory SQLite engine for tests (no Postgres required)."""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture
def db_session(engine) -> Generator[Session, None, None]:
    """Yield a DB session, rolled back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(db_session) -> TestClient:
    """TestClient with overridden DB dependency."""

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    with TestClient(fastapi_app) as c:
        yield c
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
def sample_tenant_id() -> str:
    return str(uuid.uuid4())


class _NoBroker:
    """What a test sees instead of a broker: every send is recorded, nothing leaves."""

    def __init__(self):
        self.sent: list[tuple[str, list]] = []

    def send_task(self, app, name, args=None, kwargs=None, **options):
        from types import SimpleNamespace

        self.sent.append((name, list(args or [])))
        return SimpleNamespace(id=f"test-task-{len(self.sent)}")


@pytest.fixture(autouse=True)
def no_real_broker(monkeypatch):
    """Celery's ``send_task`` (the API's ``celery_client.dispatch`` and a worker task's
    ``apply_async`` both go through it) records instead of publishing. A test that wants
    to see what was sent takes this fixture; one that mocks ``send_task`` on an app, or
    ``celery_client.dispatch``, still wins (instance attributes shadow this class patch)."""
    from celery import Celery

    broker = _NoBroker()
    monkeypatch.setattr(Celery, "send_task", lambda app, name, *a, **k: broker.send_task(app, name, *a, **k))
    return broker
