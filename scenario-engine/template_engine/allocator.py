"""TrueNorth Range - Resource allocator.

Allocates VLANs, IP blocks, and VMIDs across ranges to prevent collisions.
Uses Redis for distributed locking in production, or an in-memory store
for local development and testing.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# VLAN allocation boundaries
_VLAN_MIN = 100
_VLAN_MAX = 4000

# VMID allocation start
_VMID_BASE = 100_000


@dataclass
class _RangeAllocation:
    """Bookkeeping for a single range's allocated resources."""
    vlans: list[int] = field(default_factory=list)
    ip_blocks: dict[int, str] = field(default_factory=dict)
    vmids: list[int] = field(default_factory=list)


class ResourceAllocator:
    """Allocate VLANs, IPs, and VMIDs across ranges to prevent collisions.

    Parameters
    ----------
    redis_url:
        If provided, use Redis for distributed locking and persistence.
        If ``None``, fall back to an in-memory store (dev / single-node).
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = redis_url
        self._redis: Any | None = None
        self._lock = threading.Lock()

        # In-memory state (used when Redis is unavailable)
        self._allocations: dict[str, _RangeAllocation] = {}
        self._used_vlans: set[int] = set()
        self._used_vmids: set[int] = set()
        self._next_vlan: int = _VLAN_MIN
        self._next_vmid: int = _VMID_BASE

        if redis_url:
            try:
                import redis as _redis_mod
                self._redis = _redis_mod.Redis.from_url(redis_url, decode_responses=True)
                self._redis.ping()
                logger.info("ResourceAllocator connected to Redis at %s", redis_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis unavailable (%s) — falling back to in-memory allocator", exc)
                self._redis = None

    # ------------------------------------------------------------------
    # VLAN allocation
    # ------------------------------------------------------------------

    def allocate_vlan_range(self, range_id: str, num_segments: int) -> list[int]:
        """Allocate a contiguous block of VLANs (within 100-4000)."""
        if self._redis:
            return self._redis_allocate_vlans(range_id, num_segments)
        return self._mem_allocate_vlans(range_id, num_segments)

    def _mem_allocate_vlans(self, range_id: str, count: int) -> list[int]:
        with self._lock:
            alloc = self._allocations.setdefault(range_id, _RangeAllocation())
            start = self._next_vlan
            allocated: list[int] = []
            candidate = start
            while len(allocated) < count and candidate <= _VLAN_MAX:
                if candidate not in self._used_vlans:
                    allocated.append(candidate)
                    self._used_vlans.add(candidate)
                candidate += 1
            if len(allocated) < count:
                raise RuntimeError(f"Cannot allocate {count} VLANs — only {len(allocated)} available")
            self._next_vlan = candidate
            alloc.vlans.extend(allocated)
            logger.info("Allocated VLANs %s for range %s", allocated, range_id)
            return allocated

    def _redis_allocate_vlans(self, range_id: str, count: int) -> list[int]:
        assert self._redis is not None
        key = "tn:vlans:counter"
        alloc_key = f"tn:range:{range_id}:vlans"
        allocated: list[int] = []
        for _ in range(count):
            vlan = int(self._redis.incr(key))
            vlan_id = _VLAN_MIN + vlan - 1
            if vlan_id > _VLAN_MAX:
                raise RuntimeError("VLAN pool exhausted")
            allocated.append(vlan_id)
            self._redis.rpush(alloc_key, str(vlan_id))
        return allocated

    # ------------------------------------------------------------------
    # IP block allocation
    # ------------------------------------------------------------------

    def allocate_ip_block(self, range_id: str, segment_index: int) -> str:
        """Allocate a /24 IP block for a range segment.

        Uses the pattern ``10.<range_octet>.<segment_index>.0/24`` where
        ``range_octet`` is derived from a global counter.
        """
        if self._redis:
            return self._redis_allocate_ip(range_id, segment_index)
        return self._mem_allocate_ip(range_id, segment_index)

    def _mem_allocate_ip(self, range_id: str, segment_index: int) -> str:
        with self._lock:
            alloc = self._allocations.setdefault(range_id, _RangeAllocation())
            if segment_index in alloc.ip_blocks:
                return alloc.ip_blocks[segment_index]
            # Derive second octet from number of unique ranges
            octet2 = len(self._allocations)
            cidr = f"10.{octet2}.{segment_index}.0/24"
            alloc.ip_blocks[segment_index] = cidr
            logger.info("Allocated IP block %s for range %s segment %d", cidr, range_id, segment_index)
            return cidr

    def _redis_allocate_ip(self, range_id: str, segment_index: int) -> str:
        assert self._redis is not None
        existing = self._redis.hget(f"tn:range:{range_id}:ips", str(segment_index))
        if existing:
            return existing
        octet2 = int(self._redis.incr("tn:ip:range_counter"))
        cidr = f"10.{octet2}.{segment_index}.0/24"
        self._redis.hset(f"tn:range:{range_id}:ips", str(segment_index), cidr)
        return cidr

    # ------------------------------------------------------------------
    # VMID allocation
    # ------------------------------------------------------------------

    def allocate_vmids(self, range_id: str, count: int) -> list[int]:
        """Allocate *count* unique VMIDs starting at 100000."""
        if self._redis:
            return self._redis_allocate_vmids(range_id, count)
        return self._mem_allocate_vmids(range_id, count)

    def _mem_allocate_vmids(self, range_id: str, count: int) -> list[int]:
        with self._lock:
            alloc = self._allocations.setdefault(range_id, _RangeAllocation())
            allocated: list[int] = []
            for _ in range(count):
                vmid = self._next_vmid
                self._next_vmid += 1
                self._used_vmids.add(vmid)
                allocated.append(vmid)
            alloc.vmids.extend(allocated)
            logger.info("Allocated VMIDs %s for range %s", allocated, range_id)
            return allocated

    def _redis_allocate_vmids(self, range_id: str, count: int) -> list[int]:
        assert self._redis is not None
        alloc_key = f"tn:range:{range_id}:vmids"
        allocated: list[int] = []
        for _ in range(count):
            vmid = _VMID_BASE + int(self._redis.incr("tn:vmids:counter")) - 1
            allocated.append(vmid)
            self._redis.rpush(alloc_key, str(vmid))
        return allocated

    # ------------------------------------------------------------------
    # Release / query
    # ------------------------------------------------------------------

    def release(self, range_id: str) -> None:
        """Release all allocations for a range."""
        if self._redis:
            self._redis_release(range_id)
        else:
            self._mem_release(range_id)

    def _mem_release(self, range_id: str) -> None:
        with self._lock:
            alloc = self._allocations.pop(range_id, None)
            if alloc:
                self._used_vlans -= set(alloc.vlans)
                self._used_vmids -= set(alloc.vmids)
                logger.info("Released allocations for range %s", range_id)

    def _redis_release(self, range_id: str) -> None:
        assert self._redis is not None
        pipe = self._redis.pipeline()
        pipe.delete(f"tn:range:{range_id}:vlans")
        pipe.delete(f"tn:range:{range_id}:ips")
        pipe.delete(f"tn:range:{range_id}:vmids")
        pipe.execute()
        logger.info("Released Redis allocations for range %s", range_id)

    def get_allocations(self, range_id: str) -> dict[str, Any]:
        """Get current allocations for a range."""
        if self._redis:
            return self._redis_get_allocations(range_id)
        return self._mem_get_allocations(range_id)

    def _mem_get_allocations(self, range_id: str) -> dict[str, Any]:
        with self._lock:
            alloc = self._allocations.get(range_id)
            if not alloc:
                return {"range_id": range_id, "vlans": [], "ip_blocks": {}, "vmids": []}
            return {
                "range_id": range_id,
                "vlans": list(alloc.vlans),
                "ip_blocks": dict(alloc.ip_blocks),
                "vmids": list(alloc.vmids),
            }

    def _redis_get_allocations(self, range_id: str) -> dict[str, Any]:
        assert self._redis is not None
        vlans = [int(v) for v in self._redis.lrange(f"tn:range:{range_id}:vlans", 0, -1)]
        ip_blocks = self._redis.hgetall(f"tn:range:{range_id}:ips")
        vmids = [int(v) for v in self._redis.lrange(f"tn:range:{range_id}:vmids", 0, -1)]
        return {
            "range_id": range_id,
            "vlans": vlans,
            "ip_blocks": ip_blocks,
            "vmids": vmids,
        }