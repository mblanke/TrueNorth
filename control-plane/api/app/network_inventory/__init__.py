# Author: Marc-Andre Blanke
# Created: 2026-10-07T14:13:40Z
"""Network reservations: collision-free addresses and VLANs on networks shared by ranges.

Importing this package registers its table on ``Base.metadata``. The service
(``service.py``) reserves, syncs and releases; consumers import from here.
"""

from .models import KINDS, NetworkReservation
from .schemas import NetworkReservationOut
from .service import PoolExhaustedError, parse_ip_pool, release_range, reserve, sync, vlan_pool

__all__ = [
    "KINDS",
    "NetworkReservation",
    "NetworkReservationOut",
    "PoolExhaustedError",
    "parse_ip_pool",
    "release_range",
    "reserve",
    "sync",
    "vlan_pool",
]
