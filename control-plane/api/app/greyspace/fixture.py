"""The T0 corpus: about 20 synthetic sites, generated, never committed (ADR 0007).

Everything here is invented and released CC0: brand names, people, articles. Domain
names are fictional and only ever resolve inside a Greyspace stack, never on the real
internet. ``greyspace/scripts/build_t0.py`` writes these pages to disk with a tiny video
and the manifest; the API uses ``t0_manifest_doc()`` directly, so the control plane can
render a range's Greyspace configuration without a corpus mount.

Pure standard library (the runtime builder imports this file without the API).
"""

from __future__ import annotations

import html
from typing import Any

VERSION = "2026.10-t0"
LICENCE = "CC0-1.0"
SOURCE = "synthetic:greyspace-t0"

ISPS = (
    {"name": "Northline Transit", "asn": 64501, "prefix": "198.18.0.0/16"},
    {"name": "Bluewater Carrier", "asn": 64502, "prefix": "198.19.0.0/16"},
)

# fqdn, category, title, tagline. Addresses are assigned in order below.
SITES: tuple[tuple[str, str, str, str], ...] = (
    ("maplewire-news.com", "news", "Maplewire News", "Regional news, every hour"),
    ("harbourdaily.ca", "news", "Harbour Daily", "The port city's paper of record"),
    ("northfield-gazette.org", "news", "Northfield Gazette", "Community news since 1952"),
    ("findly-search.com", "search", "Findly", "Search the Greyspace web"),
    ("portalhub.net", "search", "PortalHub", "News, weather and mail in one place"),
    ("chirpline.com", "social", "Chirpline", "Short posts from people you follow"),
    ("friendgrid.net", "social", "FriendGrid", "Stay close to your circle"),
    ("pastebox.io", "social", "Pastebox", "Share text in one click"),
    ("quillmail.com", "webmail", "QuillMail", "Free webmail with 15 GB"),
    ("clipstream-video.com", "video", "ClipStream", "Watch and share short clips"),
    ("tuberiver.tv", "video", "TubeRiver", "Live channels and replays"),
    ("revenue-agency.gc-sim.ca", "gov", "Revenue Agency (simulated)", "Taxes and benefits"),
    ("coastguard-sim.org", "gov", "Coast Guard Notices (simulated)", "Notices to mariners"),
    ("defence-news-sim.org", "gov", "Defence Bulletin (simulated)", "Procurement and exercises"),
    ("codehub.dev", "dev", "CodeHub", "Host and review code"),
    ("stackwise.dev", "dev", "Stackwise", "Questions and answers for developers"),
    ("pkgmirror.net", "dev", "PkgMirror", "Package mirror for the Greyspace web"),
    ("shopnorth.com", "shopping", "ShopNorth", "Everything, delivered"),
    ("tradepost-market.com", "shopping", "TradePost", "Buy and sell locally"),
    ("weatherwise.ca", "news", "WeatherWise", "Forecasts for the north"),
)

# Two threat-actor domains, one per role the T0 stack exercises (C2 and phishing).
THREAT_ACTORS = (
    {
        "name": "AMBER HERON",
        "domains": (
            {"fqdn": "update-cdn-sync.net", "ip": "198.19.250.10", "role": "c2"},
            {"fqdn": "acme-payroll-portal.com", "ip": "198.19.250.11", "role": "phish"},
        ),
    },
)

VIDEO_ID = "test-pattern"
VIDEO_SITE = "clipstream-video.com"


def site_ip(index: int) -> str:
    """Even sites on ISP A (198.18.10.x), odd ones on ISP B (198.19.20.x)."""
    return f"198.18.10.{11 + index}" if index % 2 == 0 else f"198.19.20.{11 + index}"


def _page(title: str, heading: str, body: str, nav: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        "<link rel=\"stylesheet\" href=\"/style.css\"></head>\n"
        f"<body><header><a href=\"/\">{html.escape(title)}</a> {nav}</header>\n"
        f"<main><h1>{html.escape(heading)}</h1>\n{body}\n</main>\n"
        "<footer>Synthetic Greyspace fixture site. CC0. Not a real organisation.</footer>"
        "</body></html>\n"
    )


def site_files(fqdn: str, category: str, title: str, tagline: str) -> dict[str, bytes]:
    """The static files of one T0 site, relative to its site root."""
    nav = '<nav><a href="/about.html">About</a> <a href="/news/latest.html">Latest</a></nav>'
    index_body = (
        f"<p class=\"tagline\">{html.escape(tagline)}</p>\n"
        f"<p data-gs-fixture=\"{html.escape(fqdn)}\">Welcome to {html.escape(title)}, a {category} site "
        "in the Greyspace T0 fixture.</p>\n<ul><li><a href=\"/news/latest.html\">Latest update</a></li></ul>"
    )
    if fqdn == VIDEO_SITE:
        index_body += (
            f'\n<video controls width="320" src="/media/{VIDEO_ID}.webm">'
            f'<a href="/media/{VIDEO_ID}.webm">Download the clip</a></video>'
        )
    files = {
        "index.html": _page(title, title, index_body, nav),
        "about.html": _page(
            f"About {title}", f"About {title}", f"<p>{html.escape(title)} is fictional ({category}).</p>", nav
        ),
        "news/latest.html": _page(
            f"{title}: latest", "Latest update", "<p>Nothing to report. Systems normal.</p>", nav
        ),
        "robots.txt": "User-agent: *\nAllow: /\n",
        "style.css": "body{font-family:sans-serif;max-width:46em;margin:2em auto}header{border-bottom:1px solid #ccc}\n",
    }
    return {path: text.encode("utf-8") for path, text in files.items()}


def t0_manifest_doc(video_bytes: int = 0, generated_at: str = "2026-10-07T00:00:00Z") -> dict[str, Any]:
    """The T0 manifest as a JSON-ready dict. Site sizes are exact; the video's size is
    what the builder measured (0 when the control plane renders without a built corpus)."""
    sites = []
    for n, (fqdn, category, title, tagline) in enumerate(SITES):
        files = site_files(fqdn, category, title, tagline)
        size = sum(len(b) for b in files.values())
        count = len(files)
        if fqdn == VIDEO_SITE and video_bytes:
            size += video_bytes
            count += 1
        sites.append(
            {
                "fqdn": fqdn,
                "aliases": [f"www.{fqdn}"] if fqdn.count(".") == 1 else [],
                "ip": site_ip(n),
                "category": category,
                "path": f"sites/{fqdn}",
                "files": count,
                "bytes": size,
                "licence": LICENCE,
                "source": SOURCE,
            }
        )
    return {
        "format": "greyspace-corpus/1",
        "tier": "t0",
        "version": VERSION,
        "generated_at": generated_at,
        "description": "Greyspace T0 CI fixture: synthetic sites, generated by greyspace/scripts/build_t0.py",
        "isps": [dict(i) for i in ISPS],
        "sites": sites,
        "videos": [
            {
                "id": VIDEO_ID,
                "site": VIDEO_SITE,
                "path": f"sites/{VIDEO_SITE}/media/{VIDEO_ID}.webm",
                "bytes": video_bytes,
                "licence": LICENCE,
                "source": "synthetic:ffmpeg-testsrc",
            }
        ],
        "threat_actors": [
            {"name": a["name"], "domains": [dict(d) for d in a["domains"]]} for a in THREAT_ACTORS
        ],
        "totals": {"sites": len(sites), "bytes": sum(s["bytes"] for s in sites)},
    }
