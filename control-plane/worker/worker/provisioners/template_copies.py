"""Host-local copies of golden templates (ADR 0012 decision 6): names and pruning rules.

The work vCenter has no shared storage: each ESXi host has only its own local VMFS
datastore, so cloning a template that sits on another host copies its disks over the
network. After each build, ``infra/vsphere/packer/build.sh replicate`` clones the template
to every range host as ``<id>@<host>-<yyyymmdd>`` (``srv2022@esx03-20261019``). The vSphere
provisioner clones the newest copy that resides on the VM's placed host and datastore,
else the plain ``<id>`` as before.

No vendor SDK here: the provisioner reads the inventory and calls these pure functions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_COPY = re.compile(r"^(?P<base>[^@]+)@(?P<host>[A-Za-z0-9_-]+)-(?P<date>\d{8})$")


@dataclass(frozen=True)
class TemplateCopy:
    name: str
    base: str
    host: str
    date: str  # yyyymmdd: compares in date order as a string


def host_short_name(host_name: str) -> str:
    """First label of an ESXi host name: esx03.truenorth.lab -> esx03 (an IP stays whole)."""
    name = str(host_name or "").strip()
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", name):
        return name
    return name.split(".", 1)[0].lower()


def copy_name(base: str, host: str, date: str) -> str:
    return f"{base}@{host_short_name(host)}-{date}"


def parse(name: str) -> TemplateCopy | None:
    """The parts of a ``<base>@<host>-<yyyymmdd>`` name, or None for any other name."""
    m = _COPY.match(str(name or ""))
    if not m:
        return None
    return TemplateCopy(name=str(name), base=m["base"], host=m["host"].lower(), date=m["date"])


def is_copy_of(name: str, base: str) -> bool:
    got = parse(name)
    return got is not None and got.base == base


def newest_on_host(names: Iterable[str], base: str, host_name: str) -> str | None:
    """The newest copy of ``base`` named for ``host_name`` among ``names``, or None.

    The caller passes only the names it has checked reside on the host's datastore."""
    short = host_short_name(host_name)
    found = [c for n in names if (c := parse(n)) and c.base == base and c.host == short]
    return max(found, key=lambda c: c.date).name if found else None


def stale(names: Iterable[str], in_use: set[str]) -> tuple[list[str], list[str]]:
    """(deletable, kept-in-use) copies among ``names``.

    Per base template and host, every copy older than the newest is stale; a stale copy
    that a recorded range VM was cloned from (``in_use``) is kept. The newest is never
    stale, so a build in flight (which resolves the newest) is not raced. Plain names and
    anything that does not parse as a copy are never touched.
    """
    groups: dict[tuple[str, str], list[TemplateCopy]] = {}
    for n in names:
        if (c := parse(n)) is not None:
            groups.setdefault((c.base, c.host), []).append(c)
    deletable: list[str] = []
    kept: list[str] = []
    for copies in groups.values():
        newest = max(c.date for c in copies)
        for c in copies:
            if c.date == newest:
                continue
            (kept if c.name in in_use else deletable).append(c.name)
    return sorted(deletable), sorted(kept)


def names_in_use(outputs: Iterable[dict]) -> set[str]:
    """Every template name a range VM records it was cloned from (``vms[].template``)."""
    used: set[str] = set()
    for out in outputs:
        for vm in (out or {}).get("vms") or []:
            if isinstance(vm, dict) and vm.get("template"):
                used.add(str(vm["template"]))
    return used
