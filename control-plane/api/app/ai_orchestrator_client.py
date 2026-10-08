"""How the API authenticates to the ai-orchestrator service.

The orchestrator refuses every route but /health without its service token
(``AI_SERVICE_TOKEN``, ai-orchestrator/app/main.py). Every call the API makes to
``AI_ORCHESTRATOR_URL`` passes ``headers=orchestrator_headers()``; the API and the
orchestrator are given the same value.
"""

from __future__ import annotations

import os


def orchestrator_headers() -> dict[str, str]:
    """``Authorization: Bearer <AI_SERVICE_TOKEN>``, or nothing when no token is configured."""
    token = os.getenv("AI_SERVICE_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}
