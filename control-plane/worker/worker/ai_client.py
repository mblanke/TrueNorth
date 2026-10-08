"""How the worker authenticates to the ai-orchestrator service.

The orchestrator refuses every route but /health without its service token
(``AI_SERVICE_TOKEN``, ai-orchestrator/app/main.py). The API has the same helper in
control-plane/api/app/ai_orchestrator_client.py; neither service may import the other.
"""

from __future__ import annotations

import os


def orchestrator_headers() -> dict[str, str]:
    """``Authorization: Bearer <AI_SERVICE_TOKEN>``, or nothing when no token is configured."""
    token = os.getenv("AI_SERVICE_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}
