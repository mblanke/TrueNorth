"""The test suite never reaches a real broker or Redis.

Until 2026-10-05 it published real Celery messages (provision_range, run_scenario_v2,
auto_assess_competency, ...) into whatever Redis listened on localhost:6379, db 15: on a
developer's Mac, the dev stack's. tests/conftest.py now points REDIS_URL at an address that
refuses, and records every send instead (fixture ``no_real_broker``).
"""

from __future__ import annotations

import os

import pytest


def test_the_api_dispatch_is_recorded_not_sent(no_real_broker):
    from app import celery_client

    task_id = celery_client.dispatch("provision_range", "00000000-0000-0000-0000-000000000001")
    assert task_id == "test-task-1"
    assert no_real_broker.sent == [("worker.tasks.provision_range", ["00000000-0000-0000-0000-000000000001"])]


def test_the_brokers_the_suite_could_reach_are_unreachable():
    from app import celery_client

    assert os.environ["REDIS_URL"].startswith("redis://127.0.0.1:1/")
    assert celery_client.celery_app.conf.broker_url.startswith("redis://127.0.0.1:1/")
    with pytest.raises(Exception):  # noqa: B017 — any connection error: nothing listens there
        conn = celery_client.celery_app.connection_for_write()
        conn.ensure_connection(max_retries=0, timeout=1)
