"""TrueNorth Range -- AI Orchestrator Configuration Router.

DB-backed CRUD for AI backends, fleet nodes, model routing,
real test-generate via configured backends, and fleet summary.
"""
from __future__ import annotations

import logging
import time
import uuid

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AIBackendConfig, AIFleetNode, AIModelRoute
from ..schemas import (
    AIBackendConfigIn,
    AIBackendConfigUpdate,
    AIBackendConfigOut,
    AIFleetNodeOut,
    AIModelRouteIn,
    AIModelRouteOut,
    AIFleetSummaryOut,
)

logger = logging.getLogger("truenorth.api.ai_config")

router = APIRouter(prefix="/ai-config", tags=["AI Orchestrator"])


# -- Backends CRUD -------------------------------------------------------
@router.get("/backends", response_model=list[AIBackendConfigOut])
def list_backends(db: Session = Depends(get_db)):
    return db.query(AIBackendConfig).order_by(AIBackendConfig.created_at.desc()).all()


@router.post("/backends", response_model=AIBackendConfigOut, status_code=201)
def create_backend(payload: AIBackendConfigIn, db: Session = Depends(get_db)):
    backend = AIBackendConfig(
        name=payload.name,
        backend_type=payload.backend_type,
        base_url=payload.base_url,
        api_key_encrypted=payload.api_key,
        is_primary=payload.is_primary,
        max_concurrent=payload.max_concurrent,
        timeout_seconds=payload.timeout_seconds,
        notes=payload.notes,
    )
    db.add(backend)
    db.commit()
    db.refresh(backend)
    return backend


@router.get("/backends/{backend_id}", response_model=AIBackendConfigOut)
def get_backend(backend_id: uuid.UUID, db: Session = Depends(get_db)):
    backend = db.get(AIBackendConfig, str(backend_id))
    if not backend:
        raise HTTPException(404, "Backend not found")
    return backend


