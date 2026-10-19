"""Range credentials: the accounts a range template declares, stored sealed per range.

Importing this package registers ``range_credentials`` on ``Base.metadata``. ADR 0012
(decisions 12-14): ``accounts.py`` reads and validates a template's ``accounts:``,
``service.py`` keeps a range's rows in step and decides who may read them, ``wiki_doc.py``
writes the staff-only range page, ``router.py`` is ``GET /ranges/{id}/credentials``.
"""

from .models import RangeCredential

__all__ = ["RangeCredential"]
