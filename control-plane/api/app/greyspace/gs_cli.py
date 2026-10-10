#!/usr/bin/env python3
"""gs: operate a rendered Greyspace stack (ADR 0007). Rendered into every stack as ``bin/gs``.

    gs up [--build] [--probe]       start the stack (gs-core: also the host routes and forwarding)
    gs down                         stop it and remove its volumes
    gs health [--json]              is every service up, and do the web farm, DNS and mail answer
    gs crumb plant --b64 DATA       plant breadcrumbs (``--file F`` reads the JSON from a file)
    gs crumb remove --exercise E [--id ID]
    gs crumb list [--exercise E]
    gs crumb check --exercise E     is every planted breadcrumb still in place and served
    gs corpus verify DIR            manifest + every checksum of a corpus directory
    gs corpus mount --source SRV:/VOL [--dir DIR]   NFS, read-only (gs-core)

Breadcrumbs (plan slice 4) never touch the corpus: web files go to the range's overlay
volume (served in front of the corpus), DNS records to the authoritative zone files
(CoreDNS reloads them), threat-feed entries to ``intel.gs-infra.net/feed.txt`` in the
overlay. A payload is ``{"exercise": "<id>", "crumbs": [...]}``; each crumb has an ``id``
and a ``kind``:

* ``web``          ``site``, ``path``, ``content_b64``
* ``dns``          ``name``, ``type`` (TXT or A), ``value``
* ``threat_feed``  ``indicator``, ``type`` (domain, ip, url, sha256), ``note``

What was planted is kept in ``state/crumbs.json`` so ``remove`` takes out exactly that.
Standard library only; the stack directory is this file's grandparent (or ``GS_DIR``),
the Compose project is ``gs.json``'s (or ``GS_PROJECT``).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

STACK = Path(os.environ.get("GS_DIR") or Path(__file__).resolve().parent.parent)
_SERIAL = re.compile(r"^(@ IN SOA \S+ \S+ )(\d+)( .*)$", re.M)
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_TXT_SAFE = re.compile(r'^[^"\\\n\r;]{1,255}$')
MAX_CRUMB_BYTES = 64 * 1024


class GsError(RuntimeError):
    pass


def conf() -> dict:
    return json.loads((STACK / "gs.json").read_text(encoding="utf-8"))


def compose(*args: str, check: bool = True, stdin: bytes | None = None, capture: bool = True) -> subprocess.CompletedProcess:
    cfg = conf()
    cmd = ["docker", "compose", "-p", os.environ.get("GS_PROJECT") or cfg["project"], "-f", str(STACK / "compose.yaml"),
           *args]
    res = subprocess.run(cmd, input=stdin, capture_output=capture, check=False)
    if check and res.returncode != 0:
        err = (res.stderr or b"").decode("utf-8", "replace").strip()[-400:]
        raise GsError(f"{' '.join(args[:2])}: exit {res.returncode}: {err}")
    return res


def in_webfarm(script: str, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return compose("exec", "-T", "webfarm", "sh", "-c", script, stdin=stdin, check=check)


def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, check=check)


# ── up / down ───────────────────────────────────────────────────────────────
def host_network(cfg: dict) -> None:
    """gs-core: forward between the range NIC and the routed bridge, and send the ISP
    prefixes through the first ISP router (more specific than the bridge's own subnet)."""
    sh("sysctl", "-qw", "net.ipv4.ip_forward=1")
    for prefix in cfg["isp_prefixes"]:
        sh("ip", "route", "replace", prefix, "via", cfg["first_router"])
    sh("ip", "route", "replace", cfg["infrastructure"], "dev", cfg["bridge"])
    for rule in (["-i", cfg["bridge"]], ["-o", cfg["bridge"]]):
        if sh("iptables", "-C", "DOCKER-USER", *rule, "-j", "ACCEPT", check=False).returncode != 0:
            sh("iptables", "-I", "DOCKER-USER", *rule, "-j", "ACCEPT")


def cmd_up(args) -> int:
    cfg = conf()
    extra = ["--build"] if args.build else []
    profile = ["--profile", "probe"] if args.probe else []
    compose(*profile, "up", "-d", "--wait", "--wait-timeout", "240", *extra, capture=False)
    if cfg.get("host"):
        host_network(cfg)
    feed = f"/srv/overlay/sites/{cfg['intel_site']}/feed.txt"
    in_webfarm(f"mkdir -p $(dirname {feed}) && [ -s {feed} ] || "
               f"printf '# Greyspace threat-intel feed: type,indicator,note\\n' > {feed}")
    print(json.dumps({"up": True, "project": os.environ.get("GS_PROJECT") or cfg["project"]}))
    return 0


def cmd_down(args) -> int:
    compose("--profile", "probe", "down", "-v", "--remove-orphans", check=False, capture=False)
    ledger = STACK / "state" / "crumbs.json"
    if ledger.exists():
        ledger.unlink()
    return 0


# ── health ──────────────────────────────────────────────────────────────────
def dns_query(server: str, name: str, timeout: float = 3.0) -> list[str]:
    """A records for ``name`` from ``server`` (one UDP query, no recursion of our own)."""
    qid = int(time.time() * 1000) & 0xFFFF
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(q, (server, 53))
        data, _ = s.recvfrom(4096)
    _, flags, qd, an, _, _ = struct.unpack(">HHHHHH", data[:12])
    if flags & 0xF:
        return []
    pos = 12
    for _ in range(qd):
        while data[pos]:
            pos += data[pos] + 1
        pos += 5
    out = []
    for _ in range(an):
        if data[pos] & 0xC0 == 0xC0:
            pos += 2
        else:
            while data[pos]:
                pos += data[pos] + 1
            pos += 1
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:
            out.append(socket.inet_ntoa(data[pos:pos + 4]))
        pos += rdlen
    return out


def running_services() -> set[str]:
    res = compose("ps", "--format", "json", check=False)
    out = set()
    for line in (res.stdout or b"").decode().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows = json.loads(line)
        except ValueError:
            continue
        for row in rows if isinstance(rows, list) else [rows]:
            if row.get("State") == "running":
                out.add(row.get("Service"))
    return out


def cmd_health(args) -> int:
    cfg = conf()
    checks: list[dict] = []
    running = running_services()
    missing = sorted(set(cfg["services"]) - running)
    checks.append({"check": "services", "ok": not missing, "detail": f"missing: {missing}" if missing else "all running"})
    site = cfg.get("probe_site")
    if site:
        res = in_webfarm(f"wget -q -O - --header 'Host: {site}' http://127.0.0.1/ | head -c 2000", check=False)
        ok = res.returncode == 0 and bool(res.stdout.strip())
        checks.append({"check": "web", "ok": ok, "detail": f"http://{site}/ from the web farm"})
    # From inside the stack's network (the web farm's busybox), so the check means the same
    # on a gs-core VM and on a test host that does not route to the stack.
    if site:
        res = in_webfarm(f"nslookup {shlex.quote(site)} {cfg['resolver']}", check=False)
        out = res.stdout.decode("utf-8", "replace")
        addrs = re.findall(r"Address:\s*(\d+\.\d+\.\d+\.\d+)", out.split("Name:", 1)[-1]) if "Name:" in out else []
        checks.append({"check": "dns", "ok": bool(addrs), "detail": f"{site} -> {addrs}"})
    res = in_webfarm(f"echo QUIT | nc -w 5 {cfg['mail']} 25 | head -1", check=False)
    banner = res.stdout.decode("utf-8", "replace").strip()
    checks.append({"check": "smtp", "ok": banner.startswith("220"), "detail": banner[:80] or "no banner"})
    if cfg.get("host"):  # gs-core: the stack must also answer from the VM itself (range side)
        try:
            got = dns_query(cfg["resolver"], site) if site else []
            checks.append({"check": "dns-from-host", "ok": bool(got), "detail": f"{site} -> {got}"})
        except OSError as exc:
            checks.append({"check": "dns-from-host", "ok": False, "detail": str(exc)})
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}, indent=2 if not args.json else None))
    return 0 if ok else 1


