"""Range Certification Record (ADR 0013 §6, issue #244): is this deployment safe, ready and
certified for Students?

Importing this package registers its two append-only tables on ``Base.metadata``.
``models.py`` the tables and their immutability; ``sections.py`` the sections and the
verdict; ``evidence.py`` evidence TrueNorth already records; ``service.py`` the only write
path (``append``) and the hook points other slices call; ``signing.py`` the signed JSON
export; ``pdf.py`` the PDF; ``router.py`` ``GET /ranges/{id}/certification`` and exports.
"""

from .models import RangeCertificationEntry, RangeCertificationRecord, RecordImmutableError, SigningKey

__all__ = ["RangeCertificationEntry", "RangeCertificationRecord", "RecordImmutableError", "SigningKey"]
