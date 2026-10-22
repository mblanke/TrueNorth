"""Host-local copies of golden templates (ADR 0012 decision 6, ADR 0013 §2): names,
build stamps and pruning rules.

The work vCenter has no shared storage: each ESXi host has only its own local VMFS
datastore, so cloning a template that sits on another host copies its disks over the
network. After each build, ``infra/vsphere/packer/build.sh replicate`` clones the template
to every range host as ``<id>@<host>-<yyyymmdd>`` (``srv2022@esx03-20261019``) and stamps
the copy's annotation with the image and build it is
(``tn-build: <catalogue id> <build id> sha256:<checksum>``).

Since ADR 0013 a range clones only the **promoted build's** copy on each host it uses: the
copy the build record names for that host, carrying that build's stamp. There is no
newest-copy selection and no plain-name fallback (worker/template_pins.py, vsphere_api).

No vendor SDK here: the provisioner reads the inventory and calls these pure functions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_COPY = re.compile(r"^(?P<base>[^@]+)@(?P<host>[A-Za-z0-9_-]+)-(?P<date>\d{8})$")
_STAMP = re.compile(r"^tn-build:[ \t]*(?P<image>\S+)[ \t]+(?P<build>\S+)[ \t]+sha256:(?P<sum>[0-9a-f]{64})[ \t]*$",
                    re.M)


@dataclass(frozen=True)
class TemplateCopy:
    name: str
    base: str
    host: str
    date: str  # yyyymmdd


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


def stamp(catalogue_id: str, build_id: str, checksum: str) -> str:
    """The annotation line naming the image and build a template or copy is (build.sh replicate).

    It catches mix-ups (another build, another image, a copy left under a reused name), not
    tampering: anyone who may edit the annotation can write it (docs/security.md)."""
    return f"tn-build: {catalogue_id} {build_id} sha256:{checksum}"


def stamp_of(annotation: str | None) -> tuple[str, str, str] | None:
    """(catalogue id, build id, checksum) of the ``tn-build:`` line in an annotation, or None.

    More than one stamp is no stamp: the copy cannot be shown to be one build."""
    found = _STAMP.findall(str(annotation or ""))
    return (found[0][0], found[0][1], found[0][2]) if len(found) == 1 else None


def stamp_problem(annotation: str | None, catalogue_id: str, build_id: str, checksum: str) -> str | None:
    """Why a template's annotation does not prove it is ``catalogue_id``'s build ``build_id``
    with ``checksum``, or None."""
    got = stamp_of(annotation)
    want = (catalogue_id, build_id, checksum)
    if got is None:
        return f"carries no single build stamp ('{stamp(catalogue_id, build_id, '<sha256>')}' in its annotation)"
    if got != want:
        return (f"is stamped {got[0]} build {got[1]} sha256:{got[2][:12]}..., not {catalogue_id} build {build_id} "
                f"sha256:{checksum[:12]}...")
    return None


def newest_local(names, base: str, host_name: str) -> str | None:
    """TRANSITION ONLY (``TN_TEMPLATE_ALLOW_UNPROMOTED``): the newest ``<base>@<host>-<date>``
    among ``names`` for ``host_name``, unverified, so an unpromoted image still clones from
    the placed host's own disk rather than across the network. A promoted build never uses
    this: it clones exactly the copy its record names (ADR 0013 §2)."""
    short = host_short_name(host_name)
    found = [c for n in names if (c := parse(n)) and c.base == base and c.host == short]
    return max(found, key=lambda c: (c.date, c.name)).name if found else None


@dataclass(frozen=True)
class PrunePolicy:
    """What pruning must keep (worker/template_tasks.py builds it from the build records).

    ``managed``: templates (copy bases) of images that have a promoted build; only their
    copies are ever candidates. ``protected``: copy names to keep: the promoted builds'
    copies, those of builds registered since the promotion (awaiting their own), and every
    name an undestroyed range records. ``builds``: (catalogue id, build id, checksum) of
    those same builds; a copy stamped as one of them is kept whatever its name."""

    protected: frozenset[str] = frozenset()
    managed: frozenset[str] = frozenset()
    builds: frozenset[tuple[str, str, str]] = frozenset()


def prunable(annotations: dict[str, str | None], policy: PrunePolicy) -> tuple[list[str], list[str]]:
    """(deletable, kept) among copies ``{name: annotation}``.

    Only copies of a ``managed`` template are candidates; one that is ``protected`` by name
    or stamped as a protected build is kept and the rest are deletable. Plain names, names
    that do not parse as copies and copies of images with no promoted build are never
    touched. There is no date ordering: a newer copy that no build record names is as
    deletable as an older one, and an older one a build names is kept.
    """
    deletable: set[str] = set()
    kept: set[str] = set()
    for name, annotation in annotations.items():
        c = parse(name)
        if c is None or c.base not in policy.managed:
            continue
        keep = c.name in policy.protected or stamp_of(annotation) in policy.builds
        (kept if keep else deletable).add(c.name)
    return sorted(deletable), sorted(kept)


def names_in_use(outputs: Iterable[dict]) -> set[str]:
    """Every template name a range VM records it was cloned from (``vms[].template``)."""
    used: set[str] = set()
    for out in outputs:
        for vm in (out or {}).get("vms") or []:
            if isinstance(vm, dict) and vm.get("template"):
                used.add(str(vm["template"]))
    return used
