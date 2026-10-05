"""Course releases: immutable accepted versions of a course (ARC² release tarballs).

Importing this package registers its tables on ``Base.metadata``.
"""

from .models import CourseRelease, CourseReleaseBlob, EnrollmentReleasePin

__all__ = ["CourseRelease", "CourseReleaseBlob", "EnrollmentReleasePin"]
