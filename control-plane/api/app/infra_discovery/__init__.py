"""Scheduled rediscovery of hypervisor connections (the Infrastructure page's figures).

Until this existed the stored inventory changed only when someone pressed Discover. Now an
API-side loop (``runner``) rediscovers every active connection every
``INFRA_DISCOVERY_SECONDS`` (default 300; 0 turns it off), through the same backend call
the Discover endpoint makes (``service.discover_connection``, ADR 0001). It runs in the
API, as the Moodle results pull and the lab sweep do, because the discovery backends and
the sealed connection passwords are the API's; the worker image holds neither.

Importing this package registers its table on ``Base.metadata``.
"""

from .models import HypervisorDiscoveryState

__all__ = ["HypervisorDiscoveryState"]
