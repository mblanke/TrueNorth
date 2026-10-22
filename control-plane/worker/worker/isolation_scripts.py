"""The in-guest isolation probes (range certification S4, ADR 0013 §4), as guest steps.

Guest operations report only a process's exit code (``run_in_guest``), so each step runs
up to three probes at once and packs their outcomes into its exit code:
``64 + r0 + 4*r1 + 16*r2``, each ``r`` a 2-bit probe code (below). An exit code outside
64..127 (a crashed shell, a missing interpreter, a killed process) is no outcome at all,
and every probe of that step is then inconclusive. Nothing is ever read from the guest
but that number.

Probe codes, per kind (``decode`` returns them; range_isolation.classify grades them):

============  =========================================================================
``tcp``       0 connected, 1 refused, 2 other failure (no route, ICMP unreachable,
              name error), 3 timed out
``icmp``      0 echo reply, 1 no reply, 2 the probe could not run (no ping, not allowed)
``selftest``  0 a candidate (the zone's gateway, else a peer) answered ICMP, or TCP
              connected or refused; 1 nothing answered
``dns_int``   0 the internal name resolved through the range's DNS server, 1 it did not,
              3 no resolver tool in the guest
``dns_ext``   0 the external name resolved to an address outside the range,
              1 it did not resolve, 2 it resolved only to addresses inside the range (a
              simulated internet such as Greyspace), 3 no resolver tool
``route``     0 no hop outside the range answered a TTL-limited probe, 1 one did (or the
              target itself answered): the path crosses the range edge, 3 no ping
``v6route``   0 an IPv6 default route is present, 1 none, 3 could not tell
``v6global``  0 a global IPv6 address (2000::/3) is assigned, 1 none, 3 could not tell
============  =========================================================================

**No artifacts.** Linux probes run as ``/bin/bash -c <script>``, Windows ones as
``powershell.exe -NoProfile -NonInteractive -EncodedCommand``; neither writes a file
(no here-strings, no redirection but to ``/dev/null``, no transcript), so there is nothing
to clean up afterwards. Probes need no privilege: ``/dev/tcp``, ``ping``, ``getent`` and
``ip`` (Linux), ``TcpClient``, ``Ping``, ``Resolve-DnsName`` and ``Get-Net*`` (Windows).

**Injection.** Every host, name and CIDR is validated (``valid_host``, ``valid_name``)
before it gets here and is quoted again for the shell it lands in.
"""

from __future__ import annotations

import base64
import ipaddress
import re
import shlex
from dataclasses import dataclass, field

from .provisioners.base import GuestStep

BASH = "/bin/bash"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
PER_STEP = 3  # probes packed into one exit code
MARK = 64  # every packed exit code has this bit; anything else is no outcome
EVERY_CODE = tuple(range(256))  # run_in_guest stops at a step "not ok": none here is
HOPS = 4  # the TTL probe's longest path: zone gateway, (routers,) edge, beyond

KINDS = ("tcp", "icmp", "selftest", "dns_int", "dns_ext", "route", "v6route", "v6global")

_NAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*\.?$")


def valid_host(text: str) -> bool:
    """An IP literal or a DNS name, nothing a shell could read as more."""
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return valid_name(text)


def valid_name(text: str) -> bool:
    return bool(_NAME.match(text or ""))


def valid_cidr(text: str) -> bool:
    try:
        ipaddress.ip_network(text, strict=False)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class Probe:
    """One probe run from a zone's probe VM. ``args``: the kind's own arguments (a TCP
    target's host and port, a DNS server and name, the TTL probe's allowed hops)."""

    kind: str
    args: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown probe kind {self.kind!r}")
        for a in self.args:
            if not (valid_host(a) or valid_cidr(a) or a.isdigit()):
                raise ValueError(f"probe argument {a!r} is not a host, CIDR or port")


def chunks(probes: list[Probe]) -> list[list[Probe]]:
    return [probes[i:i + PER_STEP] for i in range(0, len(probes), PER_STEP)]


