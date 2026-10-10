"""Moodle results: completions and quiz grades for published courses, pulled back into
TrueNorth's enrolments, module progress and quiz attempts.

Importing this package registers its tables on ``Base.metadata``.
"""

from .models import MoodleResultCursor, MoodleResultRecord

__all__ = ["MoodleResultCursor", "MoodleResultRecord"]
