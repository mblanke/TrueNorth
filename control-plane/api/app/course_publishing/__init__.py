"""Course publications: accepted releases delivered to Moodle (stage, verify, activate).

Importing this package registers its table on ``Base.metadata``.
"""

from .models import CoursePublication

__all__ = ["CoursePublication"]
