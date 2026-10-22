"""The in-guest isolation probes (range certification S4, ADR 0013 §4), as guest steps.

Guest operations report only a process's exit code (``run_in_guest``), so each step runs
up to two probes at once and packs their outcomes into its exit code: ``64 + r0 + 8*r1``,
each ``r`` a 3-bit probe code 0..6 (below). **7 is never a code**, so an exit code outside
64..125 decodes to no outcome at all: a crashed shell, a missing interpreter (126, 127),
a killed process (128 and up), an empty decode (0). Every probe of such a step is then
inconclusive. Nothing is ever read from the guest but that number.

Probe codes, per kind (``decode`` returns them; range_isolation.classify grades them).
``5`` is the same for every kind that can return it: **not measurable from this zone**,
because the probe VM is multi-homed and its route to the target leaves by another NIC, or
(DNS) its resolver cannot be shown to be reached through the zone's NIC.

============  =========================================================================
``tcp``       0 connected, 1 refused, 2 other failure (no route, ICMP unreachable,
              name error), 3 timed out, 5 not measurable
``icmp``      0 echo reply, 1 no reply, 2 the probe could not run (no ping, not
              allowed), 3 an "unreachable" answer from one of the range's own
              addresses (rejected at the edge), 4 an "unreachable" answer from an
              address outside the range (a site router answered), 5 not measurable
``selftest``  0 a candidate (the zone's gateway, else a peer) answered ICMP, or TCP
              connected or refused; 1 nothing answered
``dns_int``   0 the internal name resolved through the range's DNS server, 1 it did not,
              3 no resolver tool in the guest, 5 not measurable
``dns_ext``   0 the external name resolved to an address outside the range,
              1 it did not resolve, 2 it resolved only to addresses inside the range (a
              simulated internet such as Greyspace), 3 no resolver tool, 5 not measurable
``route``     0 no hop outside the range answered a TTL-limited probe, 1 one did (or the
              target itself answered): the path crosses the range edge, 3 no ping,
              5 not measurable
``v6route``   0 an IPv6 route other than link-local, multicast or loopback is present
              (ULA included), 1 none, 3 could not tell
``v6addr``    0 an IPv6 address other than link-local (fe80::/10) or ::1 is assigned
              (ULA fd00::/8 included), 1 none, 3 could not tell
============  =========================================================================

**Multi-homed probe VMs** (``Context.src``: the VM's address on the zone). The probes do
not bind a source; instead every probe that leaves the zone first asks the guest which
interface its route to the target uses (``ip route get``; Windows ``Find-NetRoute``) and
measures only if that is the interface holding ``src``, else returns 5. A single-homed VM
has no other way out, so nothing is checked.

**No files.** Linux probes run as ``/bin/bash -c 'eval "$(printf %s <base64> | base64
-d)"'``, Windows ones as ``powershell.exe -NoProfile -NonInteractive -EncodedCommand``;
neither writes a file (no here-strings, no redirection but to ``/dev/null``, no
transcript). The base64 only keeps the site's addresses from being plain text in a
process listing; it hides nothing from someone who decodes it. Side effects of the
guest-operations login itself are not the probes': Windows creates a user profile at an
account's first login, and a Linux PAM stack may create a home directory.
Probes need no privilege: ``/dev/tcp``, ``ping``, ``getent`` and ``ip`` (Linux),
``TcpClient``, ``Ping``, ``Resolve-DnsName`` and ``Get-Net*`` (Windows).

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
PER_STEP = 2  # probes packed into one exit code
BITS = 3  # per probe; the value 7 is never a code
MARK = 64  # every packed exit code has this bit; anything else is no outcome
NOT_MEASURABLE = 5
EVERY_CODE = tuple(range(256))  # run_in_guest stops at a step "not ok": none here is
HOPS = 4  # the TTL probe's longest path: zone gateway, (routers,) edge, beyond

KINDS = ("tcp", "icmp", "selftest", "dns_int", "dns_ext", "route", "v6route", "v6addr")

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


def _ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class Probe:
    """One probe run from a zone's probe VM. ``args``: the kind's own arguments (a TCP
    target's host and port, a DNS server and name, the TTL probe's destination)."""

    kind: str
    args: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown probe kind {self.kind!r}")
        for a in self.args:
            if not (valid_host(a) or valid_cidr(a) or a.isdigit()):
                raise ValueError(f"probe argument {a!r} is not a host, CIDR or port")


@dataclass(frozen=True)
class Context:
    """What every probe of one zone shares. ``src``: the probe VM's address on the zone when
    it is multi-homed (its probes must leave by that NIC), else "". ``range_addresses``: the
    range's own IPv4 space, as addresses or CIDRs (a hop or an "unreachable" from it has not
    left the range; range_isolation.compact_range keeps the list short). ``inside``: CIDRs
    an external name may resolve into without counting."""

    src: str = ""
    range_addresses: tuple[str, ...] = ()
    inside: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.src and not _ip(self.src):
            raise ValueError(f"source {self.src!r} is not an address")
        if not all(_ip(a) or valid_cidr(a) for a in self.range_addresses) or not all(valid_cidr(c) for c in self.inside):
            raise ValueError("range addresses and inside CIDRs must be addresses and CIDRs")


def chunks(probes: list[Probe]) -> list[list[Probe]]:
    return [probes[i:i + PER_STEP] for i in range(0, len(probes), PER_STEP)]


def pack(codes: list[int]) -> int:
    """The exit code a step with these probe codes ends with (what the guest scripts do)."""
    return MARK + sum(c << (BITS * i) for i, c in enumerate(codes))


def decode(exit_code: int | None, count: int) -> list[int | None]:
    """The probe codes a step's exit code carries, or ``None`` for each when it carries none:
    outside 64..127, a digit of 7, or a non-zero digit beyond the step's probes."""
    if exit_code is None or not (MARK <= exit_code < 2 * MARK) or not 1 <= count <= PER_STEP:
        return [None] * count
    packed = exit_code - MARK
    digits = [(packed >> (BITS * i)) & 7 for i in range(PER_STEP)]
    if any(d == 7 for d in digits) or any(digits[count:]):
        return [None] * count
    return digits[:count]


