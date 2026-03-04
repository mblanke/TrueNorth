"""TrueNorth Range Python SDK — async-first with sync wrapper.

Usage (async):
    async with TrueNorthClient("https://range.example.com", api_key="k-...") as tn:
        ranges = await tn.list_ranges()
        await tn.provision_range(ranges[0]["id"])

Usage (sync):
    with TrueNorthClient.Sync("https://range.example.com", api_key="k-...") as tn:
        ranges = tn.list_ranges()
        tn.provision_range(ranges[0]["id"])
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Callable, Awaitable

import httpx

logger = logging.getLogger("truenorth.sdk")

# ═══════════════════════════════════════════════════════════════════
#  Exception Hierarchy
# ═══════════════════════════════════════════════════════════════════


class TrueNorthError(Exception):
    """Base exception for all SDK errors."""

    def __init__(self, message: str, status_code: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class AuthError(TrueNorthError):
    """Authentication / authorisation failure (401/403)."""


class NotFoundError(TrueNorthError):
    """Resource not found (404)."""


class ValidationError(TrueNorthError):
    """Request validation failed (422)."""


class RateLimitError(TrueNorthError):
    """Rate limit exceeded (429).  Check ``retry_after``."""

    def __init__(self, message: str, retry_after: float | None = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class ConflictError(TrueNorthError):
    """Conflict (409) — e.g. duplicate resource."""


class ServerError(TrueNorthError):
    """Upstream server error (5xx)."""


class TimeoutError(TrueNorthError):  # noqa: A001
    """Request timed out."""


# ═══════════════════════════════════════════════════════════════════
#  Pagination helpers
# ═══════════════════════════════════════════════════════════════════

_DEFAULT_PAGE_SIZE = 50


async def _auto_paginate(
    fetch: Callable[..., Awaitable[list[dict]]],
    limit: int | None = None,
    **kwargs: Any,
) -> list[dict]:
    """Iterate through all pages and return combined results."""
    items: list[dict] = []
    offset = 0
    page_size = min(limit or _DEFAULT_PAGE_SIZE, _DEFAULT_PAGE_SIZE)
    while True:
        page = await fetch(limit=page_size, offset=offset, **kwargs)
        if not page:
            break
        items.extend(page)
        if limit and len(items) >= limit:
            return items[:limit]
        if len(page) < page_size:
            break
        offset += page_size
    return items


# ═══════════════════════════════════════════════════════════════════
#  Async Client
# ═══════════════════════════════════════════════════════════════════


class TrueNorthClient:
    """Async Python SDK for the TrueNorth Range API.

    Parameters
    ----------
    base_url : str
        Root URL of the TrueNorth API (e.g. ``https://range.example.com``).
    api_key : str | None
        Static API key (``X-API-Key`` header).
    token : str | None
        Pre-existing JWT bearer token.
    timeout : float
        Default request timeout in seconds.
    max_retries : int
        Max retry attempts on transient failures.
    backoff_factor : float
        Exponential backoff multiplier (seconds).
    """

    # Retryable status codes
    _RETRYABLE: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
    ):
        self.base_url = (
            base_url or os.getenv("TRUENORTH_API_URL", "http://localhost:8080")
        ).rstrip("/")
        self._api_key = api_key or os.getenv("TRUENORTH_API_KEY")
        self._token = token or os.getenv("TRUENORTH_TOKEN")
        self._refresh_token_str: str | None = None
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._client: httpx.AsyncClient | None = None
        self._ws_tasks: list[asyncio.Task] = []

    # ── lifecycle ─────────────────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = self._build_headers()
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self._timeout),
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20,
                    keepalive_expiry=30.0,
                ),
            )
        return self._client

    def _build_headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if self._api_key:
            h["X-API-Key"] = self._api_key
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        for task in self._ws_tasks:
            task.cancel()
        self._ws_tasks.clear()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "TrueNorthClient":
        await self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ── HTTP helpers ──────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        client = await self._ensure_client()
        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await client.request(
                    method,
                    path,
                    json=json_body,
                    params=self._clean_params(params),
                )

                # Auto-refresh on 401
                if resp.status_code == 401 and self._refresh_token_str and attempt == 1:
                    await self.refresh_token()
                    client.headers.update(self._build_headers())
                    continue

                if resp.status_code in self._RETRYABLE and attempt < self._max_retries:
                    retry_after = float(resp.headers.get("Retry-After", 0))
                    wait = max(retry_after, self._backoff_factor * (2 ** (attempt - 1)))
                    logger.warning(
                        "Retryable %s on %s %s (attempt %d/%d, wait %.1fs)",
                        resp.status_code, method, path, attempt, self._max_retries, wait,
                    )
                    await asyncio.sleep(wait)
                    continue

                self._raise_for_status(resp)
                if raw:
                    return resp.text
                if resp.status_code == 204 or not resp.content:
                    return None
                return resp.json()

            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    wait = self._backoff_factor * (2 ** (attempt - 1))
                    logger.warning("Timeout on %s %s (attempt %d), retrying in %.1fs", method, path, attempt, wait)
                    await asyncio.sleep(wait)
                    continue
            except httpx.ConnectError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    wait = self._backoff_factor * (2 ** (attempt - 1))
                    await asyncio.sleep(wait)
                    continue

        raise TimeoutError(f"Request {method} {path} failed after {self._max_retries} attempts") from last_exc

    @staticmethod
    def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
        if params is None:
            return None
        return {k: v for k, v in params.items() if v is not None}

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.is_success:
            return
        detail = None
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text

        msg = f"HTTP {resp.status_code}: {detail}"
        code = resp.status_code

        if code in (401, 403):
            raise AuthError(msg, status_code=code, detail=detail)
        if code == 404:
            raise NotFoundError(msg, status_code=code, detail=detail)
        if code == 409:
            raise ConflictError(msg, status_code=code, detail=detail)
        if code == 422:
            raise ValidationError(msg, status_code=code, detail=detail)
        if code == 429:
            retry_after = float(resp.headers.get("Retry-After", 0)) or None
            raise RateLimitError(msg, retry_after=retry_after, status_code=code, detail=detail)
        if code >= 500:
            raise ServerError(msg, status_code=code, detail=detail)
        raise TrueNorthError(msg, status_code=code, detail=detail)

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, data: Any | None = None) -> Any:
        return await self._request("POST", path, json_body=data)

    async def _put(self, path: str, data: dict[str, Any]) -> Any:
        return await self._request("PUT", path, json_body=data)

    async def _patch(self, path: str, data: dict[str, Any]) -> Any:
        return await self._request("PATCH", path, json_body=data)

    async def _delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # ═══════════════════════════════════════════════════════════════
    #  Auth
    # ═══════════════════════════════════════════════════════════════

    async def login(self, username: str, password: str) -> str:
        """Authenticate and store JWT token.  Returns the access token."""
        resp = await self._post("/auth/login", {"username": username, "password": password})
        self._token = resp["access_token"]
        self._refresh_token_str = resp.get("refresh_token")
        client = await self._ensure_client()
        client.headers["Authorization"] = f"Bearer {self._token}"
        return self._token

    async def refresh_token(self) -> str:
        """Refresh the JWT using the stored refresh token."""
        if not self._refresh_token_str:
            raise AuthError("No refresh token available")
        resp = await self._post("/auth/refresh", {"refresh_token": self._refresh_token_str})
        self._token = resp["access_token"]
        self._refresh_token_str = resp.get("refresh_token", self._refresh_token_str)
        client = await self._ensure_client()
        client.headers["Authorization"] = f"Bearer {self._token}"
        return self._token

    # ═══════════════════════════════════════════════════════════════
    #  Health
    # ═══════════════════════════════════════════════════════════════

    async def health(self) -> dict:
        """Return API health status."""
        return await self._get("/health")

    # ═══════════════════════════════════════════════════════════════
    #  Tenants
    # ═══════════════════════════════════════════════════════════════

    async def list_tenants(self) -> list[dict]:
        return await self._get("/tenants")

    async def get_tenant(self, tenant_id: str) -> dict:
        return await self._get(f"/tenants/{tenant_id}")

    async def create_tenant(self, name: str, **kwargs: Any) -> dict:
        return await self._post("/tenants", {"name": name, **kwargs})

    # ═══════════════════════════════════════════════════════════════
    #  Templates
    # ═══════════════════════════════════════════════════════════════

    async def list_templates(self, limit: int = 50, offset: int = 0) -> list[dict]:
        return await self._get("/templates", limit=limit, offset=offset)

    async def list_all_templates(self) -> list[dict]:
        """Auto-paginate through all templates."""
        return await _auto_paginate(self.list_templates)

    async def get_template(self, template_id: str) -> dict:
        return await self._get(f"/templates/{template_id}")

    async def create_template(
        self, name: str, version: str, definition: dict, **kwargs: Any
    ) -> dict:
        return await self._post(
            "/templates",
            {"name": name, "version": version, "definition": definition, **kwargs},
        )

    async def update_template(self, template_id: str, **kwargs: Any) -> dict:
        return await self._put(f"/templates/{template_id}", kwargs)

    async def delete_template(self, template_id: str) -> None:
        await self._delete(f"/templates/{template_id}")

    # ═══════════════════════════════════════════════════════════════
    #  Scenarios
    # ═══════════════════════════════════════════════════════════════

    async def list_scenarios(self, limit: int = 50, offset: int = 0) -> list[dict]:
        return await self._get("/scenarios", limit=limit, offset=offset)

    async def list_all_scenarios(self) -> list[dict]:
        return await _auto_paginate(self.list_scenarios)

    async def get_scenario(self, scenario_id: str) -> dict:
        return await self._get(f"/scenarios/{scenario_id}")

    async def create_scenario(
        self, name: str, version: str, definition: dict, **kwargs: Any
    ) -> dict:
        return await self._post(
            "/scenarios",
            {"name": name, "version": version, "definition": definition, **kwargs},
        )

    async def update_scenario(self, scenario_id: str, **kwargs: Any) -> dict:
        return await self._put(f"/scenarios/{scenario_id}", kwargs)

    async def delete_scenario(self, scenario_id: str) -> None:
        await self._delete(f"/scenarios/{scenario_id}")

    # ═══════════════════════════════════════════════════════════════
    #  Ranges
    # ═══════════════════════════════════════════════════════════════

    async def list_ranges(
        self, limit: int = 50, offset: int = 0, **filters: Any
    ) -> list[dict]:
        return await self._get("/ranges", limit=limit, offset=offset, **filters)

    async def list_all_ranges(self, **filters: Any) -> list[dict]:
        return await _auto_paginate(self.list_ranges, **filters)

    async def get_range(self, range_id: str) -> dict:
        return await self._get(f"/ranges/{range_id}")

    async def create_range(self, name: str, template_id: str, **kwargs: Any) -> dict:
        return await self._post(
            "/ranges", {"name": name, "template_id": template_id, **kwargs}
        )

    async def provision_range(self, range_id: str) -> dict:
        return await self._post(f"/ranges/{range_id}/provision")

    async def destroy_range(self, range_id: str) -> dict:
        return await self._post(f"/ranges/{range_id}/destroy")

    async def stop_range(self, range_id: str) -> dict:
        return await self._post(f"/ranges/{range_id}/stop")

    async def batch_provision(self, range_ids: list[str]) -> dict:
        """Provision up to 500 ranges in a single batch."""
        return await self._post("/ranges/batch-provision", {"range_ids": range_ids})

    async def get_range_stats(self, tenant_id: str | None = None) -> dict:
        return await self._get("/ranges/stats", tenant_id=tenant_id)

    # ═══════════════════════════════════════════════════════════════
    #  Exercises
    # ═══════════════════════════════════════════════════════════════

    async def list_exercises(self, limit: int = 50, offset: int = 0) -> list[dict]:
        return await self._get("/exercises", limit=limit, offset=offset)

    async def list_all_exercises(self) -> list[dict]:
        return await _auto_paginate(self.list_exercises)

    async def get_exercise(self, exercise_id: str) -> dict:
        return await self._get(f"/exercises/{exercise_id}")

    async def create_exercise(
        self,
        name: str,
        range_id: str,
        scenario_id: str,
        **kwargs: Any,
    ) -> dict:
        return await self._post(
            "/exercises",
            {"name": name, "range_id": range_id, "scenario_id": scenario_id, **kwargs},
        )

    async def start_exercise(self, exercise_id: str) -> dict:
        return await self._post(f"/exercises/{exercise_id}/start")

    async def pause_exercise(self, exercise_id: str) -> dict:
        return await self._post(f"/exercises/{exercise_id}/pause")

    async def complete_exercise(self, exercise_id: str) -> dict:
        return await self._post(f"/exercises/{exercise_id}/complete")

    async def get_aar(self, exercise_id: str) -> dict:
        return await self._get(f"/exercises/{exercise_id}/aar")

    async def generate_aar(self, exercise_id: str) -> dict:
        return await self._post(f"/exercises/{exercise_id}/aar/generate")

    async def get_aar_html(self, exercise_id: str) -> str:
        return await self._request("GET", f"/exercises/{exercise_id}/aar/html", raw=True)

    # ═══════════════════════════════════════════════════════════════
    #  Objectives
    # ═══════════════════════════════════════════════════════════════

    async def list_objectives(self, exercise_id: str) -> list[dict]:
        return await self._get(f"/exercises/{exercise_id}/objectives")

    async def ack_objective(
        self, exercise_id: str, ref_id: str, evidence: str = ""
    ) -> dict:
        return await self._post(
            f"/exercises/{exercise_id}/objectives/{ref_id}/ack",
            {"evidence": evidence},
        )

    # ═══════════════════════════════════════════════════════════════
    #  Telemetry
    # ═══════════════════════════════════════════════════════════════

    async def search_telemetry(
        self, range_id: str, query: str = "*", size: int = 50, **kwargs: Any
    ) -> dict:
        return await self._get(
            f"/telemetry/{range_id}/search", q=query, size=size, **kwargs
        )

    async def ingest_events(self, range_id: str, events: list[dict]) -> dict:
        return await self._post(f"/telemetry/{range_id}/events", events)

    # ═══════════════════════════════════════════════════════════════
    #  Teams
    # ═══════════════════════════════════════════════════════════════

    async def list_teams(self) -> list[dict]:
        return await self._get("/teams")

    async def create_team(self, name: str, **kwargs: Any) -> dict:
        return await self._post("/teams", {"name": name, **kwargs})

    async def delete_team(self, team_id: str) -> None:
        await self._delete(f"/teams/{team_id}")

    # ═══════════════════════════════════════════════════════════════
    #  Users
    # ═══════════════════════════════════════════════════════════════

    async def list_users(self) -> list[dict]:
        return await self._get("/users")

    async def get_me(self) -> dict:
        return await self._get("/users/me")

    # ═══════════════════════════════════════════════════════════════
    #  Audit Log
    # ═══════════════════════════════════════════════════════════════

    async def list_audit_log(self, limit: int = 100) -> list[dict]:
        return await self._get("/audit-log", limit=limit)

    # ═══════════════════════════════════════════════════════════════
    #  WebSocket Subscriptions
    # ═══════════════════════════════════════════════════════════════

    async def subscribe(
        self,
        channel: str,
        callback: Callable[[dict], Awaitable[None] | None],
        *,
        reconnect: bool = True,
        max_reconnect_attempts: int = 10,
    ) -> None:
        """Subscribe to a WebSocket channel with auto-reconnect.

        Parameters
        ----------
        channel : str
            Channel name (e.g. ``ranges``, ``exercises``, ``telemetry``).
        callback : callable
            Async or sync function called with each parsed JSON message.
        reconnect : bool
            Whether to reconnect on disconnect.
        max_reconnect_attempts : int
            Maximum consecutive reconnect attempts before giving up.
        """
        try:
            import websockets  # type: ignore[import-untyped]
        except ImportError:
            raise ImportError(
                "websockets package required for subscriptions: pip install websockets"
            )

        ws_url = self.base_url.replace("http", "ws", 1) + f"/ws/{channel}"
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        async def _listen() -> None:
            attempts = 0
            while True:
                try:
                    async with websockets.connect(ws_url, additional_headers=headers) as ws:
                        attempts = 0
                        async for raw_msg in ws:
                            try:
                                msg = json.loads(raw_msg)
                            except json.JSONDecodeError:
                                msg = {"raw": raw_msg}
                            result = callback(msg)
                            if asyncio.iscoroutine(result):
                                await result
                except (websockets.ConnectionClosed, OSError) as exc:
                    if not reconnect:
                        raise
                    attempts += 1
                    if attempts > max_reconnect_attempts:
                        logger.error("WebSocket reconnect limit reached for %s", channel)
                        raise
                    wait = min(self._backoff_factor * (2 ** attempts), 30.0)
                    logger.warning("WebSocket %s disconnected, reconnecting in %.1fs (%d/%d)", channel, wait, attempts, max_reconnect_attempts)
                    await asyncio.sleep(wait)

        task = asyncio.create_task(_listen())
        self._ws_tasks.append(task)

    # ═══════════════════════════════════════════════════════════════
    #  AI Orchestrator
    # ═══════════════════════════════════════════════════════════════

    async def ai_generate(self, prompt: str, task: str = "general", **kwargs: Any) -> dict:
        """Send a generation request to the AI orchestrator."""
        return await self._post("/ai/generate", {"prompt": prompt, "task": task, **kwargs})

    async def ai_detection_rule(self, description: str, **kwargs: Any) -> dict:
        """Generate a detection rule from a natural-language description."""
        return await self._post("/ai/detection-rule", {"description": description, **kwargs})

    async def ai_scenario_suggest(self, requirements: str, **kwargs: Any) -> dict:
        """Suggest scenario configurations based on requirements."""
        return await self._post("/ai/scenario-suggest", {"requirements": requirements, **kwargs})

    async def ai_aar_analysis(self, exercise_data: dict, **kwargs: Any) -> dict:
        """Generate AI-powered AAR analysis for exercise data."""
        return await self._post("/ai/aar-analysis", {"exercise_data": exercise_data, **kwargs})

    async def get_fleet_status(self) -> dict:
        """Return Ollama fleet node statuses."""
        return await self._get("/ai/fleet/status")

    # ═══════════════════════════════════════════════════════════════
    #  Sync Wrapper
    # ═══════════════════════════════════════════════════════════════

    class Sync:
        """Synchronous wrapper — delegates to the async client via ``asyncio.run()``.

        Provides the same method signatures minus ``async``/``await``.

        Example::

            with TrueNorthClient.Sync("https://range.example.com") as tn:
                print(tn.health())
                ranges = tn.list_ranges()
        """

        def __init__(self, *args: Any, **kwargs: Any):
            self._async_client = TrueNorthClient(*args, **kwargs)
            self._loop: asyncio.AbstractEventLoop | None = None

        def _get_loop(self) -> asyncio.AbstractEventLoop:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
            return self._loop

        def _run(self, coro: Any) -> Any:
            return self._get_loop().run_until_complete(coro)

        def close(self) -> None:
            self._run(self._async_client.close())
            if self._loop and not self._loop.is_closed():
                self._loop.close()
                self._loop = None

        def __enter__(self) -> "TrueNorthClient.Sync":
            self._run(self._async_client.__aenter__())
            return self

        def __exit__(self, *args: Any) -> None:
            self.close()

        def __getattr__(self, name: str) -> Any:
            attr = getattr(self._async_client, name)
            if callable(attr) and asyncio.iscoroutinefunction(attr):

                @functools.wraps(attr)
                def sync_method(*a: Any, **kw: Any) -> Any:
                    return self._run(attr(*a, **kw))

                return sync_method
            return attr