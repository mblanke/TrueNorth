"""Range operations: each provision, destroy, stop or start, durably recorded.

Importing this package registers its table on ``Base.metadata``.
"""

from .models import IN_FLIGHT, RangeOperation

__all__ = ["IN_FLIGHT", "RangeOperation"]
