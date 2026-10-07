"""Range leases: which worker execution is acting on a range right now.

Importing this package registers its table on ``Base.metadata``. The worker takes and
gives back leases (control-plane/worker/worker/fencing.py); the API only reads them.
"""

from .models import RangeLease

__all__ = ["RangeLease"]
