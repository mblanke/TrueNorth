"""Range inventory checks: teardown by identified inventory, and drift (ADR 0013 §5,
range certification S5, issue #243).

Importing this package registers its table on ``Base.metadata``. ``models.py`` the
latest check per range and kind (written by the worker); ``service.py`` reading it and
the administrator's confirm-by-moid of unidentified items; ``router.py`` the endpoints.
"""

from .models import RangeInventoryCheck

__all__ = ["RangeInventoryCheck"]
