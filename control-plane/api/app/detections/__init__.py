"""Detection objectives: validator names, and credit for what the Student detected (ADR 0005).

Importing this package registers its table on ``Base.metadata``.
"""

from .models import DetectionSubmission

__all__ = ["DetectionSubmission"]