# --------------------------------------------------------------------------- #
# Linux: bash, no files
# --------------------------------------------------------------------------- #

_BASH_LIB = r"""
T=%(timeout)d; SRC=%(src)s; R=%(range)s; C=%(inside)s
inr(){ local a; for a in $R; do case "$a" in */*) incidr "$1" "$a" && return 0;; *) [ "$a" = "$1" ] && return 0;; esac
  done; return 1; }
devof(){ ip -o addr show to "$1" 2>/dev/null | sed -n 's/^[0-9]*: *\([^ ]*\) .*/\1/p' | head -n 1; }
# Multi-homed probe: the route for a packet *from* the zone address (source-aware, so policy
# routing is honoured) must use the zone NIC, and so must the route the unbound probe itself
# takes (the kernel picks its source from that route): either one leaving by another NIC
# means this zone's path is not what would be measured.
pinned(){ [ -z "$SRC" ] && return 0; command -v ip >/dev/null 2>&1 || return 1
  local d r u; d=$(devof "$SRC"); [ -n "$d" ] || return 1
  r=$(ip route get "$1" from "$SRC" 2>/dev/null) || return 1; u=$(ip route get "$1" 2>/dev/null) || return 1
  case " $r " in *" dev $d "*) ;; *) return 1;; esac
  case " $u " in *" dev $d "*) return 0;; esac; return 1; }
tcp(){ pinned "$1" || return 5; local e r; e=$(timeout "$T" bash -c "exec 3<>/dev/tcp/$1/$2" 2>&1); r=$?
  [ $r -eq 0 ] && return 0; [ $r -eq 124 ] && return 3
  case "$e" in *[Rr]efused*) return 1;; esac; return 2; }
# iputils' ICMP error lines: Destination Host/Net Unreachable, Destination Host/Net
# Prohibited, Packet filtered, Source Host Isolated, Time to live exceeded.
unreach(){ printf '%%s\n' "$1" | grep -Ei '^From [0-9.]+.*(unreachable|prohibited|filtered|isolated|exceeded)' \
  | sed -n 's/^From \([0-9.]*\).*/\1/p' | head -n 1; }
icmp(){ pinned "$1" || return 5; command -v ping >/dev/null 2>&1 || return 2
  local o r h; o=$(timeout $((T+2)) ping -n -c 1 -W "$T" "$1" 2>&1); r=$?
  [ $r -eq 0 ] && return 0; h=$(unreach "$o")
  if [ -n "$h" ]; then inr "$h" && return 3; return 4; fi
  [ $r -eq 1 ] && return 1; return 2; }
selftest(){ local h SRC=; for h in "$@"; do icmp "$h"; [ $? -eq 0 ] && return 0
  tcp "$h" 53; [ $? -le 1 ] && return 0; tcp "$h" 443; [ $? -le 1 ] && return 0; done; return 1; }
ip2n(){ local IFS=.; set -- $1; echo $(( ($1<<24)|($2<<16)|($3<<8)|$4 )); }
incidr(){ local a n b m; a=$(ip2n "$1"); n=$(ip2n "${2%%/*}"); b=${2##*/}
  m=$(( b==0 ? 0 : (0xFFFFFFFF<<(32-b)) & 0xFFFFFFFF )); [ $(( a & m )) -eq $(( n & m )) ]; }
inside(){ local c; for c in $C; do case "$c" in *:*) continue;; esac; incidr "$1" "$c" && return 0; done; return 1; }
dns_int(){ pinned "$1" || return 5; local o r; if command -v dig >/dev/null 2>&1; then
    o=$(timeout $((T+2)) dig +short +time="$T" +tries=1 "@$1" "$2" A 2>/dev/null); r=$?
  elif command -v nslookup >/dev/null 2>&1; then
    o=$(timeout $((T+2)) nslookup -timeout="$T" "$2" "$1" 2>/dev/null | sed -n '/^Name:/,$p'); r=$?
  elif command -v host >/dev/null 2>&1; then
    o=$(timeout $((T+2)) host -W "$T" -t A "$2" "$1" 2>/dev/null | sed -n 's/.* has address //p'); r=$?
  else return 3; fi
  [ $r -eq 0 ] && printf '%%s\n' "$o" | grep -Eq '([^0-9.]|^)[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' && return 0; return 1; }
dns_ext(){ local o r ip s out=0 any=0; command -v getent >/dev/null 2>&1 || return 3
  if [ -n "$SRC" ]; then for s in $(sed -n 's/^nameserver[[:space:]]*\([^[:space:]]*\).*/\1/p' /etc/resolv.conf 2>/dev/null)
    do any=1; pinned "$s" || return 5; done; [ $any -eq 1 ] || return 5; any=0; fi
  o=$(timeout $((T+3)) getent ahostsv4 "$1" 2>/dev/null); r=$?
  [ $r -eq 0 ] || return 1
  for ip in $(printf '%%s\n' "$o" | sed -n 's/^\([0-9.]*\) .*/\1/p' | sort -u); do any=1
    inside "$ip" || out=1; done
  [ $any -eq 1 ] || return 1; [ $out -eq 1 ] && return 0; return 2; }
# Hop 1 must answer: silence there is not proof (a ping that never prints Time Exceeded,
# busybox's, or a first hop that filters before forwarding, as pfSense does).
route(){ pinned "$1" || return 5; local t o r h; command -v ping >/dev/null 2>&1 || return 3
  for t in %(hop_list)s; do o=$(timeout $((T+2)) ping -n -c 1 -W "$T" -t "$t" "$1" 2>&1); r=$?
    [ $r -eq 0 ] && return 1
    h=$(printf '%%s\n' "$o" | sed -n 's/^From \([0-9.]*\)[ :].*/\1/p' | head -n 1)
    if [ -z "$h" ]; then [ "$t" -eq 1 ] && return 3; [ $r -eq 1 ] && continue; return 3; fi
    inr "$h" || return 1; done; return 0; }
v6route(){ command -v ip >/dev/null 2>&1 || return 3; local o; o=$(ip -6 route show table all 2>/dev/null) || return 3
  printf '%%s\n' "$o" | grep -Eiv '^(local|multicast|anycast|broadcast|unreachable|prohibit|blackhole|throw|nat) |^(fe80|ff[0-9a-f]{2}):|^::1 |^[[:space:]]*$' \
    | grep -q . && return 0; return 1; }
v6addr(){ command -v ip >/dev/null 2>&1 || return 3; local o; o=$(ip -6 addr show 2>/dev/null) || return 3
  printf '%%s\n' "$o" | sed -n 's/^ *inet6 \([0-9a-fA-F:]*\)\/.*/\1/p' | grep -Eiv '^(fe[89ab][0-9a-f]:|::1$)' \
    | grep -q . && return 0; return 1; }
"""


