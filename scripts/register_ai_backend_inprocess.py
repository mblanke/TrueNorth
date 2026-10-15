"""Register the installer's AI endpoint as an AI backend, from inside the api image.

The installer configures the AI orchestrator's endpoint (tn_ai_base_url,
vault_openai_api_key, tn_ai_*_model) but, until 2026-10-15, never registered it through
the API, so the AI settings page showed 0 backends after an install (TN-MGMT01, a vLLM
server). This registers it as the bootstrap administrator, in-process
(scripts/_inprocess_api.py): the API's routes, permissions, CSRF and validation all
apply, and no token is used.

Run by the installer (roles/tn_seed, tn_register_ai_backend) as a one-off api container:

    docker compose run --rm --no-deps -T -w /app -e PYTHONPATH=/app -e TN_AI_API_KEY \\
      -v <app checkout>:/srcapp:ro --entrypoint python api \\
      /srcapp/scripts/register_ai_backend_inprocess.py --admin <upn> --name "Platform AI" \\
      --base-url <url> --models agent,gpt-oss-120b [--update-key]

The key comes ONLY from the environment variable ``TN_AI_API_KEY`` (the installer passes
it through ``docker compose -e NAME`` from a ``no_log`` task), never from the command
line. It is never printed. Empty is allowed: an unauthenticated endpoint.

Idempotent. A backend with the same name (or, failing that, the same base URL) is reused,
never duplicated; its base URL is patched when it differs from the installer's. Its key is
left alone (an operator may have rotated it on the AI page) unless ``--update-key``. When
no backend is primary this one becomes primary. Each model id gets a route to it unless a
route for that id already exists (one pointing at another backend is an operator's
choice and is left alone). Then the backend is discovered; an unreachable endpoint or a
model it does not list is a WARNING, not a failure: the backend is registered either way.

Exit: 0 registered (routes/discovery may have warned); 1 could not create or update it;
2 the administrator does not exist.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

SRC = Path("/srcapp")
KEY_ENV = "TN_AI_API_KEY"
BACKEND_TYPE = "openai"  # OpenAI-compatible: vLLM, LiteLLM, OpenAI (app/ai_backends)
DEFAULT_NAME = "Platform AI"
ROUTE_TAGS = "installer"


def _ok(status: Any) -> bool:
    return isinstance(status, int) and 200 <= status < 300


def _brief(body: Any, limit: int = 200) -> str:
    if isinstance(body, dict):
        body = body.get("message") or body.get("detail") or body
    return " ".join(str(body).split())[:limit]


def _url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def model_ids(values: list[str]) -> list[str]:
    """Comma-separated model ids, blanks dropped, first occurrence kept."""
    seen: list[str] = []
    for value in values:
        for mid in str(value or "").split(","):
            mid = mid.strip()
            if mid and mid not in seen:
                seen.append(mid)
    return seen


def _upsert_backend(api: Any, *, name: str, base_url: str, api_key: str, update_key: bool) -> dict | None:
    status, rows = api.get("/ai-config/backends")
    if not _ok(status) or not isinstance(rows, list):
        print(f"ai backend: FAIL listing backends: {status} {_brief(rows)}")
        return None
    rows = [r for r in rows if isinstance(r, dict)]
    has_primary = any(r.get("is_primary") for r in rows)
    want_name = name.strip().lower()
    existing = next((r for r in rows if str(r.get("name") or "").strip().lower() == want_name), None)
    if existing is None:
        existing = next((r for r in rows if _url(r.get("base_url")) == _url(base_url)), None)

    if existing is None:
        payload = {
            "name": name,
            "backend_type": BACKEND_TYPE,
            "base_url": base_url,
            "api_key": api_key or None,
            "is_primary": not has_primary,
            "notes": "Registered by the installer (80-seed).",
        }
        status, backend = api.post("/ai-config/backends", json=payload)
        if not _ok(status) or not isinstance(backend, dict):
            print(f"ai backend: FAIL create {base_url}: {status} {_brief(backend)}")
            return None
        role = "primary" if backend.get("is_primary") else "not primary"
        print(f"ai backend: created '{backend.get('name')}' ({base_url}, {role})")
        return backend

    backend = existing
    drift: dict[str, Any] = {}
    if _url(backend.get("base_url")) != _url(base_url):
        drift["base_url"] = base_url
    if update_key and api_key:
        drift["api_key"] = api_key
    if drift:
        status, out = api.patch(f"/ai-config/backends/{backend['id']}", json=drift)
        if not _ok(status) or not isinstance(out, dict):
            print(f"ai backend: FAIL update '{backend.get('name')}': {status} {_brief(out)}")
            return None
        backend = out
        print(f"ai backend: updated '{backend.get('name')}' ({', '.join(sorted(drift))})")
    else:
        print(f"ai backend: unchanged '{backend.get('name')}' ({backend.get('base_url')})")
    if backend.get("backend_type") not in ("openai", "vllm", "litellm", "azure_openai"):
        print(f"ai backend: WARNING '{backend.get('name')}' is type {backend.get('backend_type')}, not changed")
    if not has_primary:
        status, out = api.post(f"/ai-config/backends/{backend['id']}/set-primary")
        if _ok(status):
            print(f"ai backend: set primary '{backend.get('name')}'")
        else:
            print(f"ai backend: WARNING could not set primary: {status} {_brief(out)}")
    return backend


def _ensure_routes(api: Any, backend_id: str, models: list[str]) -> None:
    status, rows = api.get("/ai-config/routes")
    if not _ok(status) or not isinstance(rows, list):
        print(f"ai routes: WARNING could not list routes: {status} {_brief(rows)}")
        return
    for model in models:
        same = [r for r in rows if isinstance(r, dict) and r.get("model_pattern") == model]
        if any(str(r.get("backend_id")) == str(backend_id) for r in same):
            print(f"ai route: unchanged '{model}'")
            continue
        if same:
            print(f"ai route: '{model}' routes to another backend; left alone")
            continue
        status, out = api.post(
            "/ai-config/routes", json={"model_pattern": model, "backend_id": backend_id, "tags": ROUTE_TAGS}
        )
        if _ok(status):
            print(f"ai route: added '{model}'")
        else:
            print(f"ai route: WARNING could not add '{model}': {status} {_brief(out)}")


def _discover(api: Any, backend_id: str, models: list[str]) -> None:
    status, result = api.post(f"/ai-config/backends/{backend_id}/discover")
    if not _ok(status) or not isinstance(result, dict):
        print(f"ai discover: WARNING failed: {status} {_brief(result)}")
        return
    nodes = [n for n in result.get("results") or [] if isinstance(n, dict)]
    if not nodes:
        print("ai discover: WARNING no node probed")
        return
    listed: set[str] = set()
    for node in nodes:
        if node.get("online"):
            names = [m.get("name") for m in node.get("models") or [] if isinstance(m, dict)]
            listed.update(n for n in names if n)
            print(f"ai discover: ok {node.get('node_name')} online ({len(names)} models: {', '.join(names)[:160]})")
        else:
            print(f"ai discover: WARNING {node.get('node_name')} ({node.get('url')}) is offline")
    if listed:
        missing = [m for m in models if m not in listed]
        if missing:
            print(f"ai discover: WARNING the endpoint does not list {', '.join(missing)}")


def register(
    api: Any,
    *,
    name: str,
    base_url: str,
    api_key: str,
    models: list[str],
    update_key: bool = False,
) -> int:
    """Create or reconcile the backend, route its models, discover it.
    ``api`` has ``get``/``post``/``patch`` returning ``(status, body)``. Returns the exit code."""
    backend = _upsert_backend(api, name=name, base_url=base_url, api_key=api_key, update_key=update_key)
    if backend is None:
        return 1
    backend_id = str(backend["id"])
    models = model_ids(models)
    _ensure_routes(api, backend_id, models)
    _discover(api, backend_id, models)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--admin", required=True, help="email/UPN of an existing administrator")
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--models", action="append", default=[], help="comma-separated model ids to route here")
    ap.add_argument(
        "--update-key",
        action="store_true",
        help=f"also set an existing backend's key from {KEY_ENV} (default: leave it)",
    )
    args = ap.parse_args(argv)
    api_key = os.environ.get(KEY_ENV, "")  # the only variable read; never argv, never printed

    sys.path.insert(0, str(SRC / "scripts"))
    from _inprocess_api import connect  # the shared harness: identity override, fresh CSRF, 429 backoff

    connected = connect(args.admin)
    if connected is None:
        return 2
    _client, api = connected
    return register(
        api,
        name=args.name,
        base_url=args.base_url,
        api_key=api_key,
        models=args.models,
        update_key=args.update_key,
    )


if __name__ == "__main__":
    sys.exit(main())
