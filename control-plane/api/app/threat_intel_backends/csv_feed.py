"""TrueNorth Range — CSV threat intelligence feed backend.

Reads a CSV with a header row, fetched from the feed's URL or uploaded by an operator.

  required columns   type, value
  optional columns   first_seen (ISO 8601 date or datetime), mitre_technique (ATT&CK ids,
                     separated by ; , | or spaces), severity (low|medium|high|critical),
                     confidence (0-100), name, description

``type`` is one of ipv4, ipv6, ip (either), domain, url, md5, sha1, sha256, email, and the
value must be one. A row that fails any check is rejected with its row number and the
reason; the rest of the feed is still read. A feed without the required columns, or that
is not UTF-8 CSV, is refused whole (``FeedMalformedError``).

Fetching a URL from the API server is a server-side request on an author's behalf, so:
only http(s); redirects are not followed; the response is capped (THREAT_INTEL_MAX_BYTES,
default 5 MiB); a host that resolves to a loopback, link-local, multicast, reserved or
IPv4-mapped IPv6 address is refused, as is any other non-global one (private, carrier-grade
NAT 100.64.0.0/10) unless THREAT_INTEL_ALLOW_PRIVATE_FEEDS=true (an air-gapped range serving
its own feed); and every refusal, and every fetch failure, has one fixed message, so the
endpoint cannot be used to tell open ports from closed ones. Rejection reasons never echo
row content, so a refused page cannot be read back through them.

The host is resolved once, every address it resolves to is vetted by those rules, and the
connection is then pinned to the first of them (``app.net_guard.PinnedBackend``): the address dialled
is always one that passed the checks, and the request never asks DNS again, so an answer
that changes between the check and the connect (DNS rebinding) cannot steer it elsewhere.
The URL is left as it is, so the Host header, TLS SNI and certificate verification still
use the feed's own hostname. Because the connection is pinned (and ``trust_env=False``),
the fetch goes direct and ignores HTTP(S)_PROXY / NO_PROXY / .netrc from the environment.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import os
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .. import net_guard
from ..mitre import split_attack_ids, unknown_attack_ids
from .base import (
    BaseFeedBackend,
    FeedIndicator,
    FeedMalformedError,
    FeedPull,
    FeedRejection,
    FeedSourceError,
    FeedUnreachableError,
)

REQUIRED_COLUMNS = ("type", "value")
SEVERITIES = frozenset({"low", "medium", "high", "critical"})
MAX_ROWS = 50_000
FETCH_TIMEOUT = 10.0

_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:(?!-)[a-z0-9-]{1,63}(?<!-)\.)+[a-z]{2,63}$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@(?:(?!-)[a-z0-9-]{1,63}(?<!-)\.)+[a-z]{2,63}$")
_HEX = re.compile(r"^[0-9a-f]+$")
_HASH_LENGTHS = {"md5": 32, "sha1": 40, "sha256": 64}


def max_bytes() -> int:
    try:
        return max(int(os.getenv("THREAT_INTEL_MAX_BYTES", str(5 * 1024 * 1024))), 1)
    except ValueError:
        return 5 * 1024 * 1024


def _allow_private() -> bool:
    return os.getenv("THREAT_INTEL_ALLOW_PRIVATE_FEEDS", "false").strip().lower() in ("1", "true", "yes", "on")


# -- values --------------------------------------------------------------------------------
def _normalise(kind: str, value: str) -> tuple[str, str] | str:
    """(type, value) normalised, or the reason the value is not one."""
    if kind in ("ip", "ipv4", "ipv6"):
        try:
            addr = ipaddress.ip_address(value)
        except ValueError:
            return f"value is not a valid {kind} address"
        actual = f"ipv{addr.version}"
        if kind != "ip" and actual != kind:
            return f"value is not a valid {kind} address"
        return actual, str(addr)
    if kind == "domain":
        v = value.lower().rstrip(".")
        return (kind, v) if _DOMAIN.match(v) else "value is not a valid domain"
    if kind == "url":
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https", "ftp") or not parts.netloc:
            return "value is not a valid url"
        return kind, value
    if kind in _HASH_LENGTHS:
        v = value.lower()
        if len(v) != _HASH_LENGTHS[kind] or not _HEX.match(v):
            return f"value is not a valid {kind} hash"
        return kind, v
    if kind == "email":
        v = value.lower()
        return (kind, v) if _EMAIL.match(v) else "value is not a valid email address"
    return "unknown indicator type (expected ip, ipv4, ipv6, domain, url, md5, sha1, sha256 or email)"


def _first_seen(raw: str) -> datetime | str:
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return "first_seen is not an ISO 8601 date or datetime"
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _row(rownum: int, row: dict[str, str]) -> FeedIndicator | FeedRejection:
    def cell(name: str) -> str:
        return (row.get(name) or "").strip()

    kind, value = cell("type").lower(), cell("value")
    if not kind or not value:
        return FeedRejection(rownum, "type and value are required")
    normalised = _normalise(kind, value)
    if isinstance(normalised, str):
        return FeedRejection(rownum, normalised)
    kind, value = normalised

    first_seen = None
    if cell("first_seen"):
        parsed = _first_seen(cell("first_seen"))
        if isinstance(parsed, str):
            return FeedRejection(rownum, parsed)
        first_seen = parsed

    techniques = split_attack_ids(cell("mitre_technique"))
    if bad := unknown_attack_ids(techniques):
        return FeedRejection(rownum, f"unknown MITRE ATT&CK id(s): {len(bad)} not of the form T1234, T1234.001 or TA0001")

    severity = cell("severity").lower() or None
    if severity is not None and severity not in SEVERITIES:
        return FeedRejection(rownum, "severity must be low, medium, high or critical")

    confidence = None
    if cell("confidence"):
        try:
            confidence = int(cell("confidence"))
        except ValueError:
            return FeedRejection(rownum, "confidence must be a whole number 0-100")
        if not 0 <= confidence <= 100:
            return FeedRejection(rownum, "confidence must be a whole number 0-100")

    return FeedIndicator(
        indicator_type=kind,
        value=value,
        first_seen=first_seen,
        mitre_attack_ids=tuple(dict.fromkeys(techniques)),
        severity=severity,
        confidence=confidence,
        name=cell("name")[:500] or None,
        description=cell("description") or None,
    )


def parse_csv(content: bytes) -> FeedPull:
    """Indicators and rejected rows from CSV ``content``. Raises ``FeedMalformedError``."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FeedMalformedError("the feed is not UTF-8 text") from exc
    if not text.strip():
        raise FeedMalformedError("the feed is empty")
    if "\x00" in text:
        raise FeedMalformedError("the feed is not CSV text (it contains NUL bytes)")

    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        header = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    except csv.Error as exc:
        raise FeedMalformedError(f"the feed is not valid CSV: {exc}") from exc
    if missing := [c for c in REQUIRED_COLUMNS if c not in header]:
        raise FeedMalformedError(f"the feed has no {', '.join(missing)} column (header must name: type, value)")
    reader.fieldnames = header

    pull = FeedPull()
    seen: dict[tuple[str, str], int] = {}
    try:
        for rownum, row in enumerate(reader, start=2):
            if rownum - 1 > MAX_ROWS:
                raise FeedMalformedError(f"the feed has more than {MAX_ROWS} rows")
            if None in row:  # more cells than the header names
                pull.rejected.append(FeedRejection(rownum, "row has more cells than the header"))
                continue
            if not any((v or "").strip() for v in row.values()):
                continue  # blank line
            result = _row(rownum, row)
            if isinstance(result, FeedRejection):
                pull.rejected.append(result)
                continue
            key = (result.indicator_type, result.value)
            if key in seen:  # the later row wins
                pull.indicators[seen[key]] = result
            else:
                seen[key] = len(pull.indicators)
                pull.indicators.append(result)
    except csv.Error as exc:
        raise FeedMalformedError(f"the feed is not valid CSV at line {reader.line_num}: {exc}") from exc
    return pull


