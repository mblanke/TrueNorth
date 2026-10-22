"""Range credentials: the accounts a range's machines get, stored sealed per range.

Importing this package registers ``range_credentials`` and ``range_credential_snapshots`` on
``Base.metadata``. ADR 0012 (decisions 12-14) as amended by ADR 0013 §3: ``accounts.py``
reads and validates a template's ``accounts:`` and adds the implicit staff logins,
``service.py`` keeps a range's rows in step, rotates them and decides who may read them,
``wiki_doc.py`` writes the staff-only range page (no credentials in it), ``router.py`` is
``/ranges/{id}/credentials`` (metadata), ``.../{cred_id}/reveal`` and ``.../{cred_id}/rotate``.
"""

from .models import RangeCredential, RangeCredentialSnapshot

__all__ = ["RangeCredential", "RangeCredentialSnapshot"]
