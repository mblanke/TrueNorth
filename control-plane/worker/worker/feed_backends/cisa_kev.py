"""CISA's Known Exploited Vulnerabilities catalogue.

One ``GET`` of the whole catalogue (JSON, ~1.5 MB). Only the vulnerabilities whose
``vendorProject`` and ``product`` name a product in the product map are kept, each as
``kev:<CVE>`` (kind ``vulnerability``) dated the day CISA added it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

from .base import BaseFeedSource, FeedEntry, FeedMalformedError, FeedProduct, FeedResult, kev_key

URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


class CisaKevSource(BaseFeedSource):
    name = "cisa_kev"
    label = "CISA Known Exploited Vulnerabilities"

    def __init__(self, url: str = URL):
        self.url = url

    def fetch(self, http, products: Sequence[FeedProduct], *, since: datetime | None, now: datetime) -> FeedResult:
        wanted = {k for p in products for k in p.product_keys() if k.startswith("kev:")}
        if not wanted:
            return FeedResult()
        doc = http.get_json(self.url)
        vulns = doc.get("vulnerabilities") if isinstance(doc, dict) else None
        if not isinstance(vulns, list):
            raise FeedMalformedError("the CISA KEV catalogue has no vulnerabilities list")
        return FeedResult(entries=self.entries(vulns, wanted, now))

    @staticmethod
    def entries(vulns: list, wanted: set[str], now: datetime) -> list[FeedEntry]:
        out = []
        for v in vulns:
            if not isinstance(v, dict) or not str(v.get("cveID") or "").startswith("CVE-"):
                continue
            product = kev_key(v.get("vendorProject", ""), v.get("product", ""))
            if product not in wanted:
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
                data={"cve": cve, "vendor": str(v.get("vendorProject") or ""), "product": str(v.get("product") or ""),
                      "due": str(v.get("dueDate") or ""),
                      "ransomware": str(v.get("knownRansomwareCampaignUse") or "")}))
        return out
