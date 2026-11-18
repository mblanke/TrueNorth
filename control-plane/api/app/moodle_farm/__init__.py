# Author: Marc-Andre Blanke
# Created: 2026-10-10T13:52:50Z
"""TrueNorth's own Moodle farm nodes (app/moodle_farm/service.py).

Importing this package registers its table on ``Base.metadata``.
"""

from .models import ManagedMoodleNode

__all__ = ["ManagedMoodleNode"]
