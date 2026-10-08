"""TrueNorth Range — fetching a URL someone else chose, without becoming their proxy.

Any API feature that fetches an author-supplied URL (threat-intel CSV feeds, curriculum web
pages) is a server-side request made on that author's behalf from inside the platform
network. ``fetch`` is the one way to do that:

- only http(s);
- the host is resolved ONCE and every address it resolves to is vetted: loopback,
  link-local (cloud metadata), multicast, unspecified, reserved and IPv4-mapped IPv6 are
  always refused; any other non-global address (RFC 1918, carrier-grade NAT 100.64.0.0/10,
  documentation ranges) is refused unless the caller passes ``allow_private`` (an
  air-gapped range serving its own content);
- the connection is pinned to a vetted address (``PinnedBackend``), so a DNS answer that
  changes between the check and the connect (rebinding) cannot steer it; the URL is left as
  it is, so the Host header, TLS SNI and certificate verification use the real hostname;
- redirects are not followed by the client (a redirect is how a vetted public host points
  the fetch at an internal one); a caller may allow a few (``max_redirects``), and each
  hop is then vetted and pinned again from scratch. ``trust_env=False`` ignores
  HTTP(S)_PROXY / NO_PROXY / .netrc;
- the body is capped at ``max_bytes``;
- failures raise one of three exception types carrying no detail of their own, so callers
  report one fixed message per outcome and the endpoint cannot be used to tell open ports
  from closed ones or live hosts from dead ones.

Extracted from ``threat_intel_backends/csv_feed.py`` (2026-10-08) when the curriculum
ingest was found following redirects to anywhere.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpcore
import httpx

SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")  # carrier-grade NAT, RFC 6598

# Module attributes so tests can answer DNS and stand in for sockets.
_resolve = socket.getaddrinfo
_network_backend = httpcore.SyncBackend


class GuardError(Exception):
    """Base class. Deliberately carries no detail: callers map each type to fixed text."""


class DestinationRefusedError(GuardError):
    """Not http(s), or the host resolves to an address the guard does not allow."""


class UnreachableError(GuardError):
    """DNS failed, the connection failed, it timed out, or the answer was not a 2xx."""


class TooLargeError(GuardError):
    """The body exceeded ``max_bytes``."""


def refused(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool) -> bool:
    """True when ``addr`` may not be fetched from."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return True  # ::ffff:127.0.0.1 is 127.0.0.1 to the socket
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return True  # never, even with private destinations allowed
    if addr.is_global:
        return False
    # private, shared (100.64.0.0/10) and other non-global space: an air-gapped range only
    return not allow_private


def check_destination(url: str, *, allow_private: bool) -> str:
    """The vetted address to dial for ``url``. Every address the host resolves to must pass
    ``refused``; the fetch is then pinned to the first (never re-resolved)."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise DestinationRefusedError()
    try:
        literal = ipaddress.ip_address(parts.hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:  # an address, not a name: nothing to resolve
        if refused(literal, allow_private=allow_private):
            raise DestinationRefusedError()
        return str(literal)
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        infos = _resolve(parts.hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        raise UnreachableError() from exc
    if not infos:
        raise UnreachableError()
    vetted = []
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if refused(addr, allow_private=allow_private):
            raise DestinationRefusedError()
        vetted.append(addr)
    return str(vetted[0])


class PinnedBackend(httpcore.NetworkBackend):
    """Opens TCP connections for one host only, and only to the address vetted for it."""

    def __init__(self, host: str, address: str, inner: httpcore.NetworkBackend | None = None) -> None:
        self.host, self.address = host, address
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host.lower() != self.host:
            raise httpcore.ConnectError(f"connection to {host!r} refused: the fetch is pinned to {self.host!r}")
        return self._inner.connect_tcp(self.address, port, timeout, local_address, socket_options)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("a guarded fetch does not use unix sockets")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class PinnedTransport(httpx.HTTPTransport):
    """``httpx.HTTPTransport`` whose connection pool dials ``address`` for ``host``."""

    def __init__(self, host: str, address: str) -> None:
        super().__init__()
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=1,
            network_backend=PinnedBackend(host, address, _network_backend()),
        )


@dataclass(frozen=True)
class Fetched:
    body: bytes
    content_type: str


def _client(url: str, *, allow_private: bool, timeout: float) -> httpx.Client:
    """A client for one vetted hop: pinned, no redirects, no environment proxies."""
    address = check_destination(url, allow_private=allow_private)
    try:
        host = httpx.URL(url).raw_host.decode("ascii").lower()  # what httpcore will dial for
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise DestinationRefusedError() from exc
    return httpx.Client(
        timeout=timeout, follow_redirects=False, trust_env=False, transport=PinnedTransport(host, address)
    )


def fetch(url: str, *, max_bytes: int, allow_private: bool = False, timeout: float = 10.0,
          accept: str = "*/*", max_redirects: int = 0) -> Fetched:
    """GET ``url`` under every rule in the module docstring. Synchronous: call it from a
    thread (``asyncio.to_thread``) in async code.

    ``max_redirects`` > 0 follows that many redirects, each hop a new fetch under the same
    rules: its target is resolved, vetted and pinned again (a refused target raises
    ``DestinationRefusedError``). One redirect more than that is ``UnreachableError``.
    """
    for hop in range(max_redirects + 1):
        try:
            with (
                _client(url, allow_private=allow_private, timeout=timeout) as client,
                client.stream("GET", url, headers={"Accept": accept}) as resp,
            ):
                location = resp.headers.get("location")
                if resp.is_redirect and location and hop < max_redirects:
                    url = str(resp.url.join(location))
                    continue
                if resp.status_code >= 300:
                    raise UnreachableError()
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise TooLargeError()
                return Fetched(bytes(body), resp.headers.get("content-type", ""))
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise UnreachableError() from exc
    raise UnreachableError()  # unreachable: the last hop returns or raises


def probe(url: str, *, allow_private: bool = False, timeout: float = 10.0) -> int:
    """The HTTP status ``url`` answers a guarded GET with (redirects not followed, body not
    read). For reachability checks; failures raise the same detail-free errors as fetch."""
    try:
        with (
            _client(url, allow_private=allow_private, timeout=timeout) as client,
            client.stream("GET", url) as resp,
        ):
            return resp.status_code
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise UnreachableError() from exc