def decode(exit_code: int | None, count: int) -> list[int | None]:
    """The probe codes a step's exit code carries, or ``None`` for each when it carries none."""
    if exit_code is None or not (MARK <= exit_code < 2 * MARK) or not 1 <= count <= PER_STEP:
        return [None] * count
    packed = exit_code - MARK
    return [(packed >> (2 * i)) & 3 for i in range(count)]


# --------------------------------------------------------------------------- #
# Linux: bash, no files
# --------------------------------------------------------------------------- #

_BASH_LIB = r"""
T=%(timeout)d
tcp(){ local e r; e=$(timeout "$T" bash -c "exec 3<>/dev/tcp/$1/$2" 2>&1); r=$?
  [ $r -eq 0 ] && return 0; [ $r -eq 124 ] && return 3
  case "$e" in *[Rr]efused*) return 1;; esac; return 2; }
icmp(){ command -v ping >/dev/null 2>&1 || return 2
  timeout $((T+2)) ping -n -c 1 -W "$T" "$1" >/dev/null 2>&1; case $? in 0) return 0;; 1) return 1;; esac; return 2; }
selftest(){ local h; for h in "$@"; do icmp "$h" && return 0
  tcp "$h" 53; [ $? -le 1 ] && return 0; tcp "$h" 443; [ $? -le 1 ] && return 0; done; return 1; }
ip2n(){ local IFS=.; set -- $1; echo $(( ($1<<24)|($2<<16)|($3<<8)|$4 )); }
incidr(){ local a n b m; a=$(ip2n "$1"); n=$(ip2n "${2%%/*}"); b=${2##*/}
  m=$(( b==0 ? 0 : (0xFFFFFFFF<<(32-b)) & 0xFFFFFFFF )); [ $(( a & m )) -eq $(( n & m )) ]; }
inside(){ local ip=$1 c; shift; for c in "$@"; do case "$c" in *:*) continue;; esac; incidr "$ip" "$c" && return 0; done; return 1; }
dns_int(){ local o r; if command -v dig >/dev/null 2>&1; then
    o=$(timeout $((T+2)) dig +short +time="$T" +tries=1 "@$1" "$2" A 2>/dev/null); r=$?
  elif command -v nslookup >/dev/null 2>&1; then
    o=$(timeout $((T+2)) nslookup -timeout="$T" "$2" "$1" 2>/dev/null | sed -n '/^Name:/,$p'); r=$?
  elif command -v host >/dev/null 2>&1; then
    o=$(timeout $((T+2)) host -W "$T" -t A "$2" "$1" 2>/dev/null | sed -n 's/.* has address //p'); r=$?
  else return 3; fi
  [ $r -eq 0 ] && printf '%%s\n' "$o" | grep -Eq '([^0-9.]|^)[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' && return 0; return 1; }
dns_ext(){ local n=$1 o r ip out=0 any=0; shift; command -v getent >/dev/null 2>&1 || return 3
  o=$(timeout $((T+3)) getent ahostsv4 "$n" 2>/dev/null); r=$?
  [ $r -eq 0 ] || return 1
  for ip in $(printf '%%s\n' "$o" | sed -n 's/^\([0-9.]*\) .*/\1/p' | sort -u); do any=1
    inside "$ip" "$@" || out=1; done
  [ $any -eq 1 ] || return 1; [ $out -eq 1 ] && return 0; return 2; }
route(){ local d=$1 t o r h a ok; shift; command -v ping >/dev/null 2>&1 || return 3
  for t in %(hop_list)s; do o=$(timeout $((T+2)) ping -n -c 1 -W "$T" -t "$t" "$d" 2>&1); r=$?
    [ $r -eq 0 ] && return 1
    h=$(printf '%%s\n' "$o" | sed -n 's/^From \([0-9.]*\)[ :].*/\1/p' | head -n 1)
    if [ -z "$h" ]; then [ $r -eq 1 ] && continue; return 3; fi
    ok=1; for a in "$@"; do [ "$a" = "$h" ] && ok=0; done; [ $ok -eq 0 ] || return 1; done; return 0; }
v6route(){ command -v ip >/dev/null 2>&1 || return 3; local o; o=$(ip -6 route show default 2>/dev/null) || return 3
  [ -n "$o" ] && return 0; return 1; }
v6global(){ command -v ip >/dev/null 2>&1 || return 3; local o; o=$(ip -6 addr show scope global 2>/dev/null) || return 3
  printf '%%s\n' "$o" | grep -Eq 'inet6 [23][0-9a-fA-F]{0,3}:' && return 0; return 1; }
"""


