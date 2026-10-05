"""Services start with LOG_LEVEL in the case compose and the installer write it ("info").

compose.prod.yml passes LOG_LEVEL=${LOG_LEVEL:-info} and the installer renders
LOG_LEVEL=info, but logging.basicConfig only knows "INFO": the API and the AI orchestrator
both died at import with "ValueError: Unknown level: 'info'" (found bringing the production
compose stack up, 2026-10-05).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("service", "module"), [("control-plane/api", "app.main"), ("ai-orchestrator", "app.main")])
@pytest.mark.parametrize("level", ["info", "debug", "WARNING"])
def test_a_service_imports_with_any_case_of_log_level(service, module, level):
    env = {**os.environ, "LOG_LEVEL": level, "PYTHONPATH": str(ROOT / service), "AUTH_DISABLED": "true",
           "DATABASE_URL": "sqlite://", "OTEL_ENABLED": "false"}
    r = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=ROOT / service, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1500:]
