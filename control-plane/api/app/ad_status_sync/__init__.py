"""Nightly AD status sync and the 90-day rule for local accounts (ADR 0014 decision 6; #290).

Importing this package registers its tables on ``Base.metadata``. ``directory`` reads the
directory's view of each account from Keycloak (GET only); ``service`` decides and applies;
``clock`` runs it nightly; ``last_seen`` records when TrueNorth last saw an account.
"""

from .models import AdStatusSyncResult, AdStatusSyncRun, UserLastSeen

__all__ = ["AdStatusSyncResult", "AdStatusSyncRun", "UserLastSeen"]
