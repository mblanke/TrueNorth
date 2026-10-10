"""The Greyspace corpus manifest: one format for every corpus tier (ADR 0007).

A corpus is a directory, wherever it lives (generated in CI, ``~/greyspace-corpus`` on a
Mac, a NetApp volume in the lab)::

    manifest.json        this format, "greyspace-corpus/1"
    checksums.sha256     ``sha256sum`` lines for every file, paths relative to the corpus root
    sites/<fqdn>/...     static site trees, served read-only by the web farm
    warc/                optional: the WARC files a tier was extracted from (provenance)

The manifest says which "ISPs" own which public prefixes, which site answers on which
address, which threat-actor domains exist, and where every byte came from (``source``,
``licence``). DNS zones, the web farm's virtual hosts and the routers' announcements are
all generated from it (``config.py``); nothing about a site is configured by hand.

Pure standard library: the runtime builder (``greyspace/scripts``) imports this module
without the API's database or settings.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

FORMAT = "greyspace-corpus/1"


@dataclass(frozen=True)
class Tier:
    name: str
    title: str
    cap_bytes: int | None  # hard cap on the corpus size; None = no cap (full corpus)
    location: str
    builder: str
    description: str


# One layout and one manifest format across all tiers; only size and location differ.
TIERS: dict[str, Tier] = {
    "t0": Tier(
        "t0",
        "CI fixture",
        100 * 10**6,
        "generated per build",
        "greyspace/scripts/build_t0.py",
        "About 20 synthetic CC0 sites, one tiny video and two threat-actor domains. Generated, never committed.",
    ),
    "t1": Tier(
        "t1",
        "Mac test sample",
        5 * 10**9,
        "~/greyspace-corpus",
        "greyspace/scripts/build-sample.sh --tier mac",
        "Shallow mirrors of permissive-licence sites (Wikimedia and similar). Hard cap 5 GB.",
    ),
    "t2": Tier(
        "t2",
        "Lab corpus",
        50 * 10**9,
        "NetApp volume greyspace-corpus-lab (NFS, read-only)",
        "docs/greyspace-corpus.md (plan)",
        "About 50 GB of curated site packs for lab exercises.",
    ),
    "full": Tier(
        "full",
        "Full corpus",
        None,
        "NetApp volume greyspace-corpus (NFS, read-only, 10 TB+)",
        "docs/greyspace-corpus.md (plan)",
        "The full rehosted internet: site packs, video and search. Planned, not built.",
    ),
}

THREAT_ROLES = ("c2", "phish", "payload", "redirector")

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD = re.compile(r"^[a-z]{2,63}$")


class ManifestError(ValueError):
    """The manifest is not valid; ``errors`` lists every problem found, not just the first."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Isp:
    name: str
    asn: int
    prefix: ipaddress.IPv4Network


@dataclass(frozen=True)
class Site:
    fqdn: str
    ip: ipaddress.IPv4Address
    category: str
    path: str
    aliases: tuple[str, ...] = ()
    files: int = 0
    bytes: int = 0
    licence: str = ""
    source: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        return (self.fqdn, *self.aliases)


@dataclass(frozen=True)
class ThreatDomain:
    fqdn: str
    ip: ipaddress.IPv4Address
    role: str
    actor: str


@dataclass(frozen=True)
class Video:
    id: str
    site: str
    path: str
    bytes: int
    licence: str = ""
    source: str = ""


