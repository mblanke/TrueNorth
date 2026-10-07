"""TrueNorth Range - Control-Plane API.

Production FastAPI application with:
  - Router-based decomposition (fine-grained RBAC)
  - Security middleware (rate limiting, headers, request ID, logging, sanitization)
  - Production WebSocket manager (Redis pub/sub, heartbeat, connection caps)
  - Internal event bus (Redis-backed domain events)
  - Telemetry proxy to OpenSearch
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import range_leases, range_ops, ws_auth  # noqa: F401 — range_leases, range_ops: register their tables
from .auth import CurrentUser, get_current_user
from .auth_backends import get_auth_backend
from .db import Base, engine, get_db
from .models import Range, Tenant, User, UserRole
from .rbac import Permission, require_permission
from .schemas import HealthOut
from .search_backends import get_search_backend
from .search_backends.query import MAX_QUERY_LENGTH, QueryError, parse_query
from .telemetry_mitre import tag_event
from .tenancy import get_owned
from .versioning import SERVER_PREFIX, VersionPrefixMiddleware

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
    # Production sets both false (compose.prod.yml, the installer): Alembic owns the
    # schema there, and the hardcoded dev admin must not exist. Until 2026-09-27
    # neither flag was read, so both ran in production regardless of the setting.
    if _env_flag("DB_AUTO_CREATE"):
        Base.metadata.create_all(bind=engine)
    _seed_dev_data(dev_account=_env_flag("SEED_DEV_DATA"))
    # Build the auth backend now so a bad AUTH_BACKEND / OIDC_* setting stops the
    # process at boot instead of turning every authenticated request into a 500.
    logger.info("Auth backend: %s", type(get_auth_backend()).__name__)
    # Course publications a previous process left mid-way resume in the background
    # (app/course_publishing); a Moodle that is down only delays them.
    if os.getenv("COURSE_PUBLISH_RESUME", "true").lower() == "true":
        from .course_publishing.runner import resume_on_start

        asyncio.get_running_loop().run_in_executor(None, resume_on_start)
    # Lab sessions advance (readiness, expiry, teardown) without anyone asking.
    lab_sweep = None
    if os.getenv("LAB_SESSIONS_SWEEP", "true").lower() == "true":
        from .lab_sessions.runner import loop as lab_loop

        lab_sweep = asyncio.create_task(lab_loop())

    # Range operations the broker did not take (down, or a process that died between
    # the commit and the send) are re-sent (app/range_ops). One sender per operation.
    from .db import SessionLocal
    from .range_ops.service import redispatch_interval, redispatch_loop

    interval = redispatch_interval()
    range_resend = asyncio.create_task(redispatch_loop(SessionLocal, interval)) if interval > 0 else None

    # WebSocket heartbeat (no Redis): drops dead sockets and closes those whose token has
    # expired (app/ws_auth.py). Nothing started it before, so a socket outlived its user.
    if _env_flag("WS_HEARTBEAT"):
        await app.state.ws_manager.start_local()

    yield

    if range_resend is not None:
        range_resend.cancel()
    if lab_sweep is not None:
        lab_sweep.cancel()

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


def _env_flag(name: str) -> bool:
    """On unless set to a false-ish value, so development keeps its defaults."""
    return os.getenv(name, "true").strip().lower() not in ("0", "false", "no", "off")


def _seed_dev_data(dev_account: bool = True) -> None:
    """Seed reference data, and with ``dev_account`` the dev tenant and admin if DB is empty.

    The reference seeders run either way; the installer relies on them. Only the
    hardcoded admin@truenorth.local is development-only: in production the first
    administrator comes from app.bootstrap_admin.
    """
    from .db import SessionLocal

    db = SessionLocal()
    try:
        # Use deterministic UUIDs that match the AUTH_DISABLED dev stub in auth.py
        _dev_uuid = "00000000-0000-0000-0000-000000000001"
        if not dev_account and db.query(User).filter(User.email == "admin@truenorth.local").first():
            # Starts before SEED_DEV_DATA was honoured created this account wherever the
            # tenants table was empty, production included. Say so rather than delete a
            # user row other tables may reference.
            logger.warning(
                "SEED_DEV_DATA is off but the development admin admin@truenorth.local exists "
                "(id %s). Deactivate or remove it: it is not a real person.",
                _dev_uuid,
            )
        if dev_account and db.query(Tenant).count() == 0:
            tenant = Tenant(id=_dev_uuid, name="Default Org", slug="default")
            db.add(tenant)
            db.flush()
            admin = User(
                id=_dev_uuid,
                email="admin@truenorth.local",
                display_name="Dev Admin",
                role=UserRole.admin,
                tenant_id=_dev_uuid,
                keycloak_id="dev-admin",
            )
            db.add(admin)
            db.commit()
            logger.info("Seeded default tenant and admin user")
        # Seed reference data
        from .seed import (
            seed_ai_backends,
            seed_auth_zones,
            seed_infrastructure,
            seed_nations_and_coalitions,
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
    # The published base path. Unversioned paths remain as aliases (app/versioning.py).
    servers=[{"url": SERVER_PREFIX}],
)

# -- CORS ------------------------------------------------------------------
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:4200,http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-CSRF-Token",
        "Accept",
        "Origin",
    ],
    expose_headers=["X-Request-ID", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
    max_age=86400,
)

# -- Security middleware (rate limit, headers, request ID, logging) ---------
from .middleware import setup_middleware

setup_middleware(app, redis_url=os.getenv("REDIS_URL"))

# -- Distributed tracing (OpenTelemetry) ------------------------------------
from .tracing import setup_tracing

setup_tracing(app, db_engine=engine)

# -- Prometheus metrics -----------------------------------------------------
from .metrics import PrometheusMiddleware
from .metrics import router as metrics_router

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
    ad_sync_router,
    adaptive_learning_router,
    admin_router,
    ai_authoring_router,
    ai_config_router,
    auth_zones_router,
    certifications_router,
    collective_exercises_router,
    competency_router,
    course_publications_router,
    course_releases_router,
    courses_router,
    curriculum_router,
    detection_rules_router,
    directory_router,
    exercise_forge_router,
    exercises_router,
    golden_images_router,
    hypervisors_router,
    injectors_router,
    integrations_router,
    kit_router,
    lab_sessions_router,
    learning_paths_router,
    lti_router,
    network_devices_router,
    onboarding_router,
    ops_center_router,
    proxmox_router,
    qsp_router,
    quizzes_router,
    ranges_router,
    registration_router,
    scenarios_router,
    scheduling_router,
    storage_router,
    templates_router,
    threat_intel_router,
    transcript_router,
)

# Identity intake. Registration is mounted first because /auth/me is the one
# endpoint reachable without a users row — it is how the SPA learns whether the
# caller needs to register, is awaiting approval, or is a full user.
app.include_router(registration_router)
app.include_router(onboarding_router)

# Core routers
app.include_router(ranges_router)
app.include_router(exercises_router)
app.include_router(collective_exercises_router)
app.include_router(templates_router)
app.include_router(scenarios_router)
app.include_router(injectors_router)
app.include_router(ai_authoring_router)
app.include_router(admin_router)
app.include_router(proxmox_router)
app.include_router(scheduling_router)
# LMS & Integration routers
app.include_router(courses_router)
app.include_router(course_releases_router)
app.include_router(course_publications_router)
app.include_router(lab_sessions_router)
app.include_router(learning_paths_router)
app.include_router(transcript_router)
app.include_router(competency_router)
app.include_router(certifications_router)
app.include_router(integrations_router)
app.include_router(lti_router)
# Infrastructure & Directory routers
app.include_router(hypervisors_router)
app.include_router(golden_images_router)
app.include_router(ai_config_router)
app.include_router(directory_router)
app.include_router(ad_sync_router)
app.include_router(auth_zones_router)
app.include_router(storage_router)
app.include_router(network_devices_router)
app.include_router(kit_router)
# Threat Intelligence
app.include_router(threat_intel_router)
app.include_router(detection_rules_router)
# AI Exercise Forge (EPIC 1)
app.include_router(exercise_forge_router)
# CFITES / QSP qualification spine (crosswalk -> qualifications/POs/EOs)
app.include_router(qsp_router)
# Curriculum Forge (EPIC: LLM curriculum ingestion -> content/quizzes/ranges)
app.include_router(curriculum_router)
app.include_router(quizzes_router)
# Adaptive Learning (EPIC 3)
app.include_router(adaptive_learning_router)
app.include_router(ops_center_router)

# -- API versioning: /api/v1/... -> canonical route (docs/adr/0002) ---------
# Added last so it is the outermost middleware: rate limiting, metrics and tracing all
# see the canonical path, not one per version alias.
app.add_middleware(VersionPrefixMiddleware)


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
    """Real-time event stream for a signed-in user, on a channel of their tenant
    (app/ws_auth.py: ``range.<id>``, ``exercise.<id>``, ``tenant.<id>``; admins
    ``system.*``).

    The access token is the second subprotocol: ``new WebSocket(url, ["bearer", token])``.
    No valid token, or a channel the user may not open: closed with 1008. Reply
    ``{"type": "pong"}`` to each ``{"type": "ping"}`` or the socket is dropped.
    """
    db_gen = app.dependency_overrides.get(get_db, get_db)()  # a session only for the handshake
    db = next(db_gen)
    try:
        who = await ws_auth.ws_user(ws, db)
        allowed = who is not None and ws_auth.authorize(channel, who.user, db)
    finally:
        db_gen.close()
    if not allowed:
        await ws.close(code=1008)
        return
    user = who.user
    conn_id = await ws_manager.connect(
        ws,
        channel,
        user_id=user.id,
        tenant_id=user.tenant_id,
        subprotocol=ws_auth.SUBPROTOCOL if ws_auth.bearer_token(ws) else None,
        expires_at=who.expires_at,
    )

    def room_ok(room_id: str, *, joined: bool) -> bool:
        """A room of the user's tenant; for leaving, sending or listing, one this socket joined."""
        if ws_auth.canonical_id(room_id) is None:
            return False
        conn = ws_manager.connections.get(conn_id)
        if joined and (conn is None or f"room.{room_id}" not in conn.channels):
            return False
        db_gen = app.dependency_overrides.get(get_db, get_db)()
        try:
            return ws_auth.room_allowed(room_id, user, next(db_gen))
        finally:
            db_gen.close()

    try:
        while True:
            data = await ws.receive_text()
            if len(data) > ws_auth.MAX_FRAME_BYTES:
                await ws_manager.send_to_connection(conn_id, {"type": "error", "detail": "frame too large"})
                continue
            try:
                msg = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                await ws_manager.send_to_connection(conn_id, {"type": "ack", "data": data})
                continue

            if isinstance(msg, dict) and msg.get("type") == "pong":
                ws_manager.handle_pong(conn_id)
                continue
            action = msg.get("action") if isinstance(msg, dict) else None
            room_id = str(msg.get("room_id", "")) if isinstance(msg, dict) else ""
            if action in ("join_room", "leave_room", "room_message", "room_members") and not room_ok(
                room_id, joined=action != "join_room"
            ):
                await ws_manager.send_to_connection(conn_id, {"type": "error", "detail": "room not allowed"})
                continue
            if action == "join_room":
                await ws_manager.join_room(conn_id, room_id, ws_auth.display_name(msg.get("display_name")))
            elif action == "leave_room":
                await ws_manager.leave_room(conn_id, room_id)
            elif action == "room_message":
                payload = msg.get("data") if isinstance(msg.get("data"), dict) else {}
                await ws_manager.broadcast_to_room(
                    room_id,
                    conn_id,
                    ws_auth.room_message_type(msg.get("type")),
                    {**payload, "sender_user_id": user.id},  # stamped here: a client cannot speak for another
                )
            elif action == "room_members":
                members = ws_manager.get_room_members(room_id)
                await ws_manager.send_to_connection(conn_id, {"type": "room_members", "members": members})
            else:
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
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TELEMETRY_WRITE)),
):
    """Ingest telemetry events into a range's index.  **Permission: telemetry:write**

    The range must belong to the caller's tenant (404 otherwise). Students cannot write:
    detection objectives are scored against this index. Each event is stored with a
    ``mitre_technique`` list when one is known: its own ``mitre_technique`` /
    ``technique_id`` if that is an ATT&CK ID, else one mapped from ``event_type``.
    """
    get_owned(db, Range, range_id, user, not_found="Range not found")
    index = f"range-{range_id}"
    for event in events:
        event["range_id"] = str(range_id)
        event["tenant_id"] = user.tenant_id
        if "@timestamp" not in event:
            event["@timestamp"] = datetime.now(UTC).isoformat()
        tag_event(event)  # mitre_technique, from the event or its event_type
    accepted = await get_search_backend().ingest(index, events)
    return {"accepted": accepted}


@app.get("/telemetry/{range_id}/search", tags=["telemetry"])
async def search_telemetry(
    range_id: uuid.UUID,
    q: str = Query(
        "*",
        max_length=MAX_QUERY_LENGTH,
        description="field:value, field:\"a phrase\", field:prefix*, field:* (exists) and free text, ANDed",
    ),
    size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Search a range's telemetry. The range must belong to the caller's tenant (404 otherwise).

    ``q`` is a small closed grammar (app/search_backends/query.py), never OpenSearch
    ``query_string``: no regex, fuzzy, leading wildcards or ``_``-prefixed fields.
    A query outside it is a 422.
    """
    get_owned(db, Range, range_id, user, not_found="Range not found")
    try:
        parse_query(q)
    except QueryError as exc:
        raise HTTPException(422, f"Invalid search query: {exc}") from exc
    index = f"range-{range_id}"
    return await get_search_backend().search(index, q, size)