def _bash_call(p: Probe) -> str:
    return " ".join([p.kind, *(shlex.quote(a) for a in p.args)])


def linux_script(probes: list[Probe], timeout: int) -> str:
    if not 1 <= len(probes) <= PER_STEP:
        raise ValueError("a step runs one to three probes")
    lines = [_BASH_LIB % {"timeout": max(1, int(timeout)), "hop_list": " ".join(str(t) for t in range(1, HOPS + 1))}]
    for i, p in enumerate(probes):  # concurrently: the step lasts as long as its slowest probe
        lines.append(f"{_bash_call(p)} & p{i}=$!")
    lines += [f"wait $p{i}; r{i}=$?" for i in range(len(probes))]
    # A probe function returns 0..3 by construction; anything else means the shell broke
    # (killed, a missing builtin), which must never pass as a code: exit outside 64..127.
    lines += [f"[ $r{i} -le 3 ] || exit 2" for i in range(len(probes))]
    packed = " + ".join(f"r{i}*{4 ** i}" for i in range(len(probes)))
    lines.append(f"exit $(( {MARK} + {packed} ))")
    return "\n".join(lines)


def linux_step(label: str, probes: list[Probe], timeout: int) -> GuestStep:
    return GuestStep(label, BASH, "-c " + shlex.quote(linux_script(probes, timeout)), EVERY_CODE)


# --------------------------------------------------------------------------- #
# Windows: PowerShell 5.1, -EncodedCommand, no files
# --------------------------------------------------------------------------- #

_PS_LIB = r"""
$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; $T = %(timeout_ms)d
function Tcp($h, $p) { $c = New-Object Net.Sockets.TcpClient
  try { if (-not $c.ConnectAsync($h, [int]$p).Wait($T)) { return 3 }; return 0 }
  catch { $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
    if ($e -is [Net.Sockets.SocketException] -and $e.SocketErrorCode -eq 'ConnectionRefused') { return 1 }; return 2 }
  finally { $c.Close() } }
function Icmp($h) { try { $r = (New-Object Net.NetworkInformation.Ping).Send($h, $T)
    if ($r.Status -eq 'Success') { return 0 }; return 1 } catch { return 2 } }
function Selftest([string[]]$hs) { foreach ($h in $hs) { if ((Icmp $h) -eq 0) { return 0 }
    if ((Tcp $h 53) -le 1) { return 0 }; if ((Tcp $h 443) -le 1) { return 0 } }; return 1 }
function InCidr($ip, $c) { $p = $c.Split('/'); $a = [Net.IPAddress]::Parse($ip).GetAddressBytes()
  $n = [Net.IPAddress]::Parse($p[0]).GetAddressBytes(); if ($a.Length -ne $n.Length) { return $false }; $b = [int]$p[1]
  for ($i = 0; $i -lt $a.Length; $i++) { $m = if ($b -ge 8) { 255 } elseif ($b -le 0) { 0 } else { (0xFF -shl (8 - $b)) -band 0xFF }
    if (($a[$i] -band $m) -ne ($n[$i] -band $m)) { return $false }; $b -= 8 }; return $true }
function Inside($ip, [string[]]$cs) { foreach ($c in $cs) { if (InCidr $ip $c) { return $true } }; return $false }
function HasResolver { return [bool](Get-Command Resolve-DnsName -ErrorAction SilentlyContinue) }
function DnsInt($s, $n) { if (-not (HasResolver)) { return 3 }
  try { $r = @(Resolve-DnsName -Name $n -Server $s -Type A -DnsOnly -NoHostsFile -QuickTimeout | Where-Object { $_.IPAddress })
    if ($r.Count) { return 0 }; return 1 } catch { return 1 } }
function DnsExt($n, [string[]]$cs) { if (-not (HasResolver)) { return 3 }
  try { $r = @(Resolve-DnsName -Name $n -Type A -DnsOnly -NoHostsFile -QuickTimeout | Where-Object { $_.IPAddress }) } catch { return 1 }
  if (-not $r.Count) { return 1 }; foreach ($x in $r) { if (-not (Inside $x.IPAddress $cs)) { return 0 } }; return 2 }
function Route($d, [string[]]$ok) { try { $p = New-Object Net.NetworkInformation.Ping; $buf = New-Object byte[] 32
    for ($t = 1; $t -le %(hops)d; $t++) { $r = $p.Send($d, $T, $buf, (New-Object Net.NetworkInformation.PingOptions($t, $true)))
      if ($r.Status -eq 'Success') { return 1 }
      if ($r.Address -and $r.Address.ToString() -ne '0.0.0.0' -and -not ($ok -contains $r.Address.ToString())) { return 1 } }
    return 0 } catch { return 3 } }
function V6route { if (-not (Get-Command Get-NetRoute -ErrorAction SilentlyContinue)) { return 3 }
  try { $r = @(Get-NetRoute -AddressFamily IPv6 -DestinationPrefix '::/0' -ErrorAction Stop)
    if ($r.Count) { return 0 }; return 1 } catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { return 1 }; return 3 } }
function V6global { try { $a = @(Get-NetIPAddress -AddressFamily IPv6 -ErrorAction Stop | Where-Object { $_.IPAddress -match '^[23][0-9a-fA-F]{0,3}:' })
    if ($a.Count) { return 0 }; return 1 } catch { return 3 } }
function Code($v) { $x = @($v); $n = $x[$x.Count - 1]; if ($n -is [int] -and $n -ge 0 -and $n -le 3) { return $n }; throw 'no outcome' }
"""

