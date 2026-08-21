"""TrueNorth Range — AI authoring proxies.

Thin, tenant-guarded pass-throughs to the AI orchestrator's authoring helpers.
Both orchestrator routes (`/ai/scenario-suggest`, `/ai/detection-rule`) were
implemented and prompt-engineered but had no control-plane door, so no UI could
reach them. Request models mirror the orchestrator's so bad input is rejected
here (a 422 the caller can read) instead of after a round-trip.
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser, get_current_user

logger = logging.getLogger("truenorth.api.ai_authoring")

AI_ORCHESTRATOR_URL = os.getenv("AI_ORCHESTRATOR_URL", "http://ai-orchestrator:6000")

router = APIRouter(prefix="/ai", tags=["ai-authoring"])


class ScenarioDraftIn(BaseModel):
    objectives: list[str] = Field(..., min_length=1, max_length=10)
    difficulty: str = Field(default="intermediate")
    duration_minutes: int = Field(default=60, ge=15, le=480)


class DetectionDraftIn(BaseModel):
    technique: str = Field(..., pattern=r"^T\d{4}(\.\d{3})?$")
    data_source: str = Field(default="sysmon")
    format: str = Field(default="sigma")


async def _proxy(path: str, payload: dict) -> dict:
    """POST to the orchestrator and surface its result, mapping transport faults.

    Same shape as exercise_forge._call_forge_ai: an orchestrator 4xx/5xx becomes
    a 502 (it answered, badly), an unreachable orchestrator a 503.
    """
    async with httpx.AsyncClient(timeout=330) as client:
        try:
            resp = await client.post(f"{AI_ORCHESTRATOR_URL}{path}", json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("AI orchestrator %s returned %s: %s", path, exc.response.status_code, exc.response.text)
            raise HTTPException(502, "AI orchestrator failed to generate a draft.") from exc
        except httpx.ConnectError as exc:
            logger.error("Cannot reach AI orchestrator at %s: %s", AI_ORCHESTRATOR_URL, exc)
            raise HTTPException(503, "AI orchestrator is unavailable.") from exc


@router.post("/scenario-draft")
async def scenario_draft(body: ScenarioDraftIn, user: CurrentUser = Depends(get_current_user)) -> dict:
    """Draft an engine-dialect scenario YAML from training objectives."""
    return await _proxy("/ai/scenario-suggest", body.model_dump())


@router.post("/detection-draft")
async def detection_draft(body: DetectionDraftIn, user: CurrentUser = Depends(get_current_user)) -> dict:
    """Draft a detection rule for a MITRE technique in the requested format."""
    return await _proxy("/ai/detection-rule", body.model_dump())