@dataclass(frozen=True)
class Manifest:
    tier: str
    version: str
    isps: tuple[Isp, ...]
    sites: tuple[Site, ...]
    threat_domains: tuple[ThreatDomain, ...] = ()
    videos: tuple[Video, ...] = ()
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(s.bytes for s in self.sites)

    @property
    def total_files(self) -> int:
        return sum(s.files for s in self.sites)

    @property
    def categories(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.sites:
            out[s.category] = out.get(s.category, 0) + 1
        return dict(sorted(out.items()))

    def isp_for(self, ip: ipaddress.IPv4Address) -> Isp | None:
        return next((i for i in self.isps if ip in i.prefix), None)


def valid_fqdn(name: str) -> bool:
    """A lower-case DNS name of at least two labels whose last label is alphabetic."""
    if not isinstance(name, str) or not name or len(name) > 253 or name != name.lower():
        return False
    labels = name.split(".")
    return len(labels) >= 2 and all(_LABEL.match(lbl) for lbl in labels) and bool(_TLD.match(labels[-1]))


def zone_of(fqdn: str) -> str:
    """The registrable domain a name is served from: its last two labels."""
    return ".".join(fqdn.split(".")[-2:])


def _safe_relpath(path: Any) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _ip(value: Any, where: str, errors: list[str]) -> ipaddress.IPv4Address | None:
    try:
        return ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, ValueError, TypeError):
        errors.append(f"{where}: {value!r} is not an IPv4 address")
        return None


