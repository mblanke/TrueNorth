"""Helpers shared by test modules in different directories.

tests/ is on sys.path (its conftest.py puts it there under pytest's default import mode),
so a module here imports as ``_shared`` whether pytest runs as ``pytest`` or
``python -m pytest``. Importing one test module from another (``tests.api...``) works
only under ``python -m pytest``, which is how this broke CI on 2026-10-07.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import UserRole
from app.search_backends import BaseSearchBackend, SearchBackendError, SearchMatch

REPO_ROOT = Path(__file__).resolve().parents[1]
API = REPO_ROOT / "control-plane/api"


# -- Alembic ------------------------------------------------------------------------------
def _run_upgrade_head(database_url: str) -> None:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "DATABASE_URL": database_url,
        "PYTHONPATH": str(API),
        "AUTH_DISABLED": "true",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        "alembic upgrade head failed on an empty database:\n" + result.stdout[-3000:] + "\n" + result.stderr[-3000:]
    )




# -- detection credit (tests/api/test_detections.py and the Postgres tests) ---------------
DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
STARTED = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

SCENARIO = """
name: s
version: "1"
range_template: t
variables:
  c2_domain: northwind-update.example
timeline: [{t: "0:00"}]
objectives:
  - id: detect_c2
    type: detection
    validator: opensearch_query
    params: {query: 'event_type:http AND url.domain:*{{ c2_domain }}*', min_hits: 2}
    points: 40
  - id: detect_phish
    type: detection
    validator: validate.opensearch_query
    params: {query: 'event_type:email AND attachment.name:*.docm'}
    points: 10
  - id: undefined_var
    type: detection
    validator: opensearch_query
    params: {query: 'src:{{ attacker_ip }}'}
    points: 5
  - id: write_report
    type: deliverable
    validator: deliverable_check
    points: 5
"""

_TERM = re.compile(r'([\w.@]+):("([^"]*)"|\S+)')


def _get(event: dict, dotted: str):
    if dotted in event:
        return event[dotted]
    cur = event
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class FakeStore(BaseSearchBackend):
    """Understands what credit.judge sends: bool.filter of query_string (field:value AND
    terms, * wildcards, or ``*`` alone) and a range on truenorth.ingested_at."""

    def __init__(self, events=(), down=False):
        self.events = list(events)
        self.down = down
        self.indices: set[str] = set()

    def _matches(self, event, clause) -> bool:
        if "query_string" in clause:
            q = clause["query_string"]["query"].strip()
            if q == "*":
                return True
            terms = [(f, inner if raw.startswith('"') else raw) for f, raw, inner in _TERM.findall(q)]
            return bool(terms) and all(fnmatch.fnmatch(str(_get(event, f)), pat) for f, pat in terms)
        if "range" in clause:
            [(field, bounds)] = clause["range"].items()
            value = _get(event, field)
            return value is not None and bounds["gte"] <= value <= bounds["lte"]
        raise AssertionError(clause)

    async def match(self, index, query, size=0):
        if self.down:
            raise SearchBackendError("connection refused")
        self.indices.add(index)
        hits = [i for i, e in enumerate(self.events) if all(self._matches(e, c) for c in query["bool"]["filter"])]
        return SearchMatch(total=len(hits), ids=[str(i) for i in hits[:size]])

    async def ingest(self, index, events):
        return 0

    async def search(self, index, query, size=50):
        return {}

    async def health_check(self):
        return True


def _at(minutes: float) -> str:
    return (STARTED + timedelta(minutes=minutes)).isoformat()


def beacon(minutes=5):
    return {"event_type": "http", "url.domain": "cdn.northwind-update.example", "truenorth": {"ingested_at": _at(minutes)}}


def noise(minutes=5, n=1):
    return [{"event_type": "http", "url.domain": f"site{i}.example", "truenorth": {"ingested_at": _at(minutes)}}
            for i in range(n)]


@contextmanager
def acting_as(role: UserRole, tenant: str = DEV_TENANT):
    who = CurrentUser(id=str(uuid.uuid4()), email=f"{role.value}@example.test", display_name=f"Test {role.value}",
                      role=role, tenant_id=tenant, keycloak_id="kc")
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)



