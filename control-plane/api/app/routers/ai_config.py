"""TrueNorth Range -- AI Orchestrator Configuration Router.

DB-backed CRUD for AI backends, fleet nodes, model routing,
real test-generate via configured backends, and fleet summary.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AIBackendConfig, AIFleetNode, AIModelRoute
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
            backend.api_key_encrypted = value
        else:
            setattr(backend, field, value)
    db.commit()
    db.refresh(backend)
    return backend


@router.delete("/backends/{backend_id}", status_code=204, response_class=Response)
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


# -- Known Fleet Nodes (hardcoded for LAN discovery) -----------------------
KNOWN_OLLAMA_NODES = [
    {"node_name": "wile", "host": "192.168.1.50", "port": 11434, "gpu_model": "Dell Pro Max GB10", "gpu_vram_gb": 128},
    {
        "node_name": "roadrunner",
        "host": "192.168.1.51",
        "port": 11434,
        "gpu_model": "Dell Pro Max GB10",
        "gpu_vram_gb": 128,
    },
]


def _probe_ollama_node(host: str, port: int, timeout: float = 5.0) -> dict:
    """Probe a single Ollama node and return status + model list."""
    base = f"http://{host}:{port}"
    result: dict = {"url": base, "online": False, "models": [], "version": None, "running": []}
    try:
        # Get version
        vr = httpx.get(f"{base}/api/version", timeout=timeout)
        vr.raise_for_status()
        result["version"] = vr.json().get("version")

        # Get all available models
        tr = httpx.get(f"{base}/api/tags", timeout=timeout)
        tr.raise_for_status()
        models = tr.json().get("models", [])
        result["models"] = [
            {
                "name": m["name"],
                "size_bytes": m.get("size", 0),
                "family": m.get("details", {}).get("family", ""),
                "parameter_size": m.get("details", {}).get("parameter_size", ""),
                "quantization": m.get("details", {}).get("quantization_level", ""),
            }
            for m in models
        ]

        # Get currently loaded/running models
        pr = httpx.get(f"{base}/api/ps", timeout=timeout)
        if pr.status_code == 200:
            result["running"] = [rm.get("name", "") for rm in pr.json().get("models", [])]

        result["online"] = True
    except Exception as exc:
        logger.warning("Probe failed for %s:%s — %s", host, port, exc)
    return result


@router.post("/backends/{backend_id}/discover")
def discover_fleet_nodes(backend_id: uuid.UUID, db: Session = Depends(get_db)):
    """Scan known Ollama nodes on the LAN, upsert fleet node records, and return results."""
    backend = db.get(AIBackendConfig, str(backend_id))
    if not backend:
        raise HTTPException(404, "Backend not found")

    scan_results = []
    for known in KNOWN_OLLAMA_NODES:
        probe = _probe_ollama_node(known["host"], known["port"])

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
def _call_ollama(base_url: str, prompt: str, timeout: int, model: str = "llama3.1:latest") -> dict:
    """Call Ollama /api/generate endpoint on a real Ollama node."""
    r = httpx.post(
        f"{base_url.rstrip('/')}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
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
        result = _call_mock(prompt)
        result["backend"] = "mock (no primary configured)"
        result["latency_ms"] = 0
        return result

    t0 = time.time()
    try:
        if primary.backend_type == "ollama":
            # Route through a real fleet node, not the base_url (Open WebUI)
            node = _pick_ollama_node(db, str(primary.id), model)
            if node:
                target_url = node.url
                node_label = f"{node.node_name} ({node.url})"
            else:
                # No fleet nodes discovered yet — fall back to base_url
                target_url = primary.base_url
                node_label = primary.base_url
            result = _call_ollama(target_url, prompt, primary.timeout_seconds, model)
            result["node"] = node_label
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