def _int(value: Any, where: str, errors: list[str], *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        errors.append(f"{where}: must be an integer >= {minimum}")
        return 0
    return value


def parse_manifest(doc: Any) -> Manifest:
    """Validate a decoded ``manifest.json`` and return it typed. Raises ManifestError."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        raise ManifestError(["manifest: must be a JSON object"])
    if doc.get("format") != FORMAT:
        errors.append(f"format: must be {FORMAT!r}, got {doc.get('format')!r}")
    tier = doc.get("tier")
    if tier not in TIERS:
        errors.append(f"tier: must be one of {sorted(TIERS)}, got {tier!r}")
    version = doc.get("version")
    if not isinstance(version, str) or not version.strip():
        errors.append("version: required")

    isps: list[Isp] = []
    raw_isps = doc.get("isps")
    if not isinstance(raw_isps, list) or not raw_isps:
        errors.append("isps: at least one ISP is required")
        raw_isps = []
    seen_asn: set[int] = set()
    for n, raw in enumerate(raw_isps):
        where = f"isps[{n}]"
        if not isinstance(raw, dict):
            errors.append(f"{where}: must be an object")
            continue
        try:
            prefix = ipaddress.IPv4Network(raw.get("prefix"), strict=True)
        except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError, TypeError):
            errors.append(f"{where}.prefix: {raw.get('prefix')!r} is not an IPv4 network")
            continue
        asn = _int(raw.get("asn"), f"{where}.asn", errors, minimum=1)
        if asn in seen_asn:
            errors.append(f"{where}.asn: {asn} is used twice")
        seen_asn.add(asn)
        for other in isps:
            if prefix.overlaps(other.prefix):
                errors.append(f"{where}.prefix: {prefix} overlaps {other.prefix}")
        isps.append(Isp(str(raw.get("name") or f"isp-{n}"), asn, prefix))

    names: set[str] = set()

    def _claim(name: str, where: str) -> None:
        if name in names:
            errors.append(f"{where}: {name} is declared twice")
        names.add(name)

    def _in_isp(ip: ipaddress.IPv4Address, where: str) -> None:
        if isps and not any(ip in i.prefix for i in isps):
            errors.append(f"{where}: {ip} is outside every ISP prefix")

    sites: list[Site] = []
    raw_sites = doc.get("sites")
    if not isinstance(raw_sites, list):
        errors.append("sites: must be a list")
        raw_sites = []
    for n, raw in enumerate(raw_sites):
        where = f"sites[{n}]"
        if not isinstance(raw, dict):
            errors.append(f"{where}: must be an object")
            continue
        fqdn = raw.get("fqdn")
        if not valid_fqdn(fqdn):
            errors.append(f"{where}.fqdn: {fqdn!r} is not a valid lower-case domain name")
            continue
        _claim(fqdn, f"{where}.fqdn")
        aliases = raw.get("aliases") or []
        if not isinstance(aliases, list):
            errors.append(f"{where}.aliases: must be a list")
            aliases = []
        for alias in aliases:
            if not valid_fqdn(alias):
                errors.append(f"{where}.aliases: {alias!r} is not a valid domain name")
            elif zone_of(alias) != zone_of(fqdn):
                errors.append(f"{where}.aliases: {alias} is not in {zone_of(fqdn)}")
            else:
                _claim(alias, f"{where}.aliases")
        ip = _ip(raw.get("ip"), f"{where}.ip", errors)
        if ip is not None:
            _in_isp(ip, f"{where}.ip")
        path = raw.get("path")
        if not _safe_relpath(path) or not str(path).startswith("sites/"):
            errors.append(f"{where}.path: {path!r} must be a relative path under sites/")
        category = raw.get("category")
        if not isinstance(category, str) or not category:
            errors.append(f"{where}.category: required")
        files = _int(raw.get("files", 0), f"{where}.files", errors)
        size = _int(raw.get("bytes", 0), f"{where}.bytes", errors)
        if ip is not None and isinstance(category, str) and category and _safe_relpath(path):
            sites.append(
                Site(
                    fqdn=fqdn,
                    ip=ip,
                    category=category,
                    path=path,
                    aliases=tuple(a for a in aliases if valid_fqdn(a)),
                    files=files,
                    bytes=size,
                    licence=str(raw.get("licence") or ""),
                    source=str(raw.get("source") or ""),
                )
            )

    threats: list[ThreatDomain] = []
    raw_actors = doc.get("threat_actors") or []
    if not isinstance(raw_actors, list):
        errors.append("threat_actors: must be a list")
        raw_actors = []
    for a, actor in enumerate(raw_actors):
        if not isinstance(actor, dict) or not isinstance(actor.get("domains"), list):
            errors.append(f"threat_actors[{a}]: must be an object with a domains list")
            continue
        for d, raw in enumerate(actor["domains"]):
            where = f"threat_actors[{a}].domains[{d}]"
            fqdn = raw.get("fqdn") if isinstance(raw, dict) else None
            if not valid_fqdn(fqdn):
                errors.append(f"{where}.fqdn: {fqdn!r} is not a valid lower-case domain name")
                continue
            _claim(fqdn, f"{where}.fqdn")
            role = raw.get("role")
            if role not in THREAT_ROLES:
                errors.append(f"{where}.role: must be one of {list(THREAT_ROLES)}")
            ip = _ip(raw.get("ip"), f"{where}.ip", errors)
            if ip is not None:
                _in_isp(ip, f"{where}.ip")
                threats.append(ThreatDomain(fqdn, ip, str(role), str(actor.get("name") or f"actor-{a}")))

    videos: list[Video] = []
    site_names = {s.fqdn for s in sites}
    for n, raw in enumerate(doc.get("videos") or []):
        where = f"videos[{n}]"
        if not isinstance(raw, dict):
            errors.append(f"{where}: must be an object")
            continue
        if raw.get("site") not in site_names:
            errors.append(f"{where}.site: {raw.get('site')!r} is not a site in this manifest")
        if not _safe_relpath(raw.get("path")):
            errors.append(f"{where}.path: must be a relative path")
        videos.append(
            Video(
                id=str(raw.get("id") or n),
                site=str(raw.get("site")),
                path=str(raw.get("path")),
                bytes=_int(raw.get("bytes", 0), f"{where}.bytes", errors),
                licence=str(raw.get("licence") or ""),
                source=str(raw.get("source") or ""),
            )
        )

    tier_def = TIERS.get(tier) if isinstance(tier, str) else None
    total = sum(s.bytes for s in sites)
    if tier_def and tier_def.cap_bytes is not None and total > tier_def.cap_bytes:
        errors.append(f"sites: {total} bytes exceeds the {tier} cap of {tier_def.cap_bytes} bytes")

    if errors:
        raise ManifestError(errors)
    return Manifest(
        tier=str(tier),
        version=str(version),
        isps=tuple(isps),
        sites=tuple(sites),
        threat_domains=tuple(threats),
        videos=tuple(videos),
        description=str(doc.get("description") or ""),
        extra={k: v for k, v in doc.items() if k in ("generated_at", "totals")},
    )
