"""The worker's copy of the Greyspace stack generator is the API's, byte for byte (ADR 0007).

The API renders a range's Greyspace configuration (GET /ranges/{id}/greyspace/config) and
the worker renders the same stack onto gs-core; the two images cannot import each other
(MOSA api_imports_worker), so the worker carries copies (worker/greyspace_runtime). A
drift would mean the API describes one stack and gs-core runs another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "control-plane/api/app/greyspace"
COPY = ROOT / "control-plane/worker/worker/greyspace_runtime"
FILES = ("config.py", "manifest.py", "fixture.py", "npc.py", "npc_agent.py", "gs_cli.py")


@pytest.mark.parametrize("name", FILES)
def test_the_worker_copy_matches_the_api(name):
    assert (COPY / name).read_bytes() == (SOURCE / name).read_bytes(), (
        f"worker/greyspace_runtime/{name} differs from app/greyspace/{name}: "
        f"cp control-plane/api/app/greyspace/{name} control-plane/worker/worker/greyspace_runtime/"
    )


def test_nothing_else_is_copied():
    assert sorted(p.name for p in COPY.glob("*.py") if p.name != "__init__.py") == sorted(FILES)
