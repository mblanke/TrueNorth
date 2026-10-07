"""TrueNorth Range — AI inference engine adapter interface (ADR 0001).

The AI admin screen (``/ai-config``) stores backends as DB rows
(``AIBackendConfig``: type, base URL, key, timeout). Talking to the engine a
row points at — a test completion, a model discovery probe — goes through one
of these adapters, chosen by ``backend_type`` from the registry in
``__init__``. Routers never build engine URLs or payloads themselves.

Adapters are stateless: connection details come from the row on every call.
Generation for real authoring flows does NOT go through here; it goes through
the ai-orchestrator service (``AI_ORCHESTRATOR_URL``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


def empty_probe(url: str) -> dict:
    """The probe result shape every adapter returns."""
    return {"url": url, "online": False, "models": [], "version": None, "running": []}


class BaseAIEngine(ABC):
    """One kind of inference engine (Ollama, OpenAI-compatible, mock)."""

    #: True when completions should go to a discovered fleet node rather than the
    #: backend's base_url (Ollama behind Open WebUI / a reverse proxy).
    uses_fleet_nodes: bool = False

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        model: str = "",
    ) -> dict:
        """Run one completion. Returns ``{"response": str, "model": str}``.

        Raises ``httpx.HTTPError`` when the engine is unreachable or refuses.
        """

    @abstractmethod
    def probe(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        host: str = "",
        port: int = 0,
        timeout: float = 5.0,
    ) -> dict:
        """Report reachability and served models (see :func:`empty_probe`).

        ``host``/``port`` name a known LAN node for engines that are probed per
        node; others probe ``base_url``. Never raises: failures are ``online: False``.
        """