# ── breadcrumbs ─────────────────────────────────────────────────────────────
def _ledger_path() -> Path:
    return STACK / "state" / "crumbs.json"


def load_ledger() -> list[dict]:
    p = _ledger_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def save_ledger(rows: list[dict]) -> None:
    p = _ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def _marker(exercise: str, cid: str) -> str:
    return f"gs-crumb:{exercise}:{cid}"


def _zone_file(name: str) -> Path:
    zone = ".".join(name.split(".")[-2:])
    path = STACK / "dns" / "auth" / "zones" / f"db.{zone}"
    if not path.exists():
        raise GsError(f"{name}: no Greyspace zone {zone} (only names in the corpus's zones can carry records)")
    return path


def _bump_and_write(path: Path, text: str) -> None:
    text = _SERIAL.sub(lambda m: f"{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}", text, count=1)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(path)


def _web_target(cfg: dict, crumb: dict) -> tuple[str, str]:
    site, path = str(crumb.get("site") or ""), str(crumb.get("path") or "")
    root = cfg["sites"].get(site) or (cfg["intel_site"] == site and f"sites/{site}")
    if not root:
        raise GsError(f"web crumb {crumb.get('id')}: {site!r} is not a site of this stack")
    if not _SAFE_PATH.match(path) or ".." in path.split("/") or path.endswith("/"):
        raise GsError(f"web crumb {crumb.get('id')}: path {path!r} must be an absolute file path (a-z 0-9 . _ ~ / -)")
    return site, f"/srv/overlay/{root}{path}"


