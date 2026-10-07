"""Cluster capacity: what the hosts can carry and what is running on them
(docs/adr/0006-capacity-service.md).

The infrastructure side of the scheduler's capacity check. It reads hypervisor hosts,
ranges and templates, and knows nothing of bookings; the scheduler combines its
answers with its own bookings (``scheduler/capacity.py``). The dashboard gauge and
the booking check go through the same code, so they cannot disagree.
"""

from .service import ClusterSupply, RangeLoad, running_ranges, supply

__all__ = ["ClusterSupply", "RangeLoad", "running_ranges", "supply"]
