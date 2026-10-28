"""CISA's Known Exploited Vulnerabilities catalogue.

One ``GET`` of the whole catalogue (JSON, ~1.8 MB). Only the vulnerabilities whose
``vendorProject`` and ``product`` match a KEV pattern in the product map (a name, or a
prefix ending in ``*``: ``FeedProduct.kev``) are kept, each as ``kev:<CVE>`` (kind
``vulnerability``) dated the day CISA added it, naming its own exact vendor/product key.

A configured pattern that matches no entry of the whole catalogue is reported as a
per-product warning, so a mapping that names a product the way KEV does not ("Windows
Server", when KEV says "Windows") shows on the status panel instead of passing silently.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

from .base import (
    BaseFeedSource,
    FeedEntry,
    FeedMalformedError,
    FeedProduct,
    FeedResult,
    kev_key,
    kev_pattern_matches,
)

URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


class CisaKevSource(BaseFeedSource):
    name = "cisa_kev"
    label = "CISA Known Exploited Vulnerabilities"

    def __init__(self, url: str = URL):
        self.url = url

    def fetch(self, http, products: Sequence[FeedProduct], *, since: datetime | None, now: datetime) -> FeedResult:
        mapped = [p for p in products if p.kev]
        if not mapped:
            return FeedResult()
        doc = http.get_json(self.url)
        vulns = doc.get("vulnerabilities") if isinstance(doc, dict) else None
        if not isinstance(vulns, list):
            raise FeedMalformedError("the CISA KEV catalogue has no vulnerabilities list")
        catalogue_keys = {kev_key(v.get("vendorProject", ""), v.get("product", "")) for v in vulns
                          if isinstance(v, dict)}
        errors = []
        for p in mapped:
            for vendor, pattern in p.kev:
                if not any(kev_pattern_matches(vendor, pattern, k) for k in catalogue_keys):
                    msg = f"{p.name}: no CISA KEV entry matches {vendor} / {pattern}"
                    if msg not in errors:
                        errors.append(msg)
        return FeedResult(entries=self.entries(vulns, mapped, now), errors=errors)

    @staticmethod
    def entries(vulns: list, products: Sequence[FeedProduct], now: datetime) -> list[FeedEntry]:
        out = []
        for v in vulns:
            if not isinstance(v, dict) or not str(v.get("cveID") or "").startswith("CVE-"):
                continue
            product = kev_key(v.get("vendorProject", ""), v.get("product", ""))
            if not any(p.covers(product) for p in products):
                continue
            try:
                d = date.fromisoformat(str(v.get("dateAdded")))
                added = datetime(d.year, d.month, d.day, tzinfo=UTC)
            except ValueError:
                added = now
            cve = str(v["cveID"]).strip()
            out.append(FeedEntry(
                key=f"kev:{cve}", source="cisa_kev", kind="vulnerability", products=(product,), at=added,
                title=str(v.get("vulnerabilityName") or cve)[:500],
                data={"cve": cve, "vendor": str(v.get("vendorProject") or "").strip(),
                      "product": str(v.get("product") or "").strip(), "due": str(v.get("dueDate") or ""),
                      "ransomware": str(v.get("knownRansomwareCampaignUse") or "")}))
        return out