def plant(payload: dict) -> list[dict]:
    cfg = conf()
    exercise = str(payload.get("exercise") or "")
    if not _SAFE_ID.match(exercise):
        raise GsError("payload.exercise must be an id (letters, digits, . _ : -)")
    crumbs = payload.get("crumbs")
    if not isinstance(crumbs, list) or not crumbs:
        raise GsError("payload.crumbs must be a non-empty list")
    ledger = [r for r in load_ledger()]
    planted = []
    for crumb in crumbs:
        cid = str(crumb.get("id") or "")
        if not _SAFE_ID.match(cid):
            raise GsError(f"crumb id {cid!r} must be letters, digits, . _ : -")
        kind = crumb.get("kind")
        mark = _marker(exercise, cid)
        # Re-planting the same crumb replaces it.
        for old in [r for r in ledger if r["exercise"] == exercise and r["id"] == cid]:
            _remove_one(cfg, old)
            ledger.remove(old)
        if kind == "web":
            site, target = _web_target(cfg, crumb)
            data = base64.b64decode(str(crumb.get("content_b64") or ""), validate=True)
            if len(data) > MAX_CRUMB_BYTES:
                raise GsError(f"web crumb {cid}: {len(data)} bytes is over {MAX_CRUMB_BYTES}")
            q = shlex.quote(target)
            in_webfarm(f"mkdir -p $(dirname {q}) && cat > {q}", stdin=data)
            row = {"site": site, "path": crumb["path"], "target": target, "sha256": hashlib.sha256(data).hexdigest()}
        elif kind == "dns":
            name, rtype, value = str(crumb.get("name") or "").lower(), str(crumb.get("type") or "TXT").upper(), str(
                crumb.get("value") or "")
            if rtype not in ("TXT", "A"):
                raise GsError(f"dns crumb {cid}: type must be TXT or A")
            if rtype == "A":
                socket.inet_aton(value)
            elif not _TXT_SAFE.match(value):
                raise GsError(f"dns crumb {cid}: TXT value must be 1-255 characters without quotes, ; or newlines")
            zone_path = _zone_file(name)
            zone = zone_path.name.removeprefix("db.")
            owner = "@" if name == zone else name[: -(len(zone) + 1)]
            rdata = f'"{value}"' if rtype == "TXT" else value
            text = zone_path.read_text(encoding="utf-8").rstrip("\n") + f"\n{owner} IN {rtype} {rdata} ; {mark}\n"
            _bump_and_write(zone_path, text)
            row = {"name": name, "type": rtype, "value": value, "zone_file": str(zone_path.relative_to(STACK))}
        elif kind == "threat_feed":
            indicator, itype, note = (str(crumb.get(k) or "") for k in ("indicator", "type", "note"))
            if itype not in ("domain", "ip", "url", "sha256") or not indicator or any(
                    c in indicator + note for c in "\n\r,#'"):
                raise GsError(f"threat_feed crumb {cid}: type domain|ip|url|sha256 and a single-line indicator")
            feed = f"/srv/overlay/sites/{cfg['intel_site']}/feed.txt"
            line = f"{itype},{indicator},{note} # {mark}\n"
            in_webfarm(f"mkdir -p $(dirname {feed}) && cat >> {feed}", stdin=line.encode())
            row = {"feed": feed, "indicator": indicator, "type": itype}
        else:
            raise GsError(f"crumb {cid}: kind must be web, dns or threat_feed")
        entry = {"exercise": exercise, "id": cid, "kind": kind, "marker": mark,
                 "planted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **row}
        ledger.append(entry)
        planted.append(entry)
        save_ledger(ledger)
    return planted


def _remove_one(cfg: dict, row: dict) -> None:
    if row["kind"] == "web":
        in_webfarm(f"rm -f {shlex.quote(row['target'])}", check=False)
    elif row["kind"] == "dns":
        path = STACK / row["zone_file"]
        if path.exists():
            kept = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.endswith(f"; {row['marker']}")]
            _bump_and_write(path, "\n".join(kept) + "\n")
    elif row["kind"] == "threat_feed":
        in_webfarm(f"sed -i {shlex.quote('/' + re.escape(row['marker']) + '$/d')} {shlex.quote(row['feed'])}",
                   check=False)


