"""Fixtures for worker task tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def lease_always_free(monkeypatch):
    """For tests that stub the worker's database: the range's lease (worker/fencing.py)
    is always free and taking or giving it back touches nothing. Tests of the lease
    itself use a real database (test_range_task_fencing.py)."""
    from worker import fencing

    monkeypatch.setattr(fencing, "_claim_lease", lambda db, range_id, holder: True)
    monkeypatch.setattr(fencing, "_release_lease", lambda db, range_id, holder: None)
    monkeypatch.setattr(fencing, "_extend_lease", lambda db, range_id, holder: None)
