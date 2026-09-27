"""DB_AUTO_CREATE and SEED_DEV_DATA must do what the installer says they do.

compose.prod.yml, the installer and app/bootstrap_admin.py all rely on these two
flags being false in production: Alembic owns the schema there, and the hardcoded
admin@truenorth.local must not exist. Until 2026-09-27 app/main.py read neither,
so every production start ran create_all() and minted that admin into an empty
tenants table.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from app import main
from app.models import Base, Tenant, User
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def fresh_db():
    """An empty schema, so the seed sees the first-start state it guards."""
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng)
    with patch("app.db.SessionLocal", factory):
        yield factory
    eng.dispose()


class TestEnvFlag:
    def test_on_by_default(self, monkeypatch):
        monkeypatch.delenv("SEED_DEV_DATA", raising=False)
        assert main._env_flag("SEED_DEV_DATA") is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", " false "])
    def test_false_values(self, monkeypatch, value):
        monkeypatch.setenv("SEED_DEV_DATA", value)
        assert main._env_flag("SEED_DEV_DATA") is False

    @pytest.mark.parametrize("value", ["true", "1", "yes"])
    def test_true_values(self, monkeypatch, value):
        monkeypatch.setenv("SEED_DEV_DATA", value)
        assert main._env_flag("SEED_DEV_DATA") is True


class TestSeed:
    def test_production_start_mints_no_admin_and_no_tenant(self, fresh_db):
        main._seed_dev_data(dev_account=False)
        with fresh_db() as db:
            assert db.query(User).filter(User.email == "admin@truenorth.local").count() == 0
            # seed_infrastructure used to mint a "Dev Tenant" here instead
            assert db.query(Tenant).count() == 0

    def test_development_start_still_gets_the_dev_admin(self, fresh_db):
        main._seed_dev_data(dev_account=True)
        with fresh_db() as db:
            admin = db.query(User).filter(User.email == "admin@truenorth.local").one()
            assert str(admin.id) == "00000000-0000-0000-0000-000000000001"


class TestLifespan:
    def _start(self):
        async def run():
            async with main.lifespan(main.app):
                pass

        asyncio.run(run())

    def test_production_flags_skip_create_all_and_the_dev_account(self, monkeypatch):
        monkeypatch.setenv("DB_AUTO_CREATE", "false")
        monkeypatch.setenv("SEED_DEV_DATA", "false")
        with patch.object(main.Base.metadata, "create_all") as create_all, patch.object(main, "_seed_dev_data") as seed:
            self._start()
        create_all.assert_not_called()
        seed.assert_called_once_with(dev_account=False)

    def test_unset_flags_keep_development_behaviour(self, monkeypatch):
        monkeypatch.delenv("DB_AUTO_CREATE", raising=False)
        monkeypatch.delenv("SEED_DEV_DATA", raising=False)
        with (
            patch.object(main.Base.metadata, "create_all", MagicMock()) as create_all,
            patch.object(main, "_seed_dev_data") as seed,
        ):
            self._start()
        create_all.assert_called_once()
        seed.assert_called_once_with(dev_account=True)
