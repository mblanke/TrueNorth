"""TrueNorth Range - the one seam between the exercise run loop and scenario injection.

``exercise_run.run_scenario`` and ``run_scenario_v2`` call ``dispatch_inject`` once per
timeline event at the point where injection belongs. Nothing is injected yet: the seam
returns ``{"dispatched": False, "reason": "not wired"}`` and the run loop ignores the
result, which is exactly what the commented-out ``_dispatch_inject(...)`` stub did.

Wiring real injection (injector registry, range context, result handling) happens here
and in the run loop's use of the result; the signature is the contract the run loop
depends on.
"""

from __future__ import annotations

NOT_WIRED = {"dispatched": False, "reason": "not wired"}


def dispatch_inject(exercise_id: str, action: str, params: dict) -> dict:
    """Deliver one timeline event's inject to the exercise's range. Not wired: a no-op."""
    return dict(NOT_WIRED)
