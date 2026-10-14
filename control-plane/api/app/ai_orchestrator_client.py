"""How the API authenticates to the ai-orchestrator service.

The orchestrator refuses every route but /health without its service token
(``AI_SERVICE_TOKEN``, ai-orchestrator/app/main.py). Every call the API makes to
``AI_ORCHESTRATOR_URL`` passes ``headers=orchestrator_headers()``; the API and the
orchestrator are given the same value.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException


def orchestrator_headers() -> dict[str, str]:
    """``Authorization: Bearer <AI_SERVICE_TOKEN>``, or nothing when no token is configured."""
    token = os.getenv("AI_SERVICE_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


# The orchestrator's answer when no AI endpoint is configured (tn_ai_enabled: false, or an
# empty OPENAI_BASE_URL / key): a 503 carrying this header (ai-orchestrator/app/main.py).
AI_NOT_CONFIGURED_HEADER = "X-AI-Not-Configured"
AI_NOT_CONFIGURED_DETAIL = (
    "AI is not configured on this host. An administrator must set the AI endpoint "
    '(install/README.md "AI (optional)") before AI features can be used.'
)


def raise_if_ai_not_configured(response: Any) -> None:
    """Raise a 503 naming the cause when the orchestrator said "AI is not configured".

    Callers map other orchestrator failures to 502 (it answered, badly); this one is a
    deliberate state of the host, not a fault, and must not read as a crash.
    """
    headers = getattr(response, "headers", None) or {}
    flagged = str(headers.get(AI_NOT_CONFIGURED_HEADER, "")).lower() == "true"
    if getattr(response, "status_code", None) == 503 and flagged:
        raise HTTPException(503, AI_NOT_CONFIGURED_DETAIL)
