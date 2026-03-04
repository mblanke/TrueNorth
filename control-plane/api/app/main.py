"""TrueNorth Range - Control-Plane API.

Production FastAPI application with:
  - Router-based decomposition (fine-grained RBAC)
  - Security middleware (rate limiting, headers, request ID, logging, sanitization)
  - Production WebSocket manager (Redis pub/sub, heartbeat, connection caps)
  - Internal event bus (Redis-backed domain events)
  - Telemetry proxy to OpenSearch
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from .auth import CurrentUser, get_current_user
from .db import Base, engine, get_db
from .models import Tenant, User, UserRole
from .schemas import HealthOut

logger = logging.getLogger("truenorth.api")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

APP_VERSION = "0.1.0"


# -- Lifecycle -------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown.

    DB tables are created eagerly.  The event bus and WebSocket manager
    are *not* started here to avoid Redis connection issues in non-Redis
    environments (tests, CI).  They start lazily on first use, or call
    ``/admin/startup`` in production bootstrap scripts.
    """
    logger.info("Starting TrueNorth Range API v%s", APP_VERSION)
    Base.metadata.create_all(bind=engine)
    _seed_dev_data()

    yield

    # Graceful shutdown of any started subsystems
    ws_mgr = getattr(app.state, "ws_manager", None)
    bus = getattr(app.state, "event_bus", None)
    for subsystem in (ws_mgr, bus):
        if subsystem and hasattr(subsystem, "shutdown"):
            try:
                await subsystem.shutdown()
            except Exception:
                logger.debug("Subsystem shutdown error (non-fatal)", exc_info=True)
    logger.info("Shutting down TrueNorth Range API")


def _seed_dev_data() -> None:
    """Seed development data if DB is empty."""
    from .db import SessionLocal

    db = SessionLocal()
    try:
        # Use deterministic UUIDs that match the AUTH_DISABLED dev stub in auth.py
        _DEV_UUID = "00000000-0000-0000-0000-000000000001"
        if db.query(Tenant).count() == 0:
            tenant = Tenant(id=_DEV_UUID, name="Default Org", slug="default")
            db.add(tenant)
            db.flush()
            admin = User(
                id=_DEV_UUID,
                email="admin@truenorth.local",
                display_name="Dev Admin",
                role=UserRole.admin,
                tenant_id=_DEV_UUID,
                keycloak_id="dev-admin",
            )
            db.add(admin)
            db.commit()
            logger.info("Seeded default tenant and admin user")
        # Seed reference data
        from .seed import (
            seed_nations_and_coalitions,
            seed_auth_zones,
            seed_infrastructure,
            seed_ai_backends,
        )
        seed_nations_and_coalitions(db)
        seed_auth_zones(db)
        seed_infrastructure(db)
        seed_ai_backends(db)
    except Exception as e:
        db.rollback()
        logger.warning("Seed failed (may already exist): %s", e)
    finally:
        db.close()


# -- App creation ----------------------------------------------------------
app = FastAPI(
    title="TrueNorth Range API",
    version=APP_VERSION,
    description="Control-plane API for the TrueNorth Range cyber training platform",
    lifespan=lifespan,
)

# -- CORS ------------------------------------------------------------------
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:4200,http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -- Security middleware (rate limit, headers, request ID, logging) ---------
from .middleware import setup_middleware

setup_middleware(app, redis_url=os.getenv("REDIS_URL"))

# -- Prometheus metrics -----------------------------------------------------
from .metrics import router as metrics_router, PrometheusMiddleware
app.add_middleware(PrometheusMiddleware)
app.include_router(metrics_router)

# -- Event bus (registered but NOT started) ---------------------------------
from .events import setup_event_bus

event_bus = setup_event_bus(app, redis_url=os.getenv("REDIS_URL"))

# -- WebSocket manager (registered but NOT started) -------------------------
from .websocket_manager import WebSocketManager

ws_manager = WebSocketManager(redis_url=os.getenv("REDIS_URL"))
app.state.ws_manager = ws_manager

# -- Routers (fine-grained RBAC via require_permission) --------------------
from .routers import (
    admin_router,
    exercises_router,
    proxmox_router,
    scheduling_router,
    ranges_router,
    scenarios_router,
    templates_router,
    courses_router,
    learning_paths_router,
    transcript_router,
    competency_router,
    certifications_router,
    integrations_router,
    lti_router,
    hypervisors_router,
    ai_config_router,
    directory_router,
    ad_sync_router,
    auth_zones_router,
    storage_router,
    network_devices_router,
    kit_router,
)

