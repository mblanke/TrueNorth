# Author: Marc-Andre Blanke
# Created: 2026-10-06T21:11:59Z
"""Detection objectives: validator names, and credit for what the Student detected (ADR 0005).

Importing this package registers its table on ``Base.metadata``.
"""

from .models import DetectionSubmission

__all__ = ["DetectionSubmission"]
