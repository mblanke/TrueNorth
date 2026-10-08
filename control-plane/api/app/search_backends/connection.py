"""How the API reaches OpenSearch: one place for the URL, credentials and TLS verification.

Every API client of the store (search backend, vector store, health check) builds its
httpx client from here, with the same variables the worker (worker/telemetry.py) and the
scenario engine's event store read:

    OPENSEARCH_URL         base URL (production: https://opensearch:9200)
    OPENSEARCH_USER        HTTP basic auth user; empty = no auth (dev only)
    OPENSEARCH_PASS        its password
    OPENSEARCH_VERIFY_SSL  true | false | path to a CA bundle (default true)

Production runs the OpenSearch security plugin with TLS and an installer-generated
admin password (install/README.md, "OpenSearch"); dev compose runs it without.
"""

from __future__ import annotations

import os

DEFAULT_URL = "http://opensearch:9200"
_FALSE = ("0", "false", "no", "off")
_TRUE = ("", "1", "true", "yes", "on")


def opensearch_url(default: str = DEFAULT_URL) -> str:
    return os.getenv("OPENSEARCH_URL", default).rstrip("/")


def url_configured() -> bool:
    """Was OPENSEARCH_URL set explicitly (not the built-in default)?"""
    return bool(os.getenv("OPENSEARCH_URL", "").strip())


def verify_setting() -> bool | str:
    """``OPENSEARCH_VERIFY_SSL`` as httpx's ``verify``: True, False or a CA bundle path."""
    raw = os.getenv("OPENSEARCH_VERIFY_SSL", "true").strip()
    if raw.lower() in _FALSE:
        return False
    if raw.lower() in _TRUE:
        return True
    return raw


def credentials() -> tuple[str, str] | None:
    user = os.getenv("OPENSEARCH_USER", "")
    return (user, os.getenv("OPENSEARCH_PASS", "")) if user else None


def client_kwargs(timeout: float = 10.0, auth: tuple[str, str] | None = None) -> dict:
    """httpx client arguments: timeout, basic auth (``auth`` or the env's), TLS verification."""
    kwargs: dict = {"timeout": timeout, "verify": verify_setting()}
    auth = auth or credentials()
    if auth:
        kwargs["auth"] = auth
    return kwargs
