"""OpenTelemetry distributed tracing setup for TrueNorth API.

Initialises tracer provider, configures instrumentation for FastAPI,
SQLAlchemy, httpx, Celery, and Redis.  Spans are exported via OTLP/gRPC.

Configuration (environment variables):
    OTEL_ENABLED            Enable tracing (default: false)
    OTEL_SERVICE_NAME       Service name tag (default: truenorth-api)
    OTEL_EXPORTER_OTLP_ENDPOINT  Collector endpoint (default: http://localhost:4317)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.engine import Engine

logger = logging.getLogger("truenorth.tracing")

OTEL_ENABLED = os.getenv("OTEL_ENABLED", "false").lower() == "true"
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "truenorth-api")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")


def setup_tracing(app: FastAPI, db_engine: Engine | None = None) -> None:
    """Initialise OpenTelemetry tracing if enabled.

    Safe to call even when ``OTEL_ENABLED`` is ``false``; in that case this
    is a no-op.
    """
    if not OTEL_ENABLED:
        logger.info("OpenTelemetry tracing disabled (OTEL_ENABLED != true)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OpenTelemetry packages not installed — tracing disabled. "
            "Install: pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc"
        )
        return

    # -- Provider & exporter ------------------------------------------------
    resource = Resource.create({"service.name": OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    logger.info("OpenTelemetry tracer configured — endpoint=%s service=%s", OTEL_ENDPOINT, OTEL_SERVICE_NAME)

    # -- FastAPI instrumentation --------------------------------------------
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        logger.info("Instrumented FastAPI")
    except ImportError:
        logger.debug("FastAPI OTEL instrumentation not available")

    # -- SQLAlchemy instrumentation -----------------------------------------
    if db_engine is not None:
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            SQLAlchemyInstrumentor().instrument(engine=db_engine)
            logger.info("Instrumented SQLAlchemy")
        except ImportError:
            logger.debug("SQLAlchemy OTEL instrumentation not available")

    # -- httpx instrumentation ----------------------------------------------
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
        logger.info("Instrumented httpx")
    except ImportError:
        logger.debug("httpx OTEL instrumentation not available")

    # -- Redis instrumentation ----------------------------------------------
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
        logger.info("Instrumented Redis")
    except ImportError:
        logger.debug("Redis OTEL instrumentation not available")

    # -- Celery instrumentation ---------------------------------------------
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        CeleryInstrumentor().instrument()
        logger.info("Instrumented Celery")
    except ImportError:
        logger.debug("Celery OTEL instrumentation not available")
