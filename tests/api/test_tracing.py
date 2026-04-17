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
