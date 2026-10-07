# Greyspace corpus — tiers, sources, ingest and storage plan

Status: T0 and T1 are built by scripts in `greyspace/scripts/`. **T2 and Full are a plan
only: nothing large has been downloaded.** Decisions: ADR 0007. Design: `docs/greyspace-plan.md`.

## 1. One layout, one manifest, four tiers

Every tier is a directory with the same shape, so the generator
(`control-plane/api/app/greyspace/config.py`), the web farm and `corpus.py verify` treat
them identically:

```
<corpus root>/
  manifest.json        format "greyspace-corpus/1": tier, version, ISPs + prefixes,
                       sites (fqdn, aliases, ip, category, path, files, bytes, licence, source),
                       videos, threat_actors, totals
  checksums.sha256     sha256sum-compatible, every file under sites/
  sites/<fqdn>/...     static trees (WARC-extracted or mirrored), served read-only
  warc/                provenance: the WARCs a tier was built from (not served)
```

| Tier | Size | Location | Builder | Status |
|---|---|---|---|---|
| T0 CI fixture | < 1 MB (cap 100 MB) | generated per build | `corpus.py t0` | built in CI |
| T1 Mac sample | **hard cap 5 GB** | `~/greyspace-corpus` | `build-sample.sh --tier mac` | script ready, not run |
| T2 Lab | ~50 GB | NetApp `greyspace-corpus-lab` | this plan | planned |
| Full | 10 TB+ | NetApp `greyspace-corpus` | this plan | planned |

The control plane reads a manifest from `$GREYSPACE_MANIFEST_DIR/<tier>/manifest.json`
when mounted (T0 is built in), so it can validate a range's block against T1/T2/Full
without mounting the content.

## 2. Sources and licensing

Only content we may redistribute onto a deployable kit. Every site entry records
`licence` and `source`; a tier is reviewed (licence, objectionable content, personal data)
before it leaves the machine that built it.

| Source | Licence | Use | Notes |
|---|---|---|---|
| Wikipedia / Wikinews / Wikivoyage / Wikibooks / Wiktionary | CC BY-SA (Wikinews CC BY 2.5) | reference, news, travel | Prefer Kiwix ZIM or Wikimedia HTML dumps over crawling (T2, Full); attribution page per site |
| Wikimedia Commons (subset) | per file (CC0, CC BY, CC BY-SA, PD) | images, video | Keep only files whose licence is in the allow list; licence per file in a sidecar |
| Internet Archive public-domain collections (e.g. Prelinger) | PD / CC0 | video (the bulk of Full) | Download via the IA S3-like API with per-item metadata |
| Project documentation (Python, Debian, MDN, RFC Editor) | PSF, OPL/GPL, CC BY-SA, IETF Trust | dev / code-hosting pack | Mirror docs only; no trademarks in synthetic branding |
| OpenStreetMap wiki | CC BY-SA 2.0 | reference | |
| Synthetic sites (T0 generator, LLM-free templates) | CC0 | news, social, webmail, gov, shopping look-alikes | Fictional brands; the only source for "gov/military" and "social" packs |

Excluded without legal review: Common Crawl (content copyright stays with sites), news
publishers, social networks, anything requiring login or with `noarchive`. Real brand
look-alikes are synthetic, never scraped.

## 3. Crawl and ingest pipeline (T2, Full)

1. **Acquire to WARC.** Dumps first (Kiwix ZIM → `zimdump` to HTML; Wikimedia HTML
   dumps; IA items). Crawls with **Browsertrix Crawler** (headless Chromium, JS sites,
   WARC + WACZ, per-seed scope and page limits) for dynamic sites, **wget2** (`--warc-file`,
   parallel, HTTP/2) for static docs. Politeness: robots.txt, ≤ 1 req/s per host, a
   descriptive User-Agent, crawl windows agreed with the network owner.
2. **Extract.** WARC → static tree under `sites/<fqdn>/` (`warc2zim` or `pywb`
   extraction for Browsertrix output; wget2 trees are already static). Rewrite absolute
   links to the site's own host; drop third-party trackers and ads.
