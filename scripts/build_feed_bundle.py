#!/usr/bin/env python3
"""Build a signed content feed bundle for an air-gapped TrueNorth (ADR 0014 decision 25).

Run on a machine with internet access and a checkout of this repository. It reads the same
feeds the online pull reads (endoflife.date, CISA KEV, the NVD CVE API), with the same
sources (control-plane/worker/worker/feed_backends) and product map
(content/catalogue/feed_products.yaml), and signs the result with the site's feed key: an
Ed25519 key, signed the way range certification exports are (canonical JSON without the
``signature`` member; control-plane/api/app/range_certification/signing.py).

    # once: make the key pair. Keep the private key off the air-gapped site; give the site
    # the public key (installer setting tn_feeds_bundle_public_key).
    python scripts/build_feed_bundle.py --new-key ~/.truenorth/feed-bundle.key

    # each time: build a bundle and carry it across; an admin imports it on the Content view.
    python scripts/build_feed_bundle.py --key ~/.truenorth/feed-bundle.key --out feeds-2026-10-27.json \
        [--proxy http://proxy:3128] [--since 2026-09-27]

An NVD API key (optional) is read from TN_FEEDS_NVD_API_KEY, never from the command line.
Nothing but the GET requests leaves this machine.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "control-plane/worker"))
sys.path.insert(0, str(ROOT / "control-plane/api"))

from app.range_certification.signing import canonical, key_id  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from worker.content_feeds import catalogue  # noqa: E402
from worker.feed_backends import ONLINE_SOURCES, FeedError, FeedHttp, get_feed_source  # noqa: E402

FORMAT = "truenorth-feed-bundle/1"


def _raw_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def new_key(path: Path) -> int:
    if path.exists():
        print(f"{path} exists; not overwritten", file=sys.stderr)
        return 1
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(base64.b64encode(seed).decode() + "\n")
    print("Private key written to", path, "(keep it off the air-gapped site)")
    print("Public key for tn_feeds_bundle_public_key:", base64.b64encode(_raw_public(key)).decode())
    print("Key id:", key_id(key.public_key()))
    return 0


def load_key(path: Path) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(path.read_text().strip(), validate=True))


def sign(doc: dict, key: Ed25519PrivateKey) -> dict:
    body = canonical({k: v for k, v in doc.items() if k != "signature"})
    return {**doc, "signature": {
        "alg": "Ed25519", "key_id": key_id(key.public_key()), "public_key": base64.b64encode(_raw_public(key)).decode(),
        "signed_sha256": hashlib.sha256(body).hexdigest(), "value": base64.b64encode(key.sign(body)).decode()}}


def build(*, proxy: str | None, since: datetime | None, now: datetime) -> tuple[dict, list[str], list[str]]:
    """(the unsigned bundle, per-product warnings, sources that failed outright)."""
    products = catalogue.all_products(catalogue.load())
    entries, problems, failed = {}, [], []
    with FeedHttp(proxy=proxy) as http:
        for name in ONLINE_SOURCES:
            try:
                result = get_feed_source(name).fetch(http, products, since=since, now=now)
            except FeedError as exc:
                failed.append(f"{name}: {exc}")
                continue
            problems += [f"{name}: {e}" for e in result.errors]
            for e in result.entries:
                entries[e.key] = e.to_dict()
    doc = {"format": FORMAT, "generated_at": now.isoformat(), "entries": [entries[k] for k in sorted(entries)]}
    return doc, problems, failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--new-key", type=Path, help="write a new private key here and print its public key")
    ap.add_argument("--key", type=Path, help="the private key that signs the bundle")
    ap.add_argument("--out", type=Path, help="where to write the bundle")
    ap.add_argument("--proxy", help="HTTP proxy for the feed requests")
    ap.add_argument("--since", help="NVD: CVEs modified since this date (default: 30 days ago; set it to the "
                                    "last bundle's date so nothing is missed)")
    ap.add_argument("--allow-partial", action="store_true",
                    help="sign the bundle even when a source could not be read at all")
    args = ap.parse_args(argv)
    if args.new_key:
        return new_key(args.new_key)
    if not args.key or not args.out:
        ap.error("--key and --out are required (or --new-key)")
    now = datetime.now(UTC)
    since = (datetime.fromisoformat(args.since).replace(tzinfo=UTC) if args.since
             else now - timedelta(days=30))
    doc, problems, failed = build(proxy=args.proxy, since=since, now=now)
    for p in problems:
        print("warning:", p, file=sys.stderr)
    if failed and not args.allow_partial:
        for f in failed:
            print("error:", f, file=sys.stderr)
        print("No bundle written: a source could not be read (--allow-partial signs it anyway).", file=sys.stderr)
        return 2
    signed = sign(doc, load_key(args.key))
    args.out.write_text(json.dumps(signed, indent=1) + "\n", encoding="utf-8")
    print(f"{len(doc['entries'])} entries, signed by key {signed['signature']['key_id']}: {args.out}")
    for f in failed:
        print("warning: left out:", f, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
