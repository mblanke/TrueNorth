"""TrueNorth's own Moodle farm nodes (app/moodle_farm/service.py).

Importing this package registers its table on ``Base.metadata``.
"""

from .models import ManagedMoodleNode

__all__ = ["ManagedMoodleNode"]
