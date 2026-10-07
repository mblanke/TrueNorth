"""Scenario runs: inject outcomes and scenario executions (docs/scenario-inject-execution.md).

Importing this package registers its tables on ``Base.metadata``.
"""

from .models import EXECUTION_STATES, INJECT_STATUSES, InjectRecord, ScenarioExecution

__all__ = ["EXECUTION_STATES", "INJECT_STATUSES", "InjectRecord", "ScenarioExecution"]