@router.patch("/backends/{backend_id}", response_model=AIBackendConfigOut)
def update_backend(backend_id: uuid.UUID, payload: AIBackendConfigUpdate, db: Session = Depends(get_db)):
    backend = db.get(AIBackendConfig, str(backend_id))
    if not backend:
        raise HTTPException(404, "Backend not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "api_key":
            setattr(backend, "api_key_encrypted", value)
        else:
            setattr(backend, field, value)
    db.commit()
    db.refresh(backend)
    return backend


@router.delete("/backends/{backend_id}", status_code=204)
def delete_backend(backend_id: uuid.UUID, db: Session = Depends(get_db)):
    backend = db.get(AIBackendConfig, str(backend_id))
    if not backend:
        raise HTTPException(404, "Backend not found")
    db.delete(backend)
    db.commit()


# -- Set Primary ---------------------------------------------------------
@router.post("/backends/{backend_id}/set-primary")
def set_primary_backend(backend_id: uuid.UUID, db: Session = Depends(get_db)):
    backend = db.get(AIBackendConfig, str(backend_id))
    if not backend:
        raise HTTPException(404, "Backend not found")
    for other in db.query(AIBackendConfig).filter(AIBackendConfig.id != str(backend_id)).all():
        other.is_primary = False
    backend.is_primary = True
    db.commit()
    return {"message": f"{backend.name} set as primary AI backend"}


# -- Fleet Nodes ---------------------------------------------------------
@router.get("/backends/{backend_id}/nodes", response_model=list[AIFleetNodeOut])
def list_fleet_nodes(backend_id: uuid.UUID, db: Session = Depends(get_db)):
    return db.query(AIFleetNode).filter(AIFleetNode.backend_id == str(backend_id)).all()


class FleetNodeCreateIn(BaseModel):
    node_name: str
    url: str
    gpu_model: str | None = None
    gpu_vram_gb: float | None = None
    max_requests: int = 10


@router.post("/backends/{backend_id}/nodes", response_model=AIFleetNodeOut, status_code=201)
def add_fleet_node(
    backend_id: uuid.UUID,
    payload: FleetNodeCreateIn,
    db: Session = Depends(get_db),
):
    """Add a GPU/inference node to a backend.  Accepts JSON body."""
    backend = db.get(AIBackendConfig, str(backend_id))
    if not backend:
        raise HTTPException(404, "Backend not found")
    node = AIFleetNode(
        backend_id=str(backend_id),
        node_name=payload.node_name,
        url=payload.url,
        gpu_model=payload.gpu_model,
        gpu_vram_gb=payload.gpu_vram_gb,
        max_requests=payload.max_requests,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


@router.delete("/nodes/{node_id}", status_code=204)
def remove_fleet_node(node_id: uuid.UUID, db: Session = Depends(get_db)):
    node = db.get(AIFleetNode, str(node_id))
    if not node:
        raise HTTPException(404, "Node not found")
    db.delete(node)
    db.commit()


# -- Model Routes --------------------------------------------------------
@router.get("/routes", response_model=list[AIModelRouteOut])
def list_model_routes(db: Session = Depends(get_db)):
    return db.query(AIModelRoute).order_by(AIModelRoute.priority.desc()).all()


@router.post("/routes", response_model=AIModelRouteOut, status_code=201)
def create_model_route(payload: AIModelRouteIn, db: Session = Depends(get_db)):
    route = AIModelRoute(
        model_pattern=payload.model_pattern,
        backend_id=str(payload.backend_id),
        priority=payload.priority,
        tags=payload.tags,
    )
    db.add(route)
    db.commit()
    db.refresh(route)
    return route


@router.delete("/routes/{route_id}", status_code=204)
def delete_model_route(route_id: uuid.UUID, db: Session = Depends(get_db)):
    route = db.get(AIModelRoute, str(route_id))
    if not route:
        raise HTTPException(404, "Route not found")
    db.delete(route)
    db.commit()


# -- Test Generate (real backend routing) --------------------------------
def _call_ollama(base_url: str, prompt: str, timeout: int) -> dict:
    """Call Ollama /api/generate endpoint."""
    r = httpx.post(
        f"{base_url.rstrip('/')}/api/generate",
        json={"model": "llama3.2", "prompt": prompt, "stream": False},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    return {"response": data.get("response", ""), "model": data.get("model", "unknown")}


def _call_openai(base_url: str, api_key: str | None, prompt: str, timeout: int) -> dict:
    """Call OpenAI-compatible /v1/chat/completions endpoint."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = httpx.post(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        json={
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 256,
        },
        headers=headers,
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    msg = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return {"response": msg, "model": data.get("model", "unknown")}


def _call_mock(prompt: str) -> dict:
    """Return a mock response for testing."""
    return {
        "response": f"[Mock AI] Echo: {prompt[:200]}",
        "model": "mock-v1",
    }


@router.post("/test-generate")
def test_generate(
    prompt: str = Query("Hello from TrueNorth Range"),
    db: Session = Depends(get_db),
):
    """Send prompt to the primary AI backend and return the response.

    Falls back to mock if no active backend is configured or backend is unreachable.
    """
    primary = db.query(AIBackendConfig).filter(
        AIBackendConfig.is_primary == True,
        AIBackendConfig.is_active == True,
    ).first()

    if not primary:
        # No configured backend - return mock
        result = _call_mock(prompt)
        result["backend"] = "mock (no primary configured)"
        result["latency_ms"] = 0
        return result

    t0 = time.time()
    try:
        if primary.backend_type == "ollama":
            result = _call_ollama(primary.base_url, prompt, primary.timeout_seconds)
        elif primary.backend_type in ("openai", "azure_openai", "anthropic"):
            result = _call_openai(primary.base_url, primary.api_key_encrypted, prompt, primary.timeout_seconds)
        elif primary.backend_type == "mock":
            result = _call_mock(prompt)
        else:
            result = _call_mock(prompt)
        latency = int((time.time() - t0) * 1000)
        result["backend"] = primary.name
        result["backend_type"] = primary.backend_type
        result["latency_ms"] = latency
        return result
    except httpx.HTTPError as exc:
        logger.warning("AI backend %s unreachable: %s", primary.name, exc)
        return {
            "prompt": prompt,
            "response": f"[Backend unreachable] {primary.name}: {exc}",
            "model": "error",
            "backend": primary.name,
            "latency_ms": int((time.time() - t0) * 1000),
            "error": str(exc),
        }
    except Exception as exc:
        logger.exception("Unexpected error calling AI backend %s", primary.name)
        return {
            "prompt": prompt,
            "response": f"[Error] {exc}",
            "model": "error",
            "backend": primary.name,
            "latency_ms": int((time.time() - t0) * 1000),
            "error": str(exc),
        }


# -- Fleet Summary -------------------------------------------------------
@router.get("/summary", response_model=AIFleetSummaryOut)
def fleet_summary(db: Session = Depends(get_db)):
    backends = db.query(AIBackendConfig).all()
    nodes = db.query(AIFleetNode).all()
    routes = db.query(AIModelRoute).all()
    active_backends = [b for b in backends if b.is_active]
    online_nodes = [n for n in nodes if n.status == "online"]
    by_type: dict[str, int] = {}
    for b in backends:
        by_type[b.backend_type] = by_type.get(b.backend_type, 0) + 1
    return AIFleetSummaryOut(
        total_backends=len(backends),
        active_backends=len(active_backends),
        total_nodes=len(nodes),
        online_nodes=len(online_nodes),
        total_gpu_vram_gb=sum(n.gpu_vram_gb or 0 for n in nodes),
        active_requests=sum(n.current_requests for n in nodes),
        model_routes=len(routes),
        by_backend_type=by_type,
    )
