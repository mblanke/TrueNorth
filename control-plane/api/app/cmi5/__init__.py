"""cmi5: TrueNorth's AU runtime support and its own LMS side for released courses (docs/cmi5.md).

Importing this package registers its tables on ``Base.metadata``.
"""

from .models import Cmi5Registration, Cmi5Session

__all__ = ["Cmi5Registration", "Cmi5Session"]
