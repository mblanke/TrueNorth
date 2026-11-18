# Author: Marc-Andre Blanke
# Created: 2026-10-05T18:29:45Z
"""Lab sessions: each student's own small range for a range activity.

Importing this package registers its tables on ``Base.metadata``.
"""

from .models import LabNetworkLease, LabSession

__all__ = ["LabNetworkLease", "LabSession"]
