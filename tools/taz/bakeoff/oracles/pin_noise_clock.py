"""pytest plugin for the noise-tamper-flake oracle: pin the noise router's clock.

The plan an agent receives is generated from its time window. At 2026-10-15T03:00Z the
first action planned for ws01 is already ``admin_scan``, so a test that "forges" a record by
setting kind=admin_scan posts the genuine signed action, which the API rightly accepts.
That is the window PR #158 reproduced the flake in. A fixed test passes here; the
original test fails here every time.

Loaded with ``PYTHONPATH=<kit>/oracles pytest -p pin_noise_clock``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

PINNED = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _pin_noise_clock(monkeypatch):
    from app.routers import noise

    monkeypatch.setattr(noise, "_now", lambda: PINNED)
    yield
