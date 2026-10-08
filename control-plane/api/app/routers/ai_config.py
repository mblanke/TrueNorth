"""TrueNorth Range -- AI Orchestrator Configuration Router.

DB-backed CRUD for AI backends, fleet nodes, model routing,
real test-generate via configured backends, and fleet summary.

Tenancy: AI configuration is platform-wide, not per tenant. There is one primary
backend for the whole platform (``test_generate`` and ``set_primary_backend`` act
across every row), fleet nodes and model routes carry no tenant at all, the seeded
default backend has a NULL ``tenant_id`` and nothing here ever sets one. The whole
router needs ``ai_config:write``, which only ``admin`` (the TN-Platform-Admins group)
holds. ``AIBackendConfig.tenant_id`` is an unused column; reviewed 2026-10-07.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..ai_backends import get_ai_engine
from ..db import get_db
from ..delete_guard import commit_delete
from ..models import AIBackendConfig, AIFleetNode, AIModelRoute
from ..rbac import Permission, require_permission
from ..schemas import (
    AIBackendConfigIn,
    AIBackendConfigOut,
    AIBackendConfigUpdate,
    AIFleetNodeOut,
    AIFleetSummaryOut,
    AIModelRouteIn,
    AIModelRouteOut,
    AIModelRouteUpdate,
)
from ..secretbox import seal, unseal

logger = logging.getLogger("truenorth.api.ai_config")

# Router-level authentication. AI backend endpoints, model routing and fleet nodes.
#
# Every route here was previously reachable with no credentials at all.
router = APIRouter(prefix="/ai-config", tags=["AI Orchestrator"], dependencies=[Depends(require_permission(Permission.AI_CONFIG_WRITE))])


def _backend(db: Session, backend_id: uuid.UUID) -> AIBackendConfig:
    # tenant-safe: platform-wide config behind a platform-admin-only router (module
    # docstring); there is no tenant predicate to apply.
    backend = db.get(AIBackendConfig, str(backend_id))
    if not backend:
        raise HTTPException(404, "Backend not found")
    return backend


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
        api_key_encrypted=seal(payload.api_key),  # app/secretbox.py: never stored as typed
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
    backend = _backend(db, backend_id)
    return backend


@router.patch("/backends/{backend_id}", response_model=AIBackendConfigOut)
def update_backend(backend_id: uuid.UUID, payload: AIBackendConfigUpdate, db: Session = Depends(get_db)):
    backend = _backend(db, backend_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "api_key":
            backend.api_key_encrypted = seal(value)
        else:
            setattr(backend, field, value)
    db.commit()
    db.refresh(backend)
    return backend


@router.delete("/backends/{backend_id}", status_code=204, response_class=Response)
def delete_backend(backend_id: uuid.UUID, db: Session = Depends(get_db)):
    """Delete a backend with its fleet nodes and the model routes that point at it.

    Nodes and routes have no use without their backend, so they go with it."""
    backend = _backend(db, backend_id)
    # tenant-safe: platform-wide config (module docstring).
    db.query(AIModelRoute).filter(AIModelRoute.backend_id == backend.id).delete(synchronize_session=False)
    db.query(AIFleetNode).filter(AIFleetNode.backend_id == backend.id).delete(synchronize_session=False)
    db.flush()
    db.expire(backend, ["fleet_nodes"])
    db.delete(backend)
    commit_delete(db, "AI backend")


# -- Set Primary ---------------------------------------------------------
@router.post("/backends/{backend_id}/set-primary")
def set_primary_backend(backend_id: uuid.UUID, db: Session = Depends(get_db)):
    backend = _backend(db, backend_id)
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
    _backend(db, backend_id)  # 404 on an unknown backend
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


@router.delete("/nodes/{node_id}", status_code=204, response_class=Response)
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


@router.delete("/routes/{route_id}", status_code=204, response_class=Response)
def delete_model_route(route_id: uuid.UUID, db: Session = Depends(get_db)):
    route = db.get(AIModelRoute, str(route_id))
    if not route:
        raise HTTPException(404, "Route not found")
    db.delete(route)
    db.commit()


@router.patch("/routes/{route_id}", response_model=AIModelRouteOut)
def update_model_route(route_id: uuid.UUID, payload: AIModelRouteUpdate, db: Session = Depends(get_db)):
    route = db.get(AIModelRoute, str(route_id))
    if not route:
        raise HTTPException(404, "Route not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "backend_id":
            setattr(route, field, str(value))
        else:
            setattr(route, field, value)
    db.commit()
    db.refresh(route)
    return route


# -- Known Fleet Nodes (legacy Ollama LAN discovery) -----------------------
# NOTE: this deployment serves models via vLLM behind LiteLLM (OpenAI-compatible,
# host :4000), not Ollama. This discovery path is dormant; the entry below
# describes the R7725 GPU node for reference only. The host comes from the
# environment (LLM_NODE_HOST) so no site address is committed.
KNOWN_OLLAMA_NODES = [
    {"node_name": "r7725", "host": os.getenv("LLM_NODE_HOST", "llm.example.internal"), "port": 11434, "gpu_model": "2x NVIDIA H200 NVL (144GB ea)", "gpu_vram_gb": 288},
]


@router.post("/backends/{backend_id}/discover")
def discover_fleet_nodes(backend_id: uuid.UUID, db: Session = Depends(get_db)):
    """Scan known Ollama nodes on the LAN, upsert fleet node records, and return results."""
    backend = _backend(db, backend_id)

    # The adapter decides what to probe: OpenAI-compatible engines (LiteLLM/vLLM)
    # serve models at the backend base_url, Ollama per LAN node. Unknown types
    # are probed as Ollama nodes, as before.
    engine = get_ai_engine(backend.backend_type, default="ollama")
    api_key = unseal(backend.api_key_encrypted)  # app/secretbox.py
    scan_results = []
    for known in KNOWN_OLLAMA_NODES:
        probe = engine.probe(
            base_url=backend.base_url,
            api_key=api_key,
            host=known["host"],
            port=known["port"],
        )

        # Upsert fleet node
        existing = (
            db.query(AIFleetNode)
            .filter(
                AIFleetNode.backend_id == str(backend_id),
                AIFleetNode.node_name == known["node_name"],
            )
            .first()
        )

        model_names = [m["name"] for m in probe["models"]]
        now = datetime.now(UTC)

        if existing:
            existing.url = probe["url"]
            existing.status = "online" if probe["online"] else "offline"
            existing.gpu_model = known.get("gpu_model")
            existing.gpu_vram_gb = known.get("gpu_vram_gb")
            existing.loaded_models = json.dumps(model_names) if model_names else existing.loaded_models
            existing.last_health_check = now
            node_record = existing
        else:
            node_record = AIFleetNode(
                backend_id=str(backend_id),
                node_name=known["node_name"],
                url=probe["url"],
                status="online" if probe["online"] else "offline",
                gpu_model=known.get("gpu_model"),
                gpu_vram_gb=known.get("gpu_vram_gb"),
                loaded_models=json.dumps(model_names),
                max_requests=10,
                last_health_check=now,
            )
            db.add(node_record)

        scan_results.append(
            {
                "node_name": known["node_name"],
                "url": probe["url"],
                "online": probe["online"],
                "version": probe["version"],
                "gpu_model": known.get("gpu_model"),
                "gpu_vram_gb": known.get("gpu_vram_gb"),
                "model_count": len(probe["models"]),
                "models": probe["models"],
                "running": probe["running"],
            }
        )

    db.commit()
    return {"scanned": len(KNOWN_OLLAMA_NODES), "results": scan_results}


# -- Test Generate (real backend routing) --------------------------------
@router.get("/models")
def list_available_models(db: Session = Depends(get_db)):
    """Return all models across online fleet nodes."""
    nodes = db.query(AIFleetNode).filter(AIFleetNode.status == "online").all()
    models = []
    seen = set()
    for n in nodes:
        try:
            node_models = json.loads(n.loaded_models) if n.loaded_models else []
        except (json.JSONDecodeError, TypeError):
            node_models = []
        for m in node_models:
            if m not in seen:
                seen.add(m)
                models.append({"name": m, "node": n.node_name, "node_url": n.url})
    return {"models": models, "count": len(models)}


def _pick_ollama_node(db: Session, backend_id: str, model: str | None = None) -> AIFleetNode | None:
    """Pick the best online fleet node, optionally one that has the requested model."""
    nodes = (
        db.query(AIFleetNode)
        .filter(
            AIFleetNode.backend_id == backend_id,
            AIFleetNode.status == "online",
        )
        .all()
    )
    if not nodes:
        return None
    if model:
        for n in nodes:
            try:
                node_models = json.loads(n.loaded_models) if n.loaded_models else []
            except (json.JSONDecodeError, TypeError):
                node_models = []
            if model in node_models:
                return n
    # Fall back to node with fewest active requests
    return min(nodes, key=lambda n: n.current_requests)


@router.post("/test-generate")
def test_generate(
    prompt: str = Query("Hello from TrueNorth Range"),
    model: str = Query("llama3.1:latest"),
    db: Session = Depends(get_db),
):
    """Send prompt to the primary AI backend and return the response.

    For Ollama backends, routes through an online fleet node instead of the
    base_url (which may be Open WebUI / a reverse proxy that rejects raw
    Ollama API calls).  Falls back to mock if nothing is reachable.
    """
    primary = (
        db.query(AIBackendConfig)
        .filter(
            AIBackendConfig.is_primary,
            AIBackendConfig.is_active,
        )
        .first()
    )

    if not primary:
        result = get_ai_engine("mock").generate(prompt, base_url="")
        result["backend"] = "mock (no primary configured)"
        result["latency_ms"] = 0
        return result

    # Unknown backend types answer from the mock, as they always have.
    engine = get_ai_engine(primary.backend_type, default="mock")
    # Outside the try: a missing or wrong TN_SECRETS_KEY is a 503 naming the key
    # (app/secretbox.py), not an "engine error" answer.
    api_key = unseal(primary.api_key_encrypted)
    t0 = time.time()
    try:
        target_url = primary.base_url
        node_label = None
        if engine.uses_fleet_nodes:
            # Route through a real fleet node, not the base_url (Open WebUI);
            # with no nodes discovered yet, fall back to base_url.
            node = _pick_ollama_node(db, str(primary.id), model)
            if node:
                target_url = node.url
                node_label = f"{node.node_name} ({node.url})"
            else:
                node_label = primary.base_url
        result = engine.generate(
            prompt,
            base_url=target_url,
            api_key=api_key,
            timeout=primary.timeout_seconds,
            model=model,
        )
        if node_label is not None:
            result["node"] = node_label
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
