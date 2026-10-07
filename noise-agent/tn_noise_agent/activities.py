"""What a persona actually does on the wire.

Each activity takes the planned action and the agent config, does the real thing
against the target, and returns a small detail dict (or raises). Everything is
deliberately bounded — short timeouts, few hosts, few ports — because the point is a
believable background, not load.

v1 is the Linux set. Credentialed actions (IMAP login, Kerberos, SMB with an account)
need persona passwords from an org pack and arrive with the Windows agent; until then
those activities perform the unauthenticated part of the exchange, which still puts
the protocol on the wire.
"""

from __future__ import annotations

import ipaddress
import itertools
import random
import re
import smtplib
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass(frozen=True)
class AgentConfig:
    domain: str = "corp.local"
    timeout: float = 5.0
    max_scan_hosts: int = 32
    scan_ports: tuple[int, ...] = (22, 80, 135, 443, 445, 3389)
    # Never act against these (the controller, the management network). The agent is
    # dual-homed; a target must not become a way to reach the white-cell side.
    forbidden_hosts: frozenset[str] = frozenset()
    forbidden_nets: tuple[str, ...] = ()
    allow_loopback: bool = False  # tests only


class TargetRefusedError(ValueError):
    pass


def guard(host: str, cfg: AgentConfig) -> None:
    """Refuse a target that resolves anywhere an activity has no business going."""
    if host.lower() in cfg.forbidden_hosts:
        raise TargetRefusedError(f"{host} is forbidden")
    nets = [ipaddress.ip_network(n, strict=False) for n in cfg.forbidden_nets]
    for info in socket.getaddrinfo(host, None):
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if addr.is_loopback and cfg.allow_loopback:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
            raise TargetRefusedError(f"{host} resolves to {addr}")
        if any(addr in n for n in nets):
            raise TargetRefusedError(f"{host} resolves into a forbidden network")


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the same host: a compromised intranet server must
    not be able to bounce the agent somewhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            raise urllib.error.HTTPError(newurl, code, "cross-host redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameHostRedirects)


Activity = Callable[[dict, AgentConfig], dict]

SUBJECTS = (
    "Re: weekly sync",
    "Updated roster",
    "Leave request",
    "Fwd: training schedule",
    "Quick question",
    "Minutes from this morning",
    "Stores return",
    "Reminder: timesheets due Friday",
)
LINES = (
    "Can you have a look at this before end of day?",
    "Attached is the latest version, let me know if anything is missing.",
    "Thanks, that works for me.",
    "Following up on the thread below.",
    "I've moved the meeting to 1400.",
    "Please confirm numbers by tomorrow.",
)


def _host(target: str) -> str:
    if "://" in target:
        return urllib.parse.urlsplit(target).hostname or ""
    return target.rsplit(":", 1)[0] if target.count(":") == 1 else target


def _tcp(host: str, port: int, timeout: float, *, read: bool = False) -> bytes:
    with socket.create_connection((host, port), timeout=timeout) as s:
        if read:
            s.settimeout(timeout)
            try:
                return s.recv(256)
            except TimeoutError:
                return b""
    return b""


def dns_lookup(action: dict, cfg: AgentConfig) -> dict:
    infos = socket.getaddrinfo(action["target"], None)
    return {"addresses": sorted({i[4][0] for i in infos})[:4]}


_HREF = re.compile(rb'href="(/[^"#?]*)"')


def web_browse(action: dict, cfg: AgentConfig) -> dict:
    """Open a site, then follow a link or two on it, like somebody reading the intranet."""
    target = action["target"]
    base = target if target.startswith(("http://", "https://")) else f"http://{target}"
    fetched = []
    with _opener.open(base, timeout=cfg.timeout) as resp:
        page = resp.read(256 * 1024)
        fetched.append(resp.status)
    links = sorted(set(_HREF.findall(page)))
    for link in random.sample(links, k=min(2, len(links))):
        with _opener.open(base.rstrip("/") + link.decode(), timeout=cfg.timeout) as resp:
            resp.read(256 * 1024)
            fetched.append(resp.status)
    return {"pages": len(fetched)}


def email_send(action: dict, cfg: AgentConfig) -> dict:
    sender = f"{action.get('persona') or 'noreply'}@{cfg.domain}"
    to = (action.get("params") or {}).get("to") or sender
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, random.choice(SUBJECTS)
    msg.set_content("\n\n".join(random.sample(LINES, k=2)))
    with smtplib.SMTP(action["target"], 25, timeout=cfg.timeout) as smtp:
        smtp.send_message(msg)
    return {"to": to}


def email_read(action: dict, cfg: AgentConfig) -> dict:
    banner = _tcp(action["target"], 143, cfg.timeout, read=True)
    return {"banner": banner[:40].decode(errors="replace").strip()}


def file_share(action: dict, cfg: AgentConfig) -> dict:
    _tcp(action["target"], 445, cfg.timeout)
    return {"port": 445}


def ad_logon(action: dict, cfg: AgentConfig) -> dict:
    _tcp(action["target"], 88, cfg.timeout)
    return {"port": 88}


def ssh_admin(action: dict, cfg: AgentConfig) -> dict:
    banner = _tcp(action["target"], 22, cfg.timeout, read=True)
    return {"banner": banner[:40].decode(errors="replace").strip()}


def ntp_sync(action: dict, cfg: AgentConfig) -> dict:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(cfg.timeout)
        s.sendto(b"\x1b" + 47 * b"\0", (action["target"], 123))
        data, _ = s.recvfrom(48)
    secs = struct.unpack("!I", data[40:44])[0] - 2208988800
    return {"skew_s": round(secs - time.time(), 1)}


def admin_scan(action: dict, cfg: AgentConfig) -> dict:
    """An IT inventory sweep: connect to a few common ports across a small subnet."""
    net = ipaddress.ip_network(action["target"], strict=False)
    hosts = [str(h) for h in itertools.islice(net.hosts(), cfg.max_scan_hosts)]
    for h in hosts:
        guard(h, cfg)
    open_ports = 0
    for h in hosts:
        for p in cfg.scan_ports:
            try:
                _tcp(h, p, 0.3)
                open_ports += 1
            except OSError:
                pass
    return {"hosts": len(hosts), "open": open_ports}


def admin_remote_exec(action: dict, cfg: AgentConfig) -> dict:
    for port in (135, 445):
        _tcp(action["target"], port, cfg.timeout)
    return {"ports": [135, 445]}


def bad_password(action: dict, cfg: AgentConfig) -> dict:
    """A few failed authentications in a row, like somebody with caps lock on."""
    attempts = random.randint(2, 4)
    for _ in range(attempts):
        try:
            with smtplib.SMTP(action["target"], 25, timeout=cfg.timeout) as smtp:
                smtp.ehlo()
                if smtp.has_extn("auth"):
                    smtp.login(f"{action.get('persona')}@{cfg.domain}", "Winter2026!")
        except smtplib.SMTPException:
            pass
    return {"attempts": attempts}


def bulk_upload(action: dict, cfg: AgentConfig) -> dict:
    _tcp(action["target"], 445, cfg.timeout)
    return {"port": 445}


REGISTRY: dict[str, Activity] = {
    "dns_lookup": dns_lookup,
    "web_browse": web_browse,
    "email_send": email_send,
    "email_read": email_read,
    "file_share": file_share,
    "ad_logon": ad_logon,
    "ssh_admin": ssh_admin,
    "ntp_sync": ntp_sync,
    "admin_scan": admin_scan,
    "admin_remote_exec": admin_remote_exec,
    "bad_password": bad_password,
    "bulk_upload": bulk_upload,
}


def dry_run(action: dict, cfg: AgentConfig) -> dict:
    """Do nothing on the network; used by tests and the mock provisioner."""
    return {"dry_run": True}