def _bash_call(p: Probe) -> str:
    return " ".join([p.kind, *(shlex.quote(a) for a in p.args)])


def _words(items) -> str:
    return shlex.quote(" ".join(items))


def linux_script(probes: list[Probe], timeout: int, ctx: Context = Context()) -> str:
    if not 1 <= len(probes) <= PER_STEP:
        raise ValueError("a step runs one or two probes")
    lines = [_BASH_LIB % {"timeout": max(1, int(timeout)), "hop_list": " ".join(str(t) for t in range(1, HOPS + 1)),
                          "src": shlex.quote(ctx.src), "range": _words(ctx.range_addresses),
                          "inside": _words(ctx.inside)}]
    for i, p in enumerate(probes):  # concurrently: the step lasts as long as its slowest probe
        lines.append(f"{_bash_call(p)} & p{i}=$!")
    lines += [f"wait $p{i}; r{i}=$?" for i in range(len(probes))]
    # A probe function returns 0..6 by construction; anything else means the shell broke
    # (killed, a missing builtin), which must never pass as a code: exit outside 64..127.
    lines += [f"[ $r{i} -le 6 ] || exit 2" for i in range(len(probes))]
    packed = " + ".join(f"r{i}*{1 << (BITS * i)}" for i in range(len(probes)))
    lines.append(f"exit $(( {MARK} + {packed} ))")
    return "\n".join(lines)


