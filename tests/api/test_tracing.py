"""Unit tests for OpenTelemetry tracing setup."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


class TestSetupTracing:
    def test_noop_when_disabled(self):
        """setup_tracing is a no-op when OTEL_ENABLED is false."""
        with patch.dict(os.environ, {"OTEL_ENABLED": "false"}, clear=False):
            # Re-import to pick up env
            import importlib

            from app import tracing

            importlib.reload(tracing)
            app_mock = MagicMock()
            tracing.setup_tracing(app_mock)
            # No instrumentation should be applied
            # Just assert it doesn't raise

    def test_enabled_requires_packages(self):
        """When OTEL_ENABLED=true but packages missing, it logs warning and returns."""
        with patch.dict(os.environ, {"OTEL_ENABLED": "true"}, clear=False):
            import importlib

            from app import tracing

            importlib.reload(tracing)

            with (
                patch.dict("sys.modules", {"opentelemetry": None}),
                patch("builtins.__import__", side_effect=ImportError("mock")),
            ):
                app_mock = MagicMock()
                # Should not raise even if imports fail
                tracing.setup_tracing(app_mock)

    def test_enabled_applies_every_instrumentor(self, monkeypatch, caplog):
        """With the pinned packages installed, every instrumentor actually loads.

        Each import in setup_tracing is wrapped in `except ImportError`, so an
        OpenTelemetry upgrade that renames a module would silently switch tracing
        off. This runs the real path and requires every "Instrumented ..." line.
        """
        import logging

        from app import tracing
        from fastapi import FastAPI
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from sqlalchemy import create_engine

        monkeypatch.setattr(tracing, "OTEL_ENABLED", True)
        # Keep the global tracer provider untouched for the rest of the suite.
        monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", lambda provider: None)

        caplog.set_level(logging.DEBUG, logger="truenorth.tracing")
        try:
            tracing.setup_tracing(FastAPI(), db_engine=create_engine("sqlite://"))
        finally:
            for instrumentor in (
                HTTPXClientInstrumentor(),
                RedisInstrumentor(),
                CeleryInstrumentor(),
                SQLAlchemyInstrumentor(),
            ):
                if instrumentor.is_instrumented_by_opentelemetry:
                    instrumentor.uninstrument()

        messages = {r.getMessage() for r in caplog.records}
        for part in ("FastAPI", "SQLAlchemy", "httpx", "Redis", "Celery"):
            assert f"Instrumented {part}" in messages, f"{part} instrumentation did not load"