# -- fetching ------------------------------------------------------------------------------
# One text per outcome, whatever the cause: a message that told "did not resolve" from
# "refused connection" from "answered 404" would make the API a probe of internal hosts
# and ports for anyone who may set a feed URL.
REFUSED = (
    "the feed URL must be http(s) and point at a public address "
    "(THREAT_INTEL_ALLOW_PRIVATE_FEEDS=true allows private ones)"
)
UNREACHABLE = "the feed could not be fetched"


def fetch_url(url: str) -> bytes:
    """The body at ``url``, within the size cap. Raises ``FeedSourceError`` / ``FeedUnreachableError``.

    The resolve-once / vet-every-address / pin / no-redirect / no-proxy rules live in
    ``app.net_guard``, shared with the curriculum URL ingest."""
    limit = max_bytes()
    try:
        return net_guard.fetch(
            url, max_bytes=limit, allow_private=_allow_private(), timeout=FETCH_TIMEOUT,
            accept="text/csv, text/plain",
        ).body
    except net_guard.DestinationRefusedError as exc:
        raise FeedSourceError(REFUSED) from exc
    except net_guard.TooLargeError as exc:
        raise FeedSourceError(f"the feed is larger than {limit} bytes") from exc
    except net_guard.UnreachableError as exc:
        raise FeedUnreachableError(UNREACHABLE) from exc


class CsvFeedBackend(BaseFeedBackend):
    accepts_upload = True

    def fetch(self, url: str | None, content: bytes | None = None) -> FeedPull:
        if content is None:
            if not url:
                raise FeedSourceError("the feed has no URL; set one or upload the CSV")
            content = fetch_url(url)
        return parse_csv(content)
