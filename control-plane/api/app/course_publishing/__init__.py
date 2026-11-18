# Author: Marc-Andre Blanke
# Created: 2026-10-05T18:02:59Z
"""Course publications: accepted releases delivered to Moodle (stage, verify, activate).

Importing this package registers its table on ``Base.metadata``.
"""

from .models import CoursePublication

__all__ = ["CoursePublication"]
