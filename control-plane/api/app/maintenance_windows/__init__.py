"""Maintenance windows (ADR 0013 §7, range certification S7).

Importing this package registers its table on ``Base.metadata``. Other sections use
``service`` (what a window refuses) and ``policy`` (what always bypasses one).
"""

from .models import MaintenanceWindow

__all__ = ["MaintenanceWindow"]