def linux_step(label: str, probes: list[Probe], timeout: int, ctx: Context = Context()) -> GuestStep:
    blob = base64.b64encode(linux_script(probes, timeout, ctx).encode()).decode()
    return GuestStep(label, BASH, "-c " + shlex.quote(f'eval "$(printf %s {blob} | base64 -d)"'), EVERY_CODE)


# --------------------------------------------------------------------------- #
# Windows: PowerShell 5.1, -EncodedCommand, no files
# --------------------------------------------------------------------------- #

_PS_LIB = r"""
$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; $T = %(timeout_ms)d
$SRC = %(src)s; $R = @(%(range)s); $C = @(%(inside)s)
# Multi-homed probe: Windows sends from the interface its route picks (strong host model, no
# source-based policy routing by default), so the route Find-NetRoute picks for the target must
# be on the interface holding the zone address.
function Pinned($h) { if (-not $SRC) { return $true }
  try { $i = (Get-NetIPAddress -IPAddress $SRC -ErrorAction Stop | Select-Object -First 1).InterfaceIndex
    $used = @(Find-NetRoute -RemoteIPAddress $h -ErrorAction Stop | ForEach-Object { $_.InterfaceIndex } | Select-Object -Unique)
    return ($used.Count -eq 1 -and $used[0] -eq $i) } catch { return $false } }
function Tcp($h, $p) { if (-not (Pinned $h)) { return 5 }; $c = New-Object Net.Sockets.TcpClient
  try { if (-not $c.ConnectAsync($h, [int]$p).Wait($T)) { return 3 }; return 0 }
  catch { $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
    if ($e -is [Net.Sockets.SocketException] -and $e.SocketErrorCode -eq 'ConnectionRefused') { return 1 }; return 2 }
  finally { $c.Close() } }
function Icmp($h, $pin = $true) { if ($pin -and -not (Pinned $h)) { return 5 }
  try { $r = (New-Object Net.NetworkInformation.Ping).Send($h, $T)
    if ($r.Status -eq 'Success') { return 0 }
    if ([string]$r.Status -match 'Unreachable|Prohibited|TtlExpired|TimeExceeded|Isolated' -and $r.Address -and
        $r.Address.ToString() -ne '0.0.0.0') { if (InR $r.Address.ToString()) { return 3 }; return 4 }
    return 1 } catch { return 2 } }
function Selftest([string[]]$hs) { $SRC = ''; foreach ($h in $hs) { if ((Icmp $h $false) -eq 0) { return 0 }
    if ((Tcp $h 53) -le 1) { return 0 }; if ((Tcp $h 443) -le 1) { return 0 } }; return 1 }
function InCidr($ip, $c) { $p = $c.Split('/'); $a = [Net.IPAddress]::Parse($ip).GetAddressBytes()
  $n = [Net.IPAddress]::Parse($p[0]).GetAddressBytes(); if ($a.Length -ne $n.Length) { return $false }; $b = [int]$p[1]
  for ($i = 0; $i -lt $a.Length; $i++) { $m = if ($b -ge 8) { 255 } elseif ($b -le 0) { 0 } else { (0xFF -shl (8 - $b)) -band 0xFF }
    if (($a[$i] -band $m) -ne ($n[$i] -band $m)) { return $false }; $b -= 8 }; return $true }
function InR($ip) { foreach ($x in $R) { if ($x -like '*/*') { if (InCidr $ip $x) { return $true } }
    elseif ($x -eq $ip) { return $true } }; return $false }
function Inside($ip) { foreach ($c in $C) { if (InCidr $ip $c) { return $true } }; return $false }
function HasResolver { return [bool](Get-Command Resolve-DnsName -ErrorAction SilentlyContinue) }
function DnsInt($s, $n) { if (-not (Pinned $s)) { return 5 }; if (-not (HasResolver)) { return 3 }
  try { $r = @(Resolve-DnsName -Name $n -Server $s -Type A -DnsOnly -NoHostsFile -QuickTimeout | Where-Object { $_.IPAddress })
    if ($r.Count) { return 0 }; return 1 } catch { return 1 } }
function DnsExt($n) { if ($SRC) { return 5 }; if (-not (HasResolver)) { return 3 }
  try { $r = @(Resolve-DnsName -Name $n -Type A -DnsOnly -NoHostsFile -QuickTimeout | Where-Object { $_.IPAddress }) } catch { return 1 }
  if (-not $r.Count) { return 1 }; foreach ($x in $r) { if (-not (Inside $x.IPAddress)) { return 0 } }; return 2 }
function Route($d) { if (-not (Pinned $d)) { return 5 }
  try { $p = New-Object Net.NetworkInformation.Ping; $buf = New-Object byte[] 32
    for ($t = 1; $t -le %(hops)d; $t++) { $r = $p.Send($d, $T, $buf, (New-Object Net.NetworkInformation.PingOptions($t, $true)))
      if ($r.Status -eq 'Success') { return 1 }
      $silent = -not $r.Address -or $r.Address.ToString() -eq '0.0.0.0'
      if ($silent -and $t -eq 1) { return 3 }  # hop 1 must answer: silence is not proof
      if (-not $silent -and -not (InR $r.Address.ToString())) { return 1 } }
    return 0 } catch { return 3 } }
function V6route { if (-not (Get-Command Get-NetRoute -ErrorAction SilentlyContinue)) { return 3 }
  try { $r = @(Get-NetRoute -AddressFamily IPv6 -ErrorAction Stop | Where-Object {
      $_.DestinationPrefix -notmatch '^(fe80|ff[0-9a-f]{2}):' -and $_.DestinationPrefix -ne '::1/128' })
    if ($r.Count) { return 0 }; return 1 } catch { return 3 } }
function V6addr { if (-not (Get-Command Get-NetIPAddress -ErrorAction SilentlyContinue)) { return 3 }
  try { $a = @(Get-NetIPAddress -AddressFamily IPv6 -ErrorAction Stop | Where-Object { $_.IPAddress -notmatch '^(fe[89ab][0-9a-f]:|::1$)' })
    if ($a.Count) { return 0 }; return 1 } catch { return 3 } }
function Code($v) { $x = @($v); $n = $x[$x.Count - 1]; if ($n -is [int] -and $n -ge 0 -and $n -le 6) { return $n }; throw 'no outcome' }
"""

