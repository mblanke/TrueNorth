"""The worker cannot credit a team-exercise objective or close an exercise (ADR 0020).

Instructors judge every objective (app/exercise_judging.py) and the API closes exercises
(app/exercise_completion.py). The worker used to mark every objective achieved at the end
of a mock run; this keeps that, or anything like it, from coming back.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKER = Path(__file__).resolve().parents[2] / "control-plane/worker/worker"


def test_db_ops_has_no_way_to_achieve_an_objective_or_complete_an_exercise():
    from worker import db_ops

    for name in ("achieve_objective", "complete_exercise", "refresh_exercise_score"):
        assert not hasattr(db_ops, name), name


def test_no_worker_module_writes_achieved_or_total_score():
    offenders = []
    for path in WORKER.rglob("*.py"):
        text = path.read_text()
        if re.search(r"""(\bachieved\s*=\s*(True|sa\.true\(\))|["']achieved["']\s*:\s*True|total_score\s*=\s*(?!0\b))""", text):
            offenders.append(str(path.relative_to(WORKER)))
    assert offenders == []
