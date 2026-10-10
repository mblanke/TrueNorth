"""Deployment environment and the settings production refuses to start without.

``TN_ENV`` names the environment: ``development`` (the default), ``test`` or
``production``. Development and test keep every historical default, so a laptop or the
test suite needs no configuration. Production fails fast: ``check_startup()`` collects
EVERY missing or unsafe setting and raises one ``UnsafeSettingsError`` naming them all,
instead of starting with a random CSRF key, a default database password or unverified
token audiences and failing (or worse, not failing) later.

Read at call time, not import time, so tests can change the environment.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit

ENVIRONMENTS = ("development", "test", "production")
MIN_SECRET_LENGTH = 32  # the same floor app/secretbox.py applies to TN_SECRETS_KEY

# Passwords that ship in examples, compose files and the code's own defaults.
WEAK_PASSWORDS = frozenset(
    {
        "",
        "forge",
        "postgres",
        "password",
        "changeme",
        "change-me",
        "secret",
        "admin",
        "minioadmin",
        "truenorth",
        "redis",
    }
)


class UnsafeSettingsError(RuntimeError):
    """Production was asked to start with settings it must not run with."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            "Refusing to start with TN_ENV=production; fix these settings:\n  - " + "\n  - ".join(problems)
        )


def tn_env() -> str:
    """The deployment environment. An unknown value is an error, not a silent 'development'."""
    value = os.getenv("TN_ENV", "development").strip().lower() or "development"
    if value not in ENVIRONMENTS:
        raise UnsafeSettingsError([f"TN_ENV={value!r} is not one of {', '.join(ENVIRONMENTS)}"])
    return value


def is_production() -> bool:
    return tn_env() == "production"


def app_version() -> str:
    """The running build, set by the release pipeline (TN_VERSION). ``dev`` when unset."""
    return os.getenv("TN_VERSION", "").strip() or "dev"


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_flag(name: str) -> bool:
    """A boolean setting whose default depends on the environment.

    Unset, it is ON in development and test (their historical behaviour) and OFF in
    production. Set, the value decides. Used for DB_AUTO_CREATE and SEED_DEV_DATA.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return not is_production()
    return raw.strip().lower() not in ("0", "false", "no", "off")


def docs_enabled() -> bool:
    """Serve /docs, /redoc and /openapi.json? Always outside production; there only on request."""
    return not is_production() or _truthy(os.getenv("DOCS_ENABLED", "false"))


def docs_urls() -> dict[str, str | None]:
    """The FastAPI(...) keyword arguments for the interactive docs and the schema route."""
    if docs_enabled():
        return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


def _url_password(url: str) -> str | None:
    try:
        return urlsplit(url).password
    except ValueError:
        return None


def _secret_problem(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        return f"{name} is not set"
    if len(value) < MIN_SECRET_LENGTH:
        return f"{name} is shorter than {MIN_SECRET_LENGTH} characters"
    return None


def production_problems() -> list[str]:
    """Every reason production must not start with the current environment. Empty = fine."""
    problems: list[str] = []
    env = os.getenv

    if _truthy(env("AUTH_DISABLED", "false")):
        problems.append("AUTH_DISABLED=true turns off authentication")
    backend = env("AUTH_BACKEND", "keycloak_oidc").strip().lower()
    if backend == "disabled":
        problems.append("AUTH_BACKEND=disabled turns off authentication")
    if backend == "keycloak_oidc" and not env("KEYCLOAK_AUDIENCE", "").strip():
        problems.append("KEYCLOAK_AUDIENCE is not set (tokens from any client in the realm would be accepted)")
    if backend == "keycloak_oidc" and not env("KEYCLOAK_ISSUER", "").strip():
        problems.append("KEYCLOAK_ISSUER is not set (a token's iss would not be checked)")
    if backend == "generic_oidc" and not env("OIDC_AUDIENCE", "").strip():
        problems.append("OIDC_AUDIENCE is not set (tokens issued to any audience would be accepted)")

    for name in ("CSRF_SECRET", "TN_SECRETS_KEY"):
        if problem := _secret_problem(name):
            problems.append(problem)

    db_url = env("DATABASE_URL", "").strip()
    if not db_url:
        problems.append("DATABASE_URL is not set (the default is the development database)")
    elif db_url.startswith("sqlite"):
        problems.append("DATABASE_URL points at SQLite")
    elif (_url_password(db_url) or "").lower() in WEAK_PASSWORDS:
        problems.append("DATABASE_URL has no password or a default one")

    redis_url = env("REDIS_URL", "").strip()
    if not redis_url:
        problems.append("REDIS_URL is not set (the default is an unauthenticated local Redis)")
    else:
        redis_pw = _url_password(redis_url) or env("REDIS_PASSWORD", "")
        if redis_pw.lower() in WEAK_PASSWORDS:
            problems.append("Redis has no password or a default one (REDIS_URL / REDIS_PASSWORD)")

    access = env("MINIO_ACCESS_KEY", "").strip()
    secret = env("MINIO_SECRET_KEY", "").strip()
    if not access or not secret:
        problems.append("MINIO_ACCESS_KEY and MINIO_SECRET_KEY must both be set")
    elif secret.lower() in WEAK_PASSWORDS or access.lower() == "minioadmin":
        problems.append("MinIO credentials are the defaults (minioadmin)")

    if _truthy(env("SEED_DEV_DATA", "false")):
        problems.append("SEED_DEV_DATA=true would create the hardcoded development admin")

    # Unset is allowed (a single-tenant install; app/rbac.py fails closed once a second
    # tenant exists, and startup logs it). Set, it must be a tenant id, not a slug.
    platform = env("PLATFORM_TENANT_ID", "").strip()
    if platform:
        try:
            uuid.UUID(platform)
        except ValueError:
            problems.append("PLATFORM_TENANT_ID is not a tenant id (UUID)")

    # xAPI identity (app/xapi.py): every statement names its actor's identity authority and
    # its IRIs by these. Outside production they fall back to the local web URL; here a
    # localhost value would be written into the LRS for good (IRIs compare as strings).
    domain = env("DOMAIN", "").strip()
    for name in ("XAPI_ACCOUNT_HOMEPAGE", "XAPI_IRI_BASE"):
        value = env(name, "").strip()
        if not value and not domain:
            problems.append(f"{name} is not set, nor DOMAIN: xAPI statements would name localhost")
        elif any(host in value for host in ("localhost", "127.0.0.1")):
            problems.append(f"{name} names localhost ({value})")

    # cmi5 (app/cmi5/structure.py): where Students' browsers reach the AU runtime and the API.
    # They default to the platform URL (DOMAIN, else LTI_WEB_BASE_URL), never localhost here.
    web = env("CMI5_WEB_BASE_URL", "").strip() or (
        f"https://{domain}" if domain else env("LTI_WEB_BASE_URL", "").strip()
    )
    api = env("CMI5_API_BASE_URL", "").strip() or (f"{web.rstrip('/')}/api" if web else "")
    for name, value in (("CMI5_WEB_BASE_URL", web), ("CMI5_API_BASE_URL", api)):
        if not value:
            problems.append(f"{name} is not set, nor DOMAIN or LTI_WEB_BASE_URL: cmi5 launch URLs would name localhost")
        elif any(host in value for host in ("localhost", "127.0.0.1")):
            problems.append(f"{name} resolves to localhost ({value})")
    return problems


def check_startup() -> str:
    """Validate the environment for this process; returns TN_ENV. Raises UnsafeSettingsError."""
    current = tn_env()
    if current == "production":
        problems = production_problems()
        if problems:
            raise UnsafeSettingsError(problems)
    return current