def remove(exercise: str, cid: str | None = None) -> list[dict]:
    cfg = conf()
    ledger = load_ledger()
    gone = [r for r in ledger if r["exercise"] == exercise and (cid is None or r["id"] == cid)]
    for row in gone:
        _remove_one(cfg, row)
    save_ledger([r for r in ledger if r not in gone])
    return gone


def check(exercise: str) -> list[dict]:
    out = []
    for row in [r for r in load_ledger() if r["exercise"] == exercise]:
        if row["kind"] == "web":
            res = in_webfarm(f"wget -q -O - --header {shlex.quote('Host: ' + row['site'])} "
                             f"{shlex.quote('http://127.0.0.1' + row['path'])}", check=False)
            ok = res.returncode == 0 and hashlib.sha256(res.stdout).hexdigest() == row["sha256"]
        elif row["kind"] == "dns":
            path = STACK / row["zone_file"]
            ok = path.exists() and any(ln.endswith(f"; {row['marker']}") for ln in path.read_text().splitlines())
        else:
            res = in_webfarm(f"grep -c {shlex.quote(row['marker'])} {shlex.quote(row['feed'])}", check=False)
            ok = res.returncode == 0
        out.append({"id": row["id"], "kind": row["kind"], "ok": ok})
    return out


def _payload(args) -> dict:
    raw = Path(args.file).read_bytes() if args.file else base64.b64decode(args.b64)
    return json.loads(raw.decode("utf-8"))


def cmd_crumb(args) -> int:
    if args.op == "plant":
        print(json.dumps({"planted": plant(_payload(args))}))
        return 0
    if args.op == "remove":
        print(json.dumps({"removed": remove(args.exercise, args.id)}))
        return 0
    if args.op == "list":
        rows = [r for r in load_ledger() if not args.exercise or r["exercise"] == args.exercise]
        print(json.dumps({"crumbs": rows}, indent=2))
        return 0
    results = check(args.exercise)
    print(json.dumps({"checked": results}))
    return 0 if results and all(r["ok"] for r in results) else 1


# ── corpus ──────────────────────────────────────────────────────────────────
def cmd_corpus(args) -> int:
    if args.op == "mount":
        target = Path(args.dir or conf().get("corpus_dir") or "/srv/greyspace/corpus")
        target.mkdir(parents=True, exist_ok=True)
        if sh("mountpoint", "-q", str(target), check=False).returncode == 0:
            print(json.dumps({"mounted": str(target), "already": True}))
            return 0
        sh("mount", "-t", "nfs", "-o", "ro,nfsvers=4.1,noatime,nosuid,nodev,noexec", args.source, str(target))
        print(json.dumps({"mounted": str(target), "source": args.source}))
        return 0
    root = Path(args.root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    bad = []
    for line in (root / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        path = root / rel
        h = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            bad.append(rel)
            continue
        if h.hexdigest() != digest:
            bad.append(rel)
    print(json.dumps({"tier": manifest.get("tier"), "version": manifest.get("version"),
                      "sites": len(manifest.get("sites") or []), "bad": bad[:50], "ok": not bad}))
    return 0 if not bad else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gs", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument("--build", action="store_true")
    up.add_argument("--probe", action="store_true")
    sub.add_parser("down")
    h = sub.add_parser("health")
    h.add_argument("--json", action="store_true")
    c = sub.add_parser("crumb")
    c.add_argument("op", choices=["plant", "remove", "list", "check"])
    c.add_argument("--b64", default="")
    c.add_argument("--file", default="")
    c.add_argument("--exercise", default="")
    c.add_argument("--id", default=None)
    k = sub.add_parser("corpus")
    k.add_argument("op", choices=["verify", "mount"])
    k.add_argument("root", nargs="?", default="")
    k.add_argument("--source", default="")
    k.add_argument("--dir", default="")
    args = p.parse_args(argv)
    try:
        if args.cmd == "crumb" and args.op in ("remove", "check") and not args.exercise:
            raise GsError("--exercise is required")
        if args.cmd == "corpus" and args.op == "mount" and not args.source:
            raise GsError("--source SERVER:/VOLUME is required")
        return {"up": cmd_up, "down": cmd_down, "health": cmd_health, "crumb": cmd_crumb, "corpus": cmd_corpus}[
            args.cmd](args)
    except (GsError, ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