_PS_CALL = {"tcp": "Tcp", "icmp": "Icmp", "selftest": "Selftest", "dns_int": "DnsInt", "dns_ext": "DnsExt",
            "route": "Route", "v6route": "V6route", "v6global": "V6global"}


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _ps_call(p: Probe) -> str:
    q = [_ps_quote(a) for a in p.args]
    if p.kind == "selftest":
        return f"Selftest @({', '.join(q)})"
    if p.kind in ("dns_ext", "route"):  # first argument, then a list
        rest = f"@({', '.join(q[1:])})" if q[1:] else "@()"
        return f"{_PS_CALL[p.kind]} {q[0]} {rest}"
    if p.kind == "tcp":
        return f"Tcp {q[0]} {int(p.args[1])}"
    return " ".join([_PS_CALL[p.kind], *q])


def windows_script(probes: list[Probe], timeout: int) -> str:
    if not 1 <= len(probes) <= PER_STEP:
        raise ValueError("a step runs one to three probes")
    lines = [_PS_LIB % {"timeout_ms": max(1, int(timeout)) * 1000, "hops": HOPS}]
    lines += [f"$c{i} = Code ({_ps_call(p)})" for i, p in enumerate(probes)]
    packed = " + ".join(f"$c{i} * {4 ** i}" for i in range(len(probes)))
    lines.append(f"exit ({MARK} + {packed})")
    return "\n".join(lines)


def windows_step(label: str, probes: list[Probe], timeout: int) -> GuestStep:
    encoded = base64.b64encode(windows_script(probes, timeout).encode("utf-16-le")).decode()
    return GuestStep(label, POWERSHELL, f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}",
                     EVERY_CODE)


STEP_BUILDERS = {"linux": linux_step, "windows": windows_step}


def steps(family: str, probes: list[Probe], timeout: int) -> list[GuestStep]:
    """The guest steps that run ``probes`` on a ``family`` (linux | windows) probe VM."""
    build = STEP_BUILDERS[family]
    return [build(f"isolation-{n}", group, timeout) for n, group in enumerate(chunks(probes))]
