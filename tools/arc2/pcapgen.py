"""Synthetic teaching captures for ARC² courses: a reviewed spec in, a pcap out.

An agent never writes packet bytes. It writes a short YAML spec (hosts, segments and a
list of ordinary network events), and this module renders the capture from it, the same
way every time. The engine's check re-renders the spec and compares digests, so a pcap in
a run is always exactly what its spec says (``check.py``: ``inject.capture_*``).

What a spec can describe is deliberately narrow, so a capture can teach Wireshark without
carrying anything real or harmful:

* Addresses only from the documentation ranges (RFC 5737: 192.0.2.0/24,
  198.51.100.0/24, 203.0.113.0/24); names only under ``.example``, ``.test`` or
  ``.invalid``. No real network, no DND network detail.
* Events only from an allow-list: ARP, ping, DNS, HTTP, TLS ClientHello (SNI), a refused
  TCP connection. There is no raw-payload field, so no exploit or malware bytes.
* Bounded size and duration.

Traffic between segments is emitted on both legs through the segments' gateways (MACs and
TTL change at the router), as a capture on a core switch SPAN would show it, so students can
find hosts, gateways and segments.

Pure Python, no dependencies. ``render`` also writes a summary (hosts, segments, gateways,
conversations) that answer keys and validators are built from.

Usage:
    PYTHONPATH=tools .venv/bin/python -m arc2.pcapgen check  <spec.yaml>
    PYTHONPATH=tools .venv/bin/python -m arc2.pcapgen render <spec.yaml> <out.pcap> [--summary <out.json>]
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import random
import re
import struct
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

SCHEMA = "arc2/capture/0.1"
ALLOWED_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
NAME_SUFFIXES = (".example", ".test", ".invalid")
EVENT_TYPES = {"arp", "ping", "dns", "http", "tls", "tcp_refused"}
ROLES = {"workstation", "server", "web", "dns", "gateway", "printer", "external", "sensor"}
HTTP_STATUS = {
    200: "OK",
    301: "Moved Permanently",
    302: "Found",
    304: "Not Modified",
    403: "Forbidden",
    404: "Not Found",
    500: "Internal Server Error",
}
MAX_PACKETS = 20000
MAX_SECONDS = 3600
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{0,100}$")


class SpecError(ValueError):
    """A spec that cannot be rendered; the message lists every problem."""


# ── validation ────────────────────────────────────────────────────────────


def _allowed_ip(value: Any) -> ipaddress.IPv4Address | None:
    try:
        ip = ipaddress.ip_address(str(value))
    except ValueError:
        return None
    return ip if any(ip in n for n in ALLOWED_NETS) else None


def validate(spec: dict) -> list[str]:
    """Every problem with a spec, as sentences. Empty means it can be rendered."""
    errs: list[str] = []
    if not isinstance(spec, dict):
        return ["the spec is not a mapping"]
    if spec.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA!r}")
    if not isinstance(spec.get("id"), str) or not re.match(r"^[A-Za-z0-9._-]{1,80}$", spec["id"]):
        errs.append("id must be a short identifier (the inject id)")
    if not isinstance(spec.get("seed"), int):
        errs.append("seed must be an integer")
    try:
        datetime.fromisoformat(str(spec.get("start", "")).replace("Z", "+00:00"))
    except ValueError:
        errs.append("start must be an ISO 8601 time, e.g. 2026-10-01T13:00:00Z")

    segments = spec.get("segments") or []
    hosts = spec.get("hosts") or []
    if not isinstance(segments, list) or not segments:
        errs.append("segments must list at least one segment")
        segments = []
    if not isinstance(hosts, list) or len(hosts) < 2:
        errs.append("hosts must list at least two hosts")
        hosts = [h for h in hosts if isinstance(h, dict)] if isinstance(hosts, list) else []

    host_ids: dict[str, dict] = {}
    for h in hosts:
        hid = h.get("id") if isinstance(h, dict) else None
        if not isinstance(hid, str) or not ID_RE.match(hid):
            errs.append(f"host id {hid!r} must be lowercase letters, digits and hyphens")
            continue
        if hid in host_ids:
            errs.append(f"host {hid} is listed twice")
        host_ids[hid] = h
        if not _allowed_ip(h.get("ip")):
            errs.append(f"host {hid}: ip {h.get('ip')!r} must be in 192.0.2.0/24, 198.51.100.0/24 or 203.0.113.0/24")
        if h.get("role") not in ROLES:
            errs.append(f"host {hid}: role must be one of {', '.join(sorted(ROLES))}")
        name = h.get("name")
        if name is not None and (
            not isinstance(name, str) or not NAME_RE.match(name) or not name.endswith(NAME_SUFFIXES)
        ):
            errs.append(f"host {hid}: name {name!r} must be a hostname under .example, .test or .invalid")
    ips = [str(h.get("ip")) for h in hosts if isinstance(h, dict)]
    if len(ips) != len(set(ips)):
        errs.append("two hosts share an ip")

    seg_ids = set()
    for s in segments:
        sid = s.get("id") if isinstance(s, dict) else None
        if not isinstance(sid, str) or not ID_RE.match(sid):
            errs.append(f"segment id {sid!r} must be lowercase letters, digits and hyphens")
            continue
        seg_ids.add(sid)
        try:
            net = ipaddress.ip_network(str(s.get("cidr")), strict=True)
        except ValueError:
            errs.append(f"segment {sid}: cidr {s.get('cidr')!r} is not a network")
            continue
        if not any(net.subnet_of(n) for n in ALLOWED_NETS):
            errs.append(f"segment {sid}: cidr {net} must sit inside a documentation range")
        gw = host_ids.get(s.get("gateway"))
        if not gw:
            errs.append(f"segment {sid}: gateway {s.get('gateway')!r} is not a listed host")
        elif gw.get("role") != "gateway" or _allowed_ip(gw.get("ip")) not in net:
            errs.append(f"segment {sid}: gateway {gw.get('id')} must have role gateway and an ip in {net}")

    events = list(spec.get("events") or []) + list(spec.get("noise") or [])
    if not spec.get("events"):
        errs.append("events must list at least one event")
    for i, e in enumerate(events):
        where = f"event {i + 1}"
        if not isinstance(e, dict) or e.get("type") not in EVENT_TYPES:
            errs.append(f"{where}: type must be one of {', '.join(sorted(EVENT_TYPES))}")
            continue
        allowed_keys = {
            "type",
            "at",
            "every",
            "src",
            "dst",
            "server",
            "target",
            "query",
            "path",
            "status",
            "sni",
            "count",
            "port",
        }
        extra = set(e) - allowed_keys
        if extra:
            errs.append(f"{where}: unknown field(s) {', '.join(sorted(extra))}")
        at = e.get("at", 0)
        if not isinstance(at, (int, float)) or not 0 <= at <= MAX_SECONDS:
            errs.append(f"{where}: at must be seconds from the start, 0 to {MAX_SECONDS}")
        if "every" in e and (not isinstance(e["every"], (int, float)) or not 1 <= e["every"] <= MAX_SECONDS):
            errs.append(f"{where}: every must be 1 to {MAX_SECONDS} seconds")
        if e.get("src") not in host_ids:
            errs.append(f"{where}: src {e.get('src')!r} is not a listed host")
        peer = {"arp": "target", "dns": "server"}.get(e["type"], "dst")
        if e.get(peer) not in host_ids:
            errs.append(f"{where}: {peer} {e.get(peer)!r} is not a listed host")
        if e["type"] == "dns":
            q = e.get("query")
            if not isinstance(q, str) or not NAME_RE.match(q) or not q.endswith(NAME_SUFFIXES):
                errs.append(f"{where}: query must be a name under .example, .test or .invalid")
        if e["type"] == "http":
            if not PATH_RE.match(str(e.get("path", "/"))):
                errs.append(f"{where}: path must look like /index.html")
            if int(e.get("status", 200)) not in HTTP_STATUS:
                errs.append(f"{where}: status must be one of {sorted(HTTP_STATUS)}")
        if e["type"] == "tls":
            sni = e.get("sni")
            if not isinstance(sni, str) or not NAME_RE.match(sni) or not sni.endswith(NAME_SUFFIXES):
                errs.append(f"{where}: sni must be a name under .example, .test or .invalid")
        if "count" in e and (not isinstance(e["count"], int) or not 1 <= e["count"] <= 50):
            errs.append(f"{where}: count must be 1 to 50")
        if "port" in e and (not isinstance(e["port"], int) or not 1 <= e["port"] <= 65535):
            errs.append(f"{where}: port must be 1 to 65535")
    if not errs:
        try:
            _Topology(spec)
        except SpecError as exc:
            errs.append(str(exc))
    return errs


def load(path: Path) -> dict:
    try:
        spec = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SpecError(f"{path}: cannot read the spec: {exc}") from exc
    return spec


# ── packet building ───────────────────────────────────────────────────────


def _csum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def _mac(seed: str) -> bytes:
    h = hashlib.sha256(seed.encode()).digest()
    return bytes([0x02, h[0], h[1], h[2], h[3], h[4]])  # locally administered, unicast


def _ether(dst: bytes, src: bytes, ethertype: int, payload: bytes) -> bytes:
    frame = dst + src + struct.pack("!H", ethertype) + payload
    return frame + b"\0" * max(0, 60 - len(frame))  # pad to minimum frame size


def _ipv4(src: str, dst: str, proto: int, payload: bytes, ident: int, ttl: int) -> bytes:
    s, d = ipaddress.ip_address(src).packed, ipaddress.ip_address(dst).packed
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident & 0xFFFF, 0x4000, ttl, proto, 0, s, d)
    return hdr[:10] + struct.pack("!H", _csum(hdr)) + hdr[12:] + payload


def _pseudo(src: str, dst: str, proto: int, length: int) -> bytes:
    return ipaddress.ip_address(src).packed + ipaddress.ip_address(dst).packed + struct.pack("!BBH", 0, proto, length)


def _udp(src: str, dst: str, sport: int, dport: int, data: bytes) -> bytes:
    hdr = struct.pack("!HHHH", sport, dport, 8 + len(data), 0)
    c = _csum(_pseudo(src, dst, 17, 8 + len(data)) + hdr + data) or 0xFFFF
    return hdr[:6] + struct.pack("!H", c) + data


def _tcp(src: str, dst: str, sport: int, dport: int, seq: int, ack: int, flags: int, data: bytes = b"") -> bytes:
    hdr = struct.pack("!HHIIBBHHH", sport, dport, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, 5 << 4, flags, 64240, 0, 0)
    c = _csum(_pseudo(src, dst, 6, len(hdr) + len(data)) + hdr + data)
    return hdr[:16] + struct.pack("!H", c) + hdr[18:] + data


def _dns_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


FIN, SYN, RST, PSH, ACK = 0x01, 0x02, 0x04, 0x08, 0x10


class _Topology:
    """Hosts, segments and the gateway a packet crosses to reach another segment."""

    def __init__(self, spec: dict):
        self.hosts = {h["id"]: h for h in spec["hosts"]}
        self.segments = []
        for s in spec["segments"]:
            self.segments.append({**s, "net": ipaddress.ip_network(s["cidr"])})
        self.mac = {hid: _mac(f"{spec['id']}/{hid}") for hid in self.hosts}
        self.segment_of = {}
        for hid, h in self.hosts.items():
            ip = ipaddress.ip_address(h["ip"])
            seg = next((s for s in self.segments if ip in s["net"]), None)
            self.segment_of[hid] = seg["id"] if seg else None
            if seg is None and h["role"] != "external":
                raise SpecError(f"host {hid}: ip {ip} is in no segment; only role external may be outside")

    def gateway(self, seg_id: str) -> str:
        return next(s["gateway"] for s in self.segments if s["id"] == seg_id)

    def legs(self, src: str, dst: str) -> list[tuple[str, str, int]]:
        """(L2 source host, L2 destination host, ttl decrement) for each link the packet is seen on."""
        a, b = self.segment_of[src], self.segment_of[dst]
        if a == b and a is not None:
            return [(src, dst, 0)]
        legs = []
        if a is not None:
            legs.append((src, self.gateway(a), 0))
        if b is not None:
            legs.append((self.gateway(b), dst, 1))
        return legs


class _Writer:
    def __init__(self, spec: dict):
        self.spec = spec
        self.topo = _Topology(spec)
        self.rng = random.Random(spec["seed"])
        self.t0 = datetime.fromisoformat(str(spec["start"]).replace("Z", "+00:00")).timestamp()
        self.frames: list[tuple[float, bytes]] = []
        self.convs: dict[str, int] = {}
        self.dns: list[dict] = []
        self.http: list[dict] = []
        self.tls: list[dict] = []

    def ip(self, hid: str) -> str:
        return self.topo.hosts[hid]["ip"]

    def emit_ip(self, t: float, src: str, dst: str, proto: int, payload: bytes, ttl: int = 64) -> None:
        ident = self.rng.randrange(0, 0xFFFF)
        for l2s, l2d, dec in self.topo.legs(src, dst):
            pkt = _ipv4(self.ip(src), self.ip(dst), proto, payload, ident, ttl - dec)
            self.frames.append((t, _ether(self.topo.mac[l2d], self.topo.mac[l2s], 0x0800, pkt)))
            t += 0.0002

    def conv(self, kind: str) -> None:
        self.convs[kind] = self.convs.get(kind, 0) + 1

    def arp(self, t: float, src: str, target: str) -> None:
        s, tg = self.topo.hosts[src], self.topo.hosts[target]
        smac, tmac = self.topo.mac[src], self.topo.mac[target]
        req = struct.pack(
            "!HHBBH6s4s6s4s",
            1,
            0x0800,
            6,
            4,
            1,
            smac,
            ipaddress.ip_address(s["ip"]).packed,
            b"\0" * 6,
            ipaddress.ip_address(tg["ip"]).packed,
        )
        rep = struct.pack(
            "!HHBBH6s4s6s4s",
            1,
            0x0800,
            6,
            4,
            2,
            tmac,
            ipaddress.ip_address(tg["ip"]).packed,
            smac,
            ipaddress.ip_address(s["ip"]).packed,
        )
        self.frames.append((t, _ether(b"\xff" * 6, smac, 0x0806, req)))
        self.frames.append((t + 0.0004, _ether(smac, tmac, 0x0806, rep)))
        self.conv("arp")

    def ping(self, t: float, src: str, dst: str, count: int) -> None:
        ident = self.rng.randrange(1, 0xFFFF)
        data = b"abcdefghijklmnopqrstuvwabcdefghi"
        for seq in range(1, count + 1):
            for typ, a, b, dt in ((8, src, dst, 0.0), (0, dst, src, 0.0011)):
                hdr = struct.pack("!BBHHH", typ, 0, 0, ident, seq)
                icmp = hdr[:2] + struct.pack("!H", _csum(hdr + data)) + hdr[4:] + data
                self.emit_ip(t + dt, a, b, 1, icmp, 128 if self.topo.hosts[a]["role"] == "workstation" else 64)
            t += 1.0
        self.conv("icmp")

    def dns_query(self, t: float, src: str, server: str, query: str) -> None:
        txid, sport = self.rng.randrange(0, 0xFFFF), self.rng.randrange(49152, 65535)
        q = _dns_name(query) + struct.pack("!HH", 1, 1)
        answer = next((h["ip"] for h in self.topo.hosts.values() if h.get("name") == query), None)
        req = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0) + q
        self.emit_ip(t, src, server, 17, _udp(self.ip(src), self.ip(server), sport, 53, req))
        if answer:
            rr = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 300, 4) + ipaddress.ip_address(answer).packed
            resp = struct.pack("!HHHHHH", txid, 0x8180, 1, 1, 0, 0) + q + rr
        else:
            resp = struct.pack("!HHHHHH", txid, 0x8183, 1, 0, 0, 0) + q  # NXDOMAIN
        self.emit_ip(t + 0.004, server, src, 17, _udp(self.ip(server), self.ip(src), 53, sport, resp))
        self.dns.append({"client": src, "server": server, "query": query, "answer": answer})
        self.conv("dns")

    def _tcp_session(self, t: float, src: str, dst: str, dport: int, exchanges: list[tuple[str, bytes]]) -> float:
        sport = self.rng.randrange(49152, 65535)
        cs, ss = self.rng.randrange(0, 2**32), self.rng.randrange(0, 2**32)
        a, b = self.ip(src), self.ip(dst)
        self.emit_ip(t, src, dst, 6, _tcp(a, b, sport, dport, cs, 0, SYN))
        self.emit_ip(t + 0.001, dst, src, 6, _tcp(b, a, dport, sport, ss, cs + 1, SYN | ACK))
        self.emit_ip(t + 0.0015, src, dst, 6, _tcp(a, b, sport, dport, cs + 1, ss + 1, ACK))
        cs, ss, t = cs + 1, ss + 1, t + 0.01
        for who, data in exchanges:
            if who == "c":
                self.emit_ip(t, src, dst, 6, _tcp(a, b, sport, dport, cs, ss, PSH | ACK, data))
                cs += len(data)
                self.emit_ip(t + 0.001, dst, src, 6, _tcp(b, a, dport, sport, ss, cs, ACK))
            else:
                self.emit_ip(t, dst, src, 6, _tcp(b, a, dport, sport, ss, cs, PSH | ACK, data))
                ss += len(data)
                self.emit_ip(t + 0.001, src, dst, 6, _tcp(a, b, sport, dport, cs, ss, ACK))
            t += 0.02
        self.emit_ip(t, src, dst, 6, _tcp(a, b, sport, dport, cs, ss, FIN | ACK))
        self.emit_ip(t + 0.001, dst, src, 6, _tcp(b, a, dport, sport, ss, cs + 1, FIN | ACK))
        self.emit_ip(t + 0.002, src, dst, 6, _tcp(a, b, sport, dport, cs + 1, ss + 1, ACK))
        return t

    def http_get(self, t: float, src: str, dst: str, path: str, status: int) -> None:
        host = self.topo.hosts[dst].get("name") or self.ip(dst)
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: Mozilla/5.0 (training)\r\n"
            "Accept: text/html\r\nConnection: close\r\n\r\n"
        ).encode()
        body = f"<html><body><h1>{status} {HTTP_STATUS[status]}</h1><p>Training page.</p></body></html>".encode()
        resp = (
            f"HTTP/1.1 {status} {HTTP_STATUS[status]}\r\nServer: training-web\r\nContent-Type: text/html\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode() + body
        self._tcp_session(t, src, dst, 80, [("c", req), ("s", resp)])
        self.http.append({"client": src, "server": dst, "host": host, "path": path, "status": status})
        self.conv("http")

    def tls_hello(self, t: float, src: str, dst: str, sni: str) -> None:
        name = sni.encode()
        sni_ext = struct.pack("!HHHBH", 0, len(name) + 5, len(name) + 3, 0, len(name)) + name
        versions = struct.pack("!HHB", 43, 3, 2) + b"\x03\x04"
        groups = struct.pack("!HHH", 10, 4, 2) + b"\x00\x1d"
        exts = sni_ext + versions + groups
        rnd = bytes(self.rng.randrange(256) for _ in range(32))
        ciphers = b"\x13\x01\x13\x02\xc0\x2f"
        body = (
            b"\x03\x03"
            + rnd
            + b"\x00"
            + struct.pack("!H", len(ciphers))
            + ciphers
            + b"\x01\x00"
            + struct.pack("!H", len(exts))
            + exts
        )
        hs = b"\x01" + struct.pack("!I", len(body))[1:] + body
        record = b"\x16\x03\x01" + struct.pack("!H", len(hs)) + hs
        self._tcp_session(t, src, dst, 443, [("c", record)])
        self.tls.append({"client": src, "server": dst, "sni": sni})
        self.conv("tls")

    def refused(self, t: float, src: str, dst: str, port: int) -> None:
        sport, cs = self.rng.randrange(49152, 65535), self.rng.randrange(0, 2**32)
        a, b = self.ip(src), self.ip(dst)
        self.emit_ip(t, src, dst, 6, _tcp(a, b, sport, port, cs, 0, SYN))
        self.emit_ip(t + 0.001, dst, src, 6, _tcp(b, a, port, sport, 0, cs + 1, RST | ACK))
        self.conv("tcp_refused")

    def event(self, e: dict, t: float) -> None:
        kind = e["type"]
        if kind == "arp":
            self.arp(t, e["src"], e["target"])
        elif kind == "ping":
            self.ping(t, e["src"], e["dst"], e.get("count", 4))
        elif kind == "dns":
            self.dns_query(t, e["src"], e["server"], e["query"])
        elif kind == "http":
            self.http_get(t, e["src"], e["dst"], e.get("path", "/"), int(e.get("status", 200)))
        elif kind == "tls":
            self.tls_hello(t, e["src"], e["dst"], e["sni"])
        elif kind == "tcp_refused":
            self.refused(t, e["src"], e["dst"], e.get("port", 22))

    def run(self) -> None:
        horizon = max([float(e.get("at", 0)) for e in self.spec["events"]] + [1.0]) + 5
        for e in self.spec["events"]:
            self.event(e, float(e.get("at", 0)) + self.rng.uniform(0, 0.05))
        for e in self.spec.get("noise") or []:
            every, t = float(e.get("every", 30)), float(e.get("at", 0))
            while t <= horizon:
                self.event(e, t + self.rng.uniform(0, 0.2))
                t += every
                if len(self.frames) > MAX_PACKETS:
                    raise SpecError(f"the capture would exceed {MAX_PACKETS} packets")
        if len(self.frames) > MAX_PACKETS:
            raise SpecError(f"the capture would exceed {MAX_PACKETS} packets")
        self.frames.sort(key=lambda f: f[0])

    def pcap(self) -> bytes:
        out = [struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)]
        for t, frame in self.frames:
            ts = self.t0 + t
            sec, usec = int(ts), int(round((ts - int(ts)) * 1_000_000)) % 1_000_000
            out.append(struct.pack("<IIII", sec, usec, len(frame), len(frame)) + frame)
        return b"".join(out)

    def summary(self, pcap: bytes) -> dict:
        topo = self.topo
        return {
            "schema": SCHEMA + "/summary",
            "id": self.spec["id"],
            "packets": len(self.frames),
            "duration_s": round(self.frames[-1][0] - self.frames[0][0], 3) if self.frames else 0,
            "sha256": hashlib.sha256(pcap).hexdigest(),
            "segments": [
                {
                    "id": s["id"],
                    "cidr": s["cidr"],
                    "gateway": s["gateway"],
                    "gateway_ip": topo.hosts[s["gateway"]]["ip"],
                    "gateway_mac": topo.mac[s["gateway"]].hex(":"),
                }
                for s in topo.segments
            ],
            "hosts": [
                {
                    "id": hid,
                    "ip": h["ip"],
                    "mac": topo.mac[hid].hex(":"),
                    "name": h.get("name"),
                    "role": h["role"],
                    "segment": topo.segment_of[hid],
                }
                for hid, h in topo.hosts.items()
            ],
            "conversations": dict(sorted(self.convs.items())),
            "dns": self.dns,
            "http": self.http,
            "tls": self.tls,
        }


def render(spec: dict) -> tuple[bytes, dict]:
    """(pcap bytes, summary) for a valid spec. Deterministic: same spec, same bytes."""
    errs = validate(spec)
    if errs:
        raise SpecError("; ".join(errs))
    w = _Writer(spec)
    w.run()
    data = w.pcap()
    return data, w.summary(data)


def read_pcap(data: bytes) -> list[tuple[float, bytes]]:
    """Parse a classic little-endian pcap back into (timestamp, frame). For checks and tests."""
    magic, _, _, _, _, _, linktype = struct.unpack("<IHHiIII", data[:24])
    if magic != 0xA1B2C3D4 or linktype != 1:
        raise ValueError("not a little-endian Ethernet pcap")
    out, i = [], 24
    while i < len(data):
        sec, usec, incl, _orig = struct.unpack("<IIII", data[i : i + 16])
        out.append((sec + usec / 1e6, data[i + 16 : i + 16 + incl]))
        i += 16 + incl
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arc2.pcapgen", description="Render a synthetic teaching capture from a spec.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("spec", type=Path)
    r = sub.add_parser("render")
    r.add_argument("spec", type=Path)
    r.add_argument("out", type=Path)
    r.add_argument("--summary", type=Path)
    args = ap.parse_args(argv)
    try:
        spec = load(args.spec)
        if args.cmd == "check":
            errs = validate(spec)
            print(json.dumps({"ok": not errs, "errors": errs}, indent=2))
            return 0 if not errs else 1
        data, summary = render(spec)
    except SpecError as exc:
        print(json.dumps({"ok": False, "errors": str(exc).split("; ")}, indent=2))
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {
                "ok": True,
                "pcap": str(args.out),
                "sha256": summary["sha256"],
                "packets": summary["packets"],
                "summary": str(args.summary) if args.summary else None,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
