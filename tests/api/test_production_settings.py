"""TN_ENV=production fails fast on unsafe settings (app/settings.py).

Before: production started with AUTH_DISABLED honoured, a random per-process CSRF key
(so tokens minted by one worker failed on the next), the development database
password, unverified token audiences, the schema created by create_all() and the
hardcoded development admin seeded unless two flags were remembered, and /docs open.
Each of those now stops the process at startup, all named in one error.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from app import settings
from app.auth_backends import GenericOIDCBackend, KeycloakOIDCBackend
from fastapi.testclient import TestClient

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"

GOOD = {
    "TN_ENV": "production",
    "AUTH_DISABLED": "false",
    "AUTH_BACKEND": "keycloak_oidc",
    "KEYCLOAK_AUDIENCE": "truenorth-api",
    "CSRF_SECRET": "c" * 48,
    "TN_SECRETS_KEY": "k" * 48,
    "DATABASE_URL": "postgresql+psycopg://tn:Xq7-long-db-password@pgbouncer:5432/tn",
    "REDIS_URL": "redis://:Rz9-long-redis-password@redis:6379/0",
    "MINIO_ACCESS_KEY": "tn-objects",
    "MINIO_SECRET_KEY": "Mn4-long-minio-secret",
}
NAMES = (*GOOD, "SEED_DEV_DATA", "DB_AUTO_CREATE", "DOCS_ENABLED", "OIDC_AUDIENCE", "REDIS_PASSWORD", "TN_VERSION")


@pytest.fixture
def prod(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in GOOD.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_a_complete_production_environment_starts(prod):
    assert settings.production_problems() == []
    assert settings.check_startup() == "production"


def test_an_empty_production_environment_names_every_problem(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TN_ENV", "production")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    with pytest.raises(settings.UnsafeSettingsError) as exc:
        settings.check_startup()
    text = str(exc.value)
    for name in ("AUTH_DISABLED", "KEYCLOAK_AUDIENCE", "CSRF_SECRET", "TN_SECRETS_KEY", "DATABASE_URL", "REDIS_URL",
                 "MINIO_ACCESS_KEY"):
        assert name in text, name
    assert len(exc.value.problems) >= 7


@pytest.mark.parametrize(
    ("name", "value", "needle"),
    [
        ("AUTH_DISABLED", "true", "AUTH_DISABLED"),
        ("AUTH_BACKEND", "disabled", "AUTH_BACKEND"),
        ("KEYCLOAK_AUDIENCE", "", "KEYCLOAK_AUDIENCE"),
        ("CSRF_SECRET", "", "CSRF_SECRET"),
        ("CSRF_SECRET", "short", "CSRF_SECRET"),
        ("TN_SECRETS_KEY", "", "TN_SECRETS_KEY"),
        ("DATABASE_URL", "postgresql+psycopg://forge:forge@localhost:5432/forge", "DATABASE_URL"),
        ("DATABASE_URL", "postgresql+psycopg://tn@db/tn", "DATABASE_URL"),
        ("DATABASE_URL", "sqlite:///tn.db", "SQLite"),
        ("REDIS_URL", "redis://redis:6379/0", "Redis"),
        ("MINIO_SECRET_KEY", "minioadmin", "MinIO"),
        ("MINIO_ACCESS_KEY", "minioadmin", "MinIO"),
        ("SEED_DEV_DATA", "true", "SEED_DEV_DATA"),
    ],
)
def test_each_unsafe_setting_is_refused(prod, name, value, needle):
    prod.setenv(name, value)
    problems = settings.production_problems()
    assert any(needle in p for p in problems), problems


def test_generic_oidc_needs_its_own_audience(prod):
    prod.setenv("AUTH_BACKEND", "generic_oidc")
    prod.delenv("KEYCLOAK_AUDIENCE")
    assert any("OIDC_AUDIENCE" in p for p in settings.production_problems())
    prod.setenv("OIDC_AUDIENCE", "api://truenorth")
    assert settings.production_problems() == []


def test_redis_password_may_come_from_redis_password(prod):
    prod.setenv("REDIS_URL", "redis://redis:6379/0")
    prod.setenv("REDIS_PASSWORD", "Rz9-long-redis-password")
    assert settings.production_problems() == []


def test_development_and_test_need_nothing(monkeypatch):
    for env in ("development", "test", ""):
        monkeypatch.setenv("TN_ENV", env)
        monkeypatch.setenv("AUTH_DISABLED", "true")
        assert settings.check_startup() in ("development", "test")


def test_an_unknown_environment_is_an_error(monkeypatch):
    monkeypatch.setenv("TN_ENV", "prod")
    with pytest.raises(settings.UnsafeSettingsError, match="TN_ENV"):
        settings.check_startup()


def test_schema_and_seed_flags_default_off_only_in_production(monkeypatch):
    monkeypatch.delenv("DB_AUTO_CREATE", raising=False)
    monkeypatch.setenv("TN_ENV", "development")
    assert settings.env_flag("DB_AUTO_CREATE") is True
    monkeypatch.setenv("TN_ENV", "production")
    assert settings.env_flag("DB_AUTO_CREATE") is False
    monkeypatch.setenv("DB_AUTO_CREATE", "true")
    assert settings.env_flag("DB_AUTO_CREATE") is True
    monkeypatch.setenv("TN_ENV", "development")
    monkeypatch.setenv("DB_AUTO_CREATE", "false")
    assert settings.env_flag("DB_AUTO_CREATE") is False


def test_docs_are_off_in_production_unless_asked_for(monkeypatch):
    monkeypatch.delenv("DOCS_ENABLED", raising=False)
    monkeypatch.setenv("TN_ENV", "development")
    assert settings.docs_urls()["openapi_url"] == "/openapi.json"
    monkeypatch.setenv("TN_ENV", "production")
    assert settings.docs_urls() == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    monkeypatch.setenv("DOCS_ENABLED", "true")
    assert settings.docs_urls()["docs_url"] == "/docs"


def test_version_comes_from_the_release_pipeline(monkeypatch):
    monkeypatch.delenv("TN_VERSION", raising=False)
    assert settings.app_version() == "dev"
    monkeypatch.setenv("TN_VERSION", "1.4.2")
    assert settings.app_version() == "1.4.2"


def test_the_api_refuses_to_start_in_production_with_unsafe_settings(monkeypatch):
    from app.main import app

    monkeypatch.setenv("TN_ENV", "production")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    with pytest.raises(settings.UnsafeSettingsError, match="AUTH_DISABLED"), TestClient(app):
        pass


def _import_app(env: dict) -> str:
    code = (
        "from app.main import app, APP_VERSION; "
        "print(app.docs_url, app.redoc_url, app.openapi_url, APP_VERSION, sep='|')"
    )
    base = {k: v for k, v in os.environ.items() if k not in NAMES}
    r = subprocess.run([sys.executable, "-c", code], cwd=API, env={**base, **env}, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip().splitlines()[-1]


def test_a_production_app_serves_no_docs_and_reports_its_release():
    out = _import_app({**GOOD, "TN_VERSION": "2.0.1", "PYTHONPATH": str(API)})
    assert out == "None|None|None|2.0.1"
    out = _import_app({**GOOD, "DOCS_ENABLED": "true", "PYTHONPATH": str(API)})
    assert out == "/docs|/redoc|/openapi.json|dev"


# -- token audience (keycloak_oidc.py, generic_oidc.py) ---------------------------------
def test_production_keycloak_backend_requires_an_audience(prod):
    prod.delenv("KEYCLOAK_AUDIENCE")
    with pytest.raises(ValueError, match="KEYCLOAK_AUDIENCE"):
        KeycloakOIDCBackend()


def test_production_generic_backend_requires_an_audience(prod):
    prod.setenv("OIDC_JWKS_URL", "https://idp.example/keys")
    with pytest.raises(ValueError, match="OIDC_AUDIENCE"):
        GenericOIDCBackend()


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [KeycloakOIDCBackend, GenericOIDCBackend])
async def test_production_always_verifies_the_audience(prod, mocker, cls):
    prod.setenv("OIDC_JWKS_URL", "https://idp.example/keys")
    prod.setenv("OIDC_AUDIENCE", "api://truenorth")
    backend = cls()
    mocker.patch.object(backend, "_get_jwks", return_value={"keys": []})
    decode = mocker.patch(f"{cls.__module__}.jwks_verify.decode", return_value={"sub": "x"})
    mocker.patch(f"{cls.__module__}.jwks_verify.unverified_kid", return_value=None)
    await backend.validate_token("a.b.c")
    kwargs = decode.call_args.kwargs
    assert kwargs["verify_aud"] is True and kwargs["audience"]


def test_development_keeps_the_audience_optional(monkeypatch):
    monkeypatch.setenv("TN_ENV", "development")
    monkeypatch.delenv("KEYCLOAK_AUDIENCE", raising=False)
    assert KeycloakOIDCBackend()._audience is None
