# Author: Marc-Andre Blanke
# Created: 2026-11-28T02:58:11Z
"""Usage and cost (showback): what each Student's ranges cost to run.

Allocated size (vCPU, RAM, disk, VMs) times how long it was held, priced by a versioned
rate card when the report is read. S1 derives the intervals from records that already
exist (lab sessions, class ranges, ranges); nothing here writes to them.

Importing this package registers its table on ``Base.metadata``.
"""

from . import ledger  # noqa: F401  registers the flush hook that records range state changes
from .models import RangeStateEvent, RateCard

__all__ = ["RangeStateEvent", "RateCard"]