_PS_CALL = {"tcp": "Tcp", "icmp": "Icmp", "selftest": "Selftest", "dns_int": "DnsInt", "dns_ext": "DnsExt",
            "route": "Route", "v6route": "V6route", "v6addr": "V6addr"}


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _ps_call(p: Probe) -> str:
    q = [_ps_quote(a) for a in p.args]
    if p.kind == "selftest":
        return f"Selftest @({', '.join(q)})"
    if p.kind == "tcp":
        return f"Tcp {q[0]} {int(p.args[1])}"
    return " ".join([_PS_CALL[p.kind], *q])


def windows_script(probes: list[Probe], timeout: int, ctx: Context = Context()) -> str:
    if not 1 <= len(probes) <= PER_STEP:
        raise ValueError("a step runs one or two probes")
    lines = [_PS_LIB % {"timeout_ms": max(1, int(timeout)) * 1000, "hops": HOPS, "src": _ps_quote(ctx.src),
                        "range": ", ".join(map(_ps_quote, ctx.range_addresses)),
                        "inside": ", ".join(map(_ps_quote, ctx.inside))}]
    lines += [f"$c{i} = Code ({_ps_call(p)})" for i, p in enumerate(probes)]
    packed = " + ".join(f"$c{i} * {1 << (BITS * i)}" for i in range(len(probes)))
    lines.append(f"exit ({MARK} + {packed})")
    return "\n".join(lines)


