"""TrueNorth Range - explicit NIC MAC addresses for vSphere range VMs.

Why explicit (manual) MACs, not the ones vCenter generates
-----------------------------------------------------------
Observed 2026-10-20 on the work vCenter: two VMs of one range cloned side by side on
esx02 were both given 00:50:56:ba:2b:47. vCenter later regenerated one of them, after
the provisioner had already written that VM's guest network config (cloud-init netplan
``match: macaddress``) keyed to the first MAC, so the VM never got its static IP.

Reading the MAC back after the clone does not fix that: vCenter may still change a
*generated* MAC afterwards (its duplicate detection runs late, e.g. at power-on), so the
value read back is not final, whatever the order. A *manual* MAC is never changed by
vCenter. So every range NIC gets one, chosen before anything is cloned:

* from a TrueNorth-owned slice (``VSPHERE_MAC_POOL``) of VMware's manual range
  00:50:56:00:00:00-00:50:56:3F:FF:FF, which vCenter never generates from;
* reserved per NIC on the ``network_reservations`` table (kind ``mac``, worker/range_alloc.py),
  whose (domain, kind, value) uniqueness and per-domain advisory lock make it unique
  across concurrent builds, hosts, ranges and tenants, and persisted for the range's
  life (a retry gets the same MACs);
* checked against the VMs already on the networks the range attaches to before the
  first clone (a VM someone gave a manual MAC from the slice by hand), and read back
  from every clone before its guest config is written.

The guest config (Linux netplan, VyOS/pfSense guestinfo, Windows Sysprep NIC map) is then
built from those MACs. No vendor SDK here: pure functions the provisioner calls.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

# VMware's range for manually assigned MACs (vSphere docs, "MAC address assignment").
VMWARE_MANUAL_FIRST = 0x005056000000
VMWARE_MANUAL_LAST = 0x0050563FFFFF
# TrueNorth's slice of it: 262,144 addresses, the top quarter, away from the low values
# people pick by hand. Overridable with VSPHERE_MAC_POOL ("first-last").
DEFAULT_POOL = "00:50:56:3c:00:00-00:50:56:3f:ff:ff"


def to_int(mac: str) -> int:
    """00:50:56:3c:00:05 (any case, ':' or '-') -> int; ValueError if it is not a MAC."""
    parts = str(mac).strip().replace("-", ":").split(":")
    if len(parts) != 6 or not all(len(p) == 2 for p in parts):
        raise ValueError(f"not a MAC address: {mac!r}")
    return int("".join(parts), 16)


def to_str(value: int) -> str:
    raw = f"{value:012x}"
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2))


def normalize(mac: str | None) -> str:
    """Lower-case colon form, '' for nothing."""
    return to_str(to_int(mac)) if mac else ""


class MacPool(Sequence):
    """The pool as a lazy sequence of MAC strings (no 262k-element list per build).

    range_alloc.reserve_values hands out the lowest free values in sequence order."""

    def __init__(self, first: int, last: int) -> None:
        self.first, self.last = first, last

    def __len__(self) -> int:
        return self.last - self.first + 1

    def __getitem__(self, index):  # type: ignore[override]
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return to_str(self.first + index)

    def __iter__(self) -> Iterator[str]:
        return (to_str(v) for v in range(self.first, self.last + 1))

    def __contains__(self, mac: object) -> bool:
        try:
            return self.first <= to_int(str(mac)) <= self.last
        except ValueError:
            return False


def parse_pool(text: str) -> MacPool:
    """``first-last`` (inclusive) inside VMware's manual range; ValueError otherwise."""
    raw = (text or DEFAULT_POOL).strip()
    head, sep, tail = raw.partition("-") if raw.count("-") == 1 else (raw[:17], raw[17:18], raw[18:])
    if sep != "-":
        raise ValueError(f"VSPHERE_MAC_POOL {text!r}: expected first-last, e.g. {DEFAULT_POOL}")
    first, last = to_int(head), to_int(tail)
    if first > last:
        raise ValueError(f"VSPHERE_MAC_POOL {text!r}: first is above last")
    if first < VMWARE_MANUAL_FIRST or last > VMWARE_MANUAL_LAST:
        raise ValueError(f"VSPHERE_MAC_POOL {text!r} is outside VMware's manual range "
                         "00:50:56:00:00:00-00:50:56:3f:ff:ff (vCenter refuses or regenerates other values)")
    return MacPool(first, last)


def holder(vm_name: str, nic_index: int) -> str:
    """The reservation holder of one NIC: the VM's range-prefixed name and NIC index."""
    return f"{vm_name}#nic{nic_index}"


def holders(vm_defs: list[dict]) -> list[str]:
    return [holder(v["name"], i) for v in vm_defs for i in range(len(v["nics"]))]


def assign(vm_defs: list[dict], reserved: dict) -> None:
    """Put each NIC's reserved MAC on it (``nic["mac"]``). RuntimeError when one has no
    reservation or two NICs got the same value: never picked here (the table decides)."""
    missing, seen = [], {}
    for v in vm_defs:
        for i, nic in enumerate(v["nics"]):
            mac = normalize(reserved.get(holder(v["name"], i)))
            if not mac:
                missing.append(holder(v["name"], i))
                continue
            if mac in seen:
                raise RuntimeError(f"MAC {mac} is reserved for both {seen[mac]} and {holder(v['name'], i)}")
            seen[mac] = holder(v["name"], i)
            nic["mac"] = mac
    if missing:
        raise RuntimeError(f"NICs {missing} have no MAC reservation (worker: allocation_needs, network_reservations)")


def planned(vm_def: dict) -> list[str]:
    """The VM's NIC MACs in NIC order ('' where none is planned)."""
    return [nic.get("mac", "") for nic in vm_def["nics"]]


def collisions(wanted: dict[str, str], in_use: dict[str, str]) -> list[str]:
    """Human-readable clashes: ``wanted`` {mac: holder} against ``in_use`` {mac: 'VM x on net'}."""
    return [f"MAC {mac} (for {who}) is already used by {in_use[mac]}"
            for mac, who in sorted(wanted.items()) if mac in in_use]


def mismatches(vm_def: dict, actual: list[str]) -> list[str]:
    """Where a built VM's NIC MACs (guest NIC order) differ from the planned ones."""
    want = planned(vm_def)
    got = [normalize(m) for m in actual]
    if len(got) != len(want):
        return [f"{len(got)} NICs, {len(want)} planned"]
    return [f"nic{i}: {g or 'none'} instead of {w}" for i, (w, g) in enumerate(zip(want, got, strict=True))
            if w and g != w]