3. **Dedupe.** Within a crawl: WARC `revisit` records (Browsertrix `--dedupIndexUrl`,
   wget2 CDX). Across sites: content-addressed (sha256) hard links for identical files
   (shared JS/CSS/images). On the array: NetApp inline dedupe + compression on the volume.
4. **Manifest and checksums.** `corpus.py index` assigns addresses and writes
   `manifest.json` + `checksums.sha256` (sha256 of every served file); `corpus.py verify`
   re-hashes after every copy. Video goes in `videos[]` with its site.
5. **Review.** Licence allow list, objectionable-content scan, PII scan; failures are
   removed and the manifest regenerated.
6. **Publish.** Copy to the NetApp volume, verify, then take a snapshot named for the
   version (`corpus@2026.10`). Ranges pin a version; old versions stay as snapshots.

## 4. NetApp layout

```
volume greyspace-ingest        rw, crawler and extraction workspace; never exported to ranges
  warc/<source>/<yyyy-mm>/*.warc.gz, *.cdx
  staging/<version>/           extracted tree being reviewed
volume greyspace-corpus-lab    T2 (~50 GB); NFSv4 export, read-only, gs-core hosts only
volume greyspace-corpus        Full (10 TB+); NFSv4 export, read-only, gs-core hosts only
  manifest.json, checksums.sha256, sites/, (videos live inside their sites)
  .snapshot/corpus@<version>   one snapshot per published version
```

Exports are read-only and limited to the gs-core hosts' storage-VLAN addresses; student
VLANs never reach NFS (`docs/dell-netapp-mobile-kit-inventory.md`, STORAGE VLAN). The
per-range breadcrumb overlay is a local volume on gs-core, never on these volumes.

## 5. Staged download order

| Stage | Content | Size (est.) | Gives |
|---|---|---|---|
| 1 | T1 sample on a Mac (`build-sample.sh --tier mac`) | ≤ 5 GB | real pages through the stack; validates layout |
| 2 | Synthetic packs at scale (gov, social, webmail, shopping look-alikes) | 2–5 GB | the brands exercises reference |
| 3 | Wikipedia EN (Kiwix `wikipedia_en_all_maxi`) + Wikivoyage/Wikinews ZIMs | ~110 GB | search, reference, news — T2 takes a 30 GB topic subset |
| 4 | Dev/doc pack (Python, MDN, Debian, RFCs, package mirror metadata) | 20–40 GB | developer and code-hosting realism |
| 5 | Commons image subset (licence-filtered) | 0.5–1 TB | image hosts, OSINT steps |
| 6 | IA public-domain video | 5–8 TB | video sites (most of Full) |
| 7 | Browsertrix crawls of permissive sites for breadth | 0.5–1 TB | long tail of "random sites" |

T2 (~50 GB) = stage 1 + 2 + a 30 GB slice of 3 + 10 GB of 4.

## 6. Bandwidth and time estimate

- **Line rate.** 1 Gbit/s sustained ≈ 450 GB/h. Full (10 TB) ≈ 23 h at line rate,
  ≈ 2–3 days at a realistic 400–500 Mbit/s effective for dump/IA downloads.
- **Polite crawl.** At 1 req/s per host, 30 hosts in parallel and ~150 KB per page with
  requisites ≈ 4.5 MB/s ≈ 16 GB/h; crawled stages (7) take days to weeks of wall time
  and are throttled by politeness, not bandwidth.
- **T2** (~50 GB): ≈ 1–2 h of downloads plus 3–4 h of crawl/extract.
- **T1** (5 GB): ≈ 1–3 h on home broadband with the script's 2 MB/s limit per site.
- **Processing.** Extraction + hashing ≈ 200–400 MB/s per core on NVMe; Full ≈ 8–12 h on
  a 16-core box. Copy to NetApp over 10/25 GbE ≈ 3–5 h, then `corpus.py verify`.

## 7. Open items

- Legal review of the licence allow list and attribution pages before any tier ships on a kit.
- Search index per corpus version (stage 3 dependency for the search service).
- TLS: the Greyspace root CA and per-site certificates (ADR 0007, later slice).
