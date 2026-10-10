"""Greyspace corpus ingest: WARC archives -> a corpus tree (plan slice 8, ADR 0007).

The offline half of corpus operations. A crawl (wget --warc-file, Heritrix, Browsertrix,
or ``corpus.py sample-warc`` for the CI sample) produces WARC files; ``extract`` turns
their ``response`` records into the corpus layout every tier shares::

    sites/<fqdn>/<path>       one file per captured URL (``/`` and ``/dir/`` -> index.html)

and ``report`` says how big the tree is and how much of it is duplicate content, so a
curator can see what a corpus costs before it goes onto the NetApp volume. The manifest
and checksums are written by ``corpus.py index`` (the same code the Mac sample uses).

Only successful (200) responses with a body are kept; a URL captured twice keeps the
last capture. Paths are normalised and confined to the site directory: a record whose
path would escape it (``..``, absolute, control characters) is skipped and counted.

Pure standard library; reads gzip-per-record (``.warc.gz``) and plain ``.warc``.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import posixpath
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .manifest import valid_fqdn

WARC_VERSION = b"WARC/1.0"
MAX_RECORD_BYTES = 64 * 1024 * 1024


@dataclass
class WarcRecord:
    headers: dict[str, str]
    block: bytes

    @property
    def type(self) -> str:
        return self.headers.get("warc-type", "")

    @property
    def uri(self) -> str:
        return self.headers.get("warc-target-uri", "").strip("<>")


def _read_line(fh) -> bytes:
    return fh.readline(65536)


def read_records(fh) -> Iterator[WarcRecord]:
    """Every record of a (decompressed) WARC stream."""
    while True:
        line = _read_line(fh)
        if not line:
            return
        if not line.strip():
            continue
        if not line.startswith(b"WARC/"):
            raise ValueError(f"not a WARC record header: {line[:40]!r}")
        headers: dict[str, str] = {}
        while True:
            h = _read_line(fh)
            if not h or h in (b"\r\n", b"\n"):
                break
            k, _, v = h.decode("utf-8", "replace").partition(":")
            headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0"))
        if length > MAX_RECORD_BYTES:
            fh.read(length)
            continue
        yield WarcRecord(headers, fh.read(length))


def open_warc(path: Path):
    """A streaming reader: gzip.open reads every member of a ``.warc.gz`` in turn."""
    with path.open("rb") as fh:
        magic = fh.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return io.BufferedReader(io.FileIO(str(path), "r"))


def write_record(out, rtype: str, uri: str, block: bytes, *, date: str, content_type: str) -> None:
    """One gzip member per record, as crawlers write ``.warc.gz``."""
    rid = hashlib.sha1(f"{rtype}{uri}{date}".encode()).hexdigest()
    head = (
        f"WARC/1.0\r\nWARC-Type: {rtype}\r\nWARC-Record-ID: <urn:sha1:{rid}>\r\nWARC-Date: {date}\r\n"
        f"WARC-Target-URI: {uri}\r\nContent-Type: {content_type}\r\nContent-Length: {len(block)}\r\n\r\n"
    ).encode()
    out.write(gzip.compress(head + block + b"\r\n\r\n", mtime=0))


def http_response(body: bytes, content_type: str) -> bytes:
    return (
        f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    )


def _split_http(block: bytes) -> tuple[int, bytes]:
    head, sep, body = block.partition(b"\r\n\r\n")
    if not sep:
        return 0, b""
    status_line = head.split(b"\r\n", 1)[0].split()
    try:
        status = int(status_line[1])
    except (IndexError, ValueError):
        return 0, b""
    return status, body


def target_path(uri: str) -> tuple[str, str] | None:
    """``(fqdn, relative path)`` for a captured URL, or None when it cannot be stored safely."""
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not valid_fqdn(host):
        return None
    path = unquote(parts.path or "/")
    if any(ord(c) < 32 for c in path) or "\\" in path:
        return None
    norm = posixpath.normpath("/" + path.lstrip("/"))
    if path.endswith("/") or norm == "/":
        norm = posixpath.join(norm, "index.html")
    rel = norm.lstrip("/")
    if not rel or rel.startswith("..") or any(p in ("", ".", "..") for p in rel.split("/")):
        return None
    return host, rel


@dataclass
class IngestResult:
    records: int = 0
    stored: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    sites: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def extract(warcs: list[Path], out: Path, *, only: set[str] | None = None) -> IngestResult:
    """Write every 200 response of ``warcs`` under ``out/sites/<fqdn>/``. ``only``: hosts to keep."""
    result = IngestResult()
    sites_root = (out / "sites").resolve()
    for warc in warcs:
        with open_warc(warc) as fh:
            _extract_stream(fh, sites_root, only, result)
    return result


def _extract_stream(fh, sites_root: Path, only: set[str] | None, result: IngestResult) -> None:
    for rec in read_records(fh):
        result.records += 1
        if rec.type != "response":
            result.skip(f"warc-type {rec.type or 'none'}")
            continue
        target = target_path(rec.uri)
        if target is None:
            result.skip("unsafe or unsupported URL")
            continue
        host, rel = target
        if only is not None and host not in only:
            result.skip("host not selected")
            continue
        status, body = _split_http(rec.block)
        if status != 200 or not body:
            result.skip(f"http status {status}")
            continue
        dest = (sites_root / host / rel).resolve()
        if not str(dest).startswith(str(sites_root / host) + "/"):
            result.skip("path escapes the site")
            continue
        if dest.exists() and dest.is_dir():
            result.skip("path is a directory")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        result.stored += 1
        result.sites[host] = result.sites.get(host, 0) + 1


def report(root: Path) -> dict:
    """Size, file count and duplicate content of a corpus tree, per site and in total."""
    by_hash: dict[str, list[tuple[str, int]]] = {}
    per_site: dict[str, dict[str, int]] = {}
    total = files = 0
    for path in sorted((root / "sites").rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root / "sites").as_posix()
        site = rel.split("/", 1)[0]
        size = path.stat().st_size
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        by_hash.setdefault(digest, []).append((rel, size))
        s = per_site.setdefault(site, {"files": 0, "bytes": 0})
        s["files"] += 1
        s["bytes"] += size
        total += size
        files += 1
    dupes = {h: v for h, v in by_hash.items() if len(v) > 1}
    wasted = sum(v[0][1] * (len(v) - 1) for v in dupes.values())
    largest = sorted(((size, rel) for v in by_hash.values() for rel, size in v), reverse=True)[:10]
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
        "bytes": total,
        "unique_bytes": total - wasted,
        "duplicate_groups": len(dupes),
        "duplicate_bytes": wasted,
        "dedupe_ratio": round((total - wasted) / total, 4) if total else 1.0,
        "sites": per_site,
        "largest": [{"path": rel, "bytes": size} for size, rel in largest],
        "duplicates": [
            {"sha256": h, "bytes": v[0][1], "paths": [rel for rel, _ in v][:10]}
            for h, v in sorted(dupes.items(), key=lambda kv: -kv[1][0][1] * len(kv[1]))[:20]
        ],
    }
