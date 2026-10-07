#!/usr/bin/env python3
"""Greyspace corpus and stack tooling (ADR 0007). Standard library only.

    corpus.py t0 --out DIR                    build the T0 CI fixture (about 20 synthetic sites)
    corpus.py index --root DIR --tier t1 --version V --seeds FILE
                                              write manifest.json + checksums.sha256 for a tree
                                              of mirrored sites (build-sample.sh calls this)
    corpus.py verify --root DIR               check the manifest and every checksum
    corpus.py render --corpus DIR --out DIR [--packs news,dev] [--no-threat]
                                              generate the Docker stack for a corpus

Every tier has the same layout (sites/<fqdn>/..., manifest.json, checksums.sha256), so
``render`` and the web farm do not care which tier they are given. The manifest parser
and the config generator are the API's own (control-plane/api/app/greyspace), so the
control plane and the runtime agree by construction.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane" / "api"))

from app.greyspace import config, fixture  # noqa: E402 — after the path insert
from app.greyspace.manifest import TIERS, ManifestError, parse_manifest, valid_fqdn  # noqa: E402

IMAGES_DIR = ROOT / "greyspace" / "images"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(root: Path) -> int:
    """``checksums.sha256`` for every file under sites/ (sha256sum -c compatible)."""
    lines = []
    for path in sorted((root / "sites").rglob("*")):
        if path.is_file():
            lines.append(f"{_sha256(path)}  {path.relative_to(root).as_posix()}\n")
    (root / "checksums.sha256").write_text("".join(lines), encoding="utf-8")
    return len(lines)


def _tree_stats(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def _make_video(dest: Path) -> bool:
    """A two-second 160x120 test pattern (ffmpeg testsrc, CC0). False when ffmpeg is missing."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
           "-c:v", "libvpx", "-b:v", "64k", "-an", str(dest)]
    return subprocess.run(cmd, check=False).returncode == 0 and dest.is_file()


def cmd_t0(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    for fqdn, category, title, tagline in fixture.SITES:
        for rel, data in fixture.site_files(fqdn, category, title, tagline).items():
            dest = out / "sites" / fqdn / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    video = out / "sites" / fixture.VIDEO_SITE / "media" / f"{fixture.VIDEO_ID}.webm"
    have_video = not args.no_video and _make_video(video)
    doc = fixture.t0_manifest_doc(video_bytes=video.stat().st_size if have_video else 0)
    if not have_video:
        print("t0: no video (ffmpeg missing or --no-video); the manifest lists none", file=sys.stderr)
        doc["videos"] = []
    parse_manifest(doc)  # the cap and every invariant, before anything is published
    (out / "manifest.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    count = write_checksums(out)
    print(f"t0: {len(doc['sites'])} sites, {count} files, {doc['totals']['bytes']} bytes -> {out}")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    """Manifest for a mirrored tree. Seeds file: ``<fqdn> <category> <licence> <source-url>``."""
    root = Path(args.root).resolve()
    seeds: dict[str, tuple[str, str, str]] = {}
    for line in Path(args.seeds).read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 4 and not line.lstrip().startswith("#"):
            seeds[parts[0].lower()] = (parts[1], parts[2], parts[3])
    isps = [dict(i) for i in fixture.ISPS]
    pools = [ipaddress.IPv4Network(i["prefix"]) for i in isps]
    hosts = [list(p.hosts())[256:] for p in pools]  # skip the infrastructure /24
    sites = []
    for n, site_dir in enumerate(sorted(p for p in (root / "sites").iterdir() if p.is_dir())):
        fqdn = site_dir.name.lower()
        if not valid_fqdn(fqdn):
            print(f"index: skipping {site_dir.name}: not a domain name", file=sys.stderr)
            continue
        category, licence, source = seeds.get(fqdn, ("misc", "unknown", "unknown"))
        files, size = _tree_stats(site_dir)
        sites.append({
            "fqdn": fqdn, "aliases": [], "ip": str(hosts[n % len(hosts)][n // len(hosts)]),
            "category": category, "path": f"sites/{site_dir.name}", "files": files, "bytes": size,
            "licence": licence, "source": source,
        })
    doc = {
        "format": "greyspace-corpus/1", "tier": args.tier, "version": args.version,
        "description": f"Greyspace {args.tier} sample built by greyspace/scripts/build-sample.sh",
        "isps": isps, "sites": sites, "videos": [],
        "threat_actors": [{"name": a["name"], "domains": [dict(d) for d in a["domains"]]} for a in fixture.THREAT_ACTORS],
        "totals": {"sites": len(sites), "bytes": sum(s["bytes"] for s in sites)},
    }
    parse_manifest(doc)
    (root / "manifest.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"index: {len(sites)} sites, {write_checksums(root)} files -> {root / 'manifest.json'}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = parse_manifest(json.loads((root / "manifest.json").read_text(encoding="utf-8")))
    bad = 0
    for line in (root / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        path = root / rel
        if not path.is_file() or _sha256(path) != digest:
            print(f"verify: {rel}: checksum mismatch or missing", file=sys.stderr)
            bad += 1
    for site in manifest.sites:
        if not (root / site.path).is_dir():
            print(f"verify: {site.fqdn}: {site.path} is missing", file=sys.stderr)
            bad += 1
    cap = TIERS[manifest.tier].cap_bytes
    print(f"verify: {manifest.tier} {manifest.version}: {len(manifest.sites)} sites, "
          f"{manifest.total_bytes} bytes (cap {cap}), {bad} problems")
    return 1 if bad else 0


def cmd_render(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus).resolve()
    manifest = parse_manifest(json.loads((corpus / "manifest.json").read_text(encoding="utf-8")))
    params = config.BlockParams(
        corpus_tier=manifest.tier,
        site_packs=tuple(args.packs.split(",")) if args.packs else None,
        threat_infra=not args.no_threat,
    )
    rendered = config.render(manifest, params, corpus_dir=str(corpus), images_dir=str(IMAGES_DIR))
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    for rel, text in rendered.files.items():
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        if rel.endswith(".sh"):
            os.chmod(dest, 0o755)
    (out / "summary.json").write_text(json.dumps(rendered.summary, indent=2) + "\n", encoding="utf-8")
    print(f"render: {len(rendered.files)} files, {rendered.summary['sites']} sites -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("t0")
    p.add_argument("--out", default=str(ROOT / "build" / "greyspace" / "corpus-t0"))
    p.add_argument("--no-video", action="store_true")
    p = sub.add_parser("index")
    p.add_argument("--root", required=True)
    p.add_argument("--tier", required=True, choices=sorted(TIERS))
    p.add_argument("--version", required=True)
    p.add_argument("--seeds", required=True)
    p = sub.add_parser("verify")
    p.add_argument("--root", required=True)
    p = sub.add_parser("render")
    p.add_argument("--corpus", required=True)
    p.add_argument("--out", default=str(ROOT / "build" / "greyspace" / "stack"))
    p.add_argument("--packs", default="")
    p.add_argument("--no-threat", action="store_true")
    args = parser.parse_args(argv)
    try:
        return {"t0": cmd_t0, "index": cmd_index, "verify": cmd_verify, "render": cmd_render}[args.cmd](args)
    except (ManifestError, config.ConfigError) as exc:
        print(f"{args.cmd}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
