"""The worker decides which ranges a maintenance window covers exactly as the API does.

The API refuses work inside a window (app/maintenance_windows/policy.py); the worker
re-checks at a range task's entry (worker/maintenance_windows.py) with its own copy of
``applies`` and ``template_tags``, because it may not import the API (ADR 0003). If the
two drift, a build the API would refuse runs on the worker, or the worker refuses one the
API let through.
"""

from __future__ import annotations

import inspect
import itertools
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))
sys.path.insert(0, str(ROOT / "control-plane/api"))

from app.maintenance_windows import policy  # noqa: E402
from worker import maintenance_windows as worker_policy  # noqa: E402

T1, T2 = uuid.uuid4(), uuid.uuid4()
R1, R2 = uuid.uuid4(), uuid.uuid4()

WINDOWS = [
    {"window_tenant": None, "scope": "all", "range_ids": [], "tags": []},
    {"window_tenant": T1, "scope": "all", "range_ids": [], "tags": []},
    {"window_tenant": str(T2), "scope": "all", "range_ids": [], "tags": []},
    {"window_tenant": T1, "scope": "ranges", "range_ids": [str(R1)], "tags": []},
    {"window_tenant": None, "scope": "ranges", "range_ids": [str(R2), str(R1)], "tags": []},
    {"window_tenant": T1, "scope": "tags", "range_ids": [], "tags": ["SOC", " blue "]},
    {"window_tenant": T1, "scope": "nonsense", "range_ids": [], "tags": []},
]
RANGES = [
    {"range_id": R1, "range_tenant": T1, "range_tags": {"soc"}},
    {"range_id": str(R2), "range_tenant": str(T1), "range_tags": set()},
    {"range_id": R1, "range_tenant": T2, "range_tags": {"blue", "red"}},
    {"range_id": None, "range_tenant": T1, "range_tags": set()},  # a lab not created yet
]


@pytest.mark.parametrize(("window", "rng"), list(itertools.product(WINDOWS, RANGES)))
def test_applies_agrees(window, rng):
    assert policy.applies(**window, **rng) == worker_policy.applies(**window, **rng)


@pytest.mark.parametrize(
    "yml",
    [
        None,
        "",
        "id: t\n",
        "tags: [SOC, blue]\n",
        "tags: soc\n",
        "tags: [1, ' x ', '', null]\n",
        "nodes:\n  - id: a\n    tags: [user]\n",
        "tags: [unclosed\n",
        "- just\n- a list\n",
        "base: &b [soc]\ntags: *b\n",  # aliases are refused by both: no tags
    ],
)
def test_template_tags_agree(yml):
    assert policy.template_tags(yml) == worker_policy.template_tags(yml)


def test_the_bodies_are_the_same_code():
    """Not only equal on the cases above: the same statements (docstrings aside)."""

    def body(fn):
        src = inspect.getsource(fn)
        lines = [ln for ln in src.split('"""', 2)[-1].splitlines() if ln.strip() and not ln.strip().startswith("#")]
        return [ln.split("  #")[0].rstrip() for ln in lines]

    assert body(policy.applies) == body(worker_policy.applies)


def test_the_worker_gates_the_actions_the_api_refuses():
    assert set(worker_policy.GATED) == set(policy.REFUSED_ACTIONS)


def test_bypasses_markers_and_statuses_agree():
    from app.range_ops.models import HELD, IN_FLIGHT

    assert worker_policy.BYPASS_KINDS == policy.BYPASS_KINDS
    assert worker_policy.HELD_MARKER == policy.HELD_MARKER
    assert (worker_policy.HELD, worker_policy.IN_FLIGHT) == (HELD, IN_FLIGHT)
    assert policy.HELD_MARKER in worker_policy.message({"ends_at": "2026-11-02T07:00:00", "reason": "x"}, "destroy")