def windows_step(label: str, probes: list[Probe], timeout: int, ctx: Context = Context()) -> GuestStep:
    encoded = base64.b64encode(windows_script(probes, timeout, ctx).encode("utf-16-le")).decode()
    return GuestStep(label, POWERSHELL, f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}",
                     EVERY_CODE)


STEP_BUILDERS = {"linux": linux_step, "windows": windows_step}


# Windows' CreateProcess takes a command line of at most 32767 characters; keep clear of it.
MAX_WINDOWS_ARGS = 30_000
NEEDS_RANGE = frozenset({"icmp", "route"})  # read $R
NEEDS_INSIDE = frozenset({"dns_ext"})  # read $C


def _ctx_for(group: list[Probe], ctx: Context) -> Context:
    """Only what the step's probes read, so a large range's lists ride only on the steps
    that need them."""
    return Context(ctx.src, ctx.range_addresses if any(p.kind in NEEDS_RANGE for p in group) else (),
                   ctx.inside if any(p.kind in NEEDS_INSIDE for p in group) else ())


def groups(family: str, probes: list[Probe], timeout: int, ctx: Context = Context()
           ) -> list[tuple[GuestStep, list[Probe]]]:
    """(guest step, the probes it runs, in order) covering ``probes`` on a ``family`` (linux |
    windows) probe VM. A Windows pair whose command line would pass MAX_WINDOWS_ARGS runs
    as two steps. A single probe still too long is sent anyway: Windows refuses to start it,
    the step fails, and its probe is inconclusive (never a pass)."""
    build = STEP_BUILDERS[family]
    out: list[tuple[GuestStep, list[Probe]]] = []
    for group in chunks(probes):
        parts = [group]
        if family == "windows" and len(group) > 1 and \
                len(build("x", group, timeout, _ctx_for(group, ctx)).arguments) > MAX_WINDOWS_ARGS:
            parts = [[p] for p in group]
        for part in parts:
            out.append((build(f"isolation-{len(out)}", part, timeout, _ctx_for(part, ctx)), part))
    return out


def steps(family: str, probes: list[Probe], timeout: int, ctx: Context = Context()) -> list[GuestStep]:
    """The guest steps that run ``probes`` on a ``family`` (linux | windows) probe VM."""
    return [step for step, _ in groups(family, probes, timeout, ctx)]