# Core routers
app.include_router(ranges_router)
app.include_router(exercises_router)
app.include_router(templates_router)
app.include_router(scenarios_router)
app.include_router(admin_router)
app.include_router(proxmox_router)
app.include_router(scheduling_router)
# LMS & Integration routers
app.include_router(courses_router)
app.include_router(learning_paths_router)
app.include_router(transcript_router)
app.include_router(competency_router)
app.include_router(certifications_router)
app.include_router(integrations_router)
app.include_router(lti_router)
# Infrastructure & Directory routers
app.include_router(hypervisors_router)
app.include_router(ai_config_router)
app.include_router(directory_router)
app.include_router(ad_sync_router)
app.include_router(auth_zones_router)
app.include_router(storage_router)
app.include_router(network_devices_router)
app.include_router(kit_router)


# -- Health check (backwards-compatible format) ----------------------------
@app.get("/health", response_model=HealthOut, tags=["health"])
def health_check(db: Session = Depends(get_db)):
    """Quick health check - liveness + dependency flags."""
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    redis_ok = False
    try:
        import redis as sync_redis

        r = sync_redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/15"),
            socket_connect_timeout=1,
        )
        redis_ok = bool(r.ping())
        r.close()
    except Exception:
        pass

    return HealthOut(
        status="ok",
        version=APP_VERSION,
        app="truenorth-range",
        db=db_ok,
        redis=redis_ok,
    )


# -- Deep health checks (readiness + full dependency audit) ----------------
from .health import HealthChecker

_checker = HealthChecker()


@app.get("/health/ready", tags=["health"], summary="Readiness probe")
async def readiness():
    """Readiness probe - checks DB + Redis connectivity."""
    from dataclasses import asdict

    result = await _checker.readiness()
    return asdict(result)


@app.get("/health/deep", tags=["health"], summary="Deep health check")
async def deep_health():
    """Full dependency audit of all backing services."""
    from dataclasses import asdict

    result = await _checker.deep_check()
    return asdict(result)


# -- WebSocket endpoint ----------------------------------------------------
@app.websocket("/ws/{channel}")
async def websocket_endpoint(ws: WebSocket, channel: str):
    """Real-time event stream. Channels: 'ranges', 'exercises', 'all'."""
    conn_id = await ws_manager.connect(ws, channel)
    try:
        while True:
            data = await ws.receive_text()
            await ws_manager.send_to_connection(conn_id, {"type": "ack", "data": data})
    except WebSocketDisconnect:
        await ws_manager.disconnect(conn_id)
    except Exception:
        await ws_manager.disconnect(conn_id)


# -- Telemetry proxy (OpenSearch) ------------------------------------------
@app.post("/telemetry/{range_id}/events", tags=["telemetry"], status_code=202)
async def ingest_telemetry(
    range_id: uuid.UUID,
    events: list[dict],
    user: CurrentUser = Depends(get_current_user),
):
    """Ingest telemetry events into OpenSearch."""
    import httpx

    os_url = os.getenv("OPENSEARCH_URL", "http://opensearch:9200")
    index = f"range-{range_id}"
    bulk_body = ""
    for event in events:
        event["range_id"] = str(range_id)
        event["tenant_id"] = user.tenant_id
        if "@timestamp" not in event:
            event["@timestamp"] = datetime.now(timezone.utc).isoformat()
        bulk_body += json.dumps({"index": {"_index": index}}) + "\n"
        bulk_body += json.dumps(event) + "\n"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{os_url}/_bulk",
                content=bulk_body,
                headers={"Content-Type": "application/x-ndjson"},
            )
            resp.raise_for_status()
    except Exception as e:
        logger.error("OpenSearch ingest error: %s", e)
        raise HTTPException(502, f"OpenSearch error: {e}")
    return {"accepted": len(events)}


@app.get("/telemetry/{range_id}/search", tags=["telemetry"])
async def search_telemetry(
    range_id: uuid.UUID,
    q: str = Query("*", description="OpenSearch query string"),
    size: int = Query(50, le=500),
    user: CurrentUser = Depends(get_current_user),
):
    """Search telemetry events in OpenSearch."""
    import httpx

    os_url = os.getenv("OPENSEARCH_URL", "http://opensearch:9200")
    index = f"range-{range_id}"
    body = {
        "query": {"query_string": {"query": q}},
        "size": size,
        "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{os_url}/{index}/_search", json=body)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("OpenSearch search error: %s", e)
        raise HTTPException(502, f"OpenSearch error: {e}")