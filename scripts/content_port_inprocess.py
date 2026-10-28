"""Port proven content between TrueNorth sites from the command line (docs/content-port.md).

The air-gapped way to do what the Content view's Port content panel does. It drives the
API in-process from inside the api image, acting as a named platform administrator
(scripts/_inprocess_api.py: no token or password; every request still goes through the
API's routes, permission checks and audit), so the same rules apply: only proven content is
exported, a bundle is verified against tn_content_port_trusted_keys before anything is read,
and everything lands not yet proven.

Run on the host as a one-off api container (as the installer runs load_content_inprocess.py):

    docker compose run --rm --no-deps -w /app -e PYTHONPATH=/app \\
      -v <app checkout>:/srcapp:ro -v <transfer dir>:/port --entrypoint python api \\
      /srcapp/scripts/content_port_inprocess.py --admin <upn> <command> ...

Commands:

    key                                   this site's public key (for the importing site's
                                          tn_content_port_trusted_keys)
    list [--proven-only]                  courses, range templates and scenarios, proven or why not
    export --item TYPE:ID ... --out FILE  a signed bundle of those items and what they need;
                                          refused (exit 3) unless every one is proven
    preview FILE [--tenant ID] [--shared] what importing would do (nothing is stored)
    import FILE [--tenant ID] [--shared] [--overwrite KEY ...] --yes
                                          import; a conflict is overwritten only when named

``verify FILE --public-key B64`` checks a bundle's signature offline, with no API and no
database (it needs only the ``cryptography`` package): run it on the transfer machine.

Exit codes: 0 done; 1 refused or failed; 2 usage or setup; 3 export refused (not proven).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import uuid
from pathlib import Path

CONTEXT = b"truenorth-content-port/1\n"  # app/content_port/signing.py CONTEXT
KEY_RE_TYPES = ("course", "range_template", "scenario")


# ── offline verification (no app import) ────────────────────────────────────────
def canonical(doc: dict) -> bytes:
    """app/range_certification/signing.canonical."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _refuse(text):
    raise ValueError(f"{text} is not allowed (strict JSON: no NaN, Infinity or floats)")


def strict_loads(raw: bytes):
    """As the API reads a bundle (app/content_port/bundle.loads): no duplicate keys, no NaN or
    Infinity, no floats. A document it refuses never "verifies" here either."""
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_refuse, parse_float=_refuse)


def verify_offline(doc: dict, public_b64: str) -> list[str]:
    """Problems with ``doc`` against ``public_b64`` (empty: it verifies)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        raw = base64.b64decode(public_b64, validate=True)
        pub = Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        return ["the public key is not a base64 Ed25519 public key"]
    kid = hashlib.sha256(pub.public_bytes(serialization.Encoding.Raw,
                                          serialization.PublicFormat.Raw)).hexdigest()[:16]
    sig = doc.get("signature") if isinstance(doc, dict) else None
    if not isinstance(sig, dict):
        return ["not signed"]
    problems = []
    if sig.get("alg") != "Ed25519" or sig.get("key_id") != kid:
        problems.append(f"signed by key {sig.get('key_id')!r}, not {kid}")
    try:
        body = CONTEXT + canonical({k: v for k, v in doc.items() if k != "signature"})
        pub.verify(base64.b64decode(sig.get("value") or "", validate=True), body)
    except (InvalidSignature, ValueError, TypeError):
        problems.append("the signature does not verify (changed after signing, or another key or purpose)")
    for item in doc.get("items") or []:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, dict) or hashlib.sha256(canonical(content)).hexdigest() != item.get(
                "content_sha256"):
            problems.append(f"item {item.get('key') if isinstance(item, dict) else '?'}: content hash mismatch")
    return problems


# ── talking to the API ──────────────────────────────────────────────────────────
def multipart(path: Path, fields: list[tuple[str, str]]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
             for name, value in fields]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="bundle.json"\r\n'
                 f"Content-Type: application/json\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def _form(args) -> list[tuple[str, str]]:
    fields = []
    if args.tenant:
        fields.append(("target_tenant_id", str(uuid.UUID(args.tenant))))
    if args.shared:
        fields.append(("shared", "true"))
    for key in getattr(args, "overwrite", None) or []:
        fields.append(("overwrite", key))
    return fields


def _upload(api, path: str, file: Path, fields) -> tuple[int, object]:
    body, ctype = multipart(file, fields)
    return api.post(path, content=body, headers={"Content-Type": ctype})


def _print_plan(body: dict) -> None:
    print(f"Bundle {body['bundle_id']} made {body['exported_at']}, signed by {body['key_id']}")
    if body.get("already_imported_at"):
        print(f"  already imported on {body['already_imported_at']}: refused")
    for i in body["items"]:
        extra = f" ({i['conflict']})" if i.get("conflict") else ""
        print(f"  {i['action']:<9}{extra} {i['key']}  {i['name']}")
        for r in i.get("reasons") or []:
            print(f"      ! {r}")
        for n in i.get("notes") or []:
            print(f"      - {n}")
    for b in body.get("missing_golden_builds") or []:
        print(f"  port this golden template first: {b['catalogue_id']} build {b['build_id']} "
              f"(needed by {', '.join(b['needed_by'])})")


def _item_ref(text: str) -> dict:
    item_type, _, item_id = text.partition(":")
    if item_type not in KEY_RE_TYPES:
        raise argparse.ArgumentTypeError(f"{text!r}: TYPE is one of {', '.join(KEY_RE_TYPES)}")
    try:
        return {"item_type": item_type, "item_id": str(uuid.UUID(item_id))}
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: ID is a UUID") from None


def run(api, args) -> int:
    if args.command == "key":
        status, body = api.get("/content-port/signing-key")
        if status != 200:
            print(f"refused ({status}): {body}", file=sys.stderr)
            return 1
        print(f"key_id {body['key_id']}\ntn_content_port_trusted_keys: {body['public_key']}")
        return 0
    if args.command == "list":
        status, body = api.get("/content-port/items")
        if status != 200:
            print(f"refused ({status}): {body}", file=sys.stderr)
            return 1
        for s in body:
            if args.proven_only and not s["proven"]:
                continue
            print(f"{s['item_type']}:{s['item_id']}  {s['title']}\n    {s['label']}")
        return 0
    if args.command == "export":
        out = Path(args.out)
        if out.exists() and not args.force:
            print(f"{out} exists; pass --force to replace it", file=sys.stderr)
            return 2
        status, body = api.post("/content-port/export", json={"items": args.item})
        if status == 409 and isinstance(body, dict) and isinstance(body.get("detail"), dict):
            print(body["detail"].get("message", "refused"), file=sys.stderr)
            for i in body["detail"].get("items") or []:
                role = " (needed by the selection)" if i.get("dependency") else ""
                print(f"  {i['item_type']}:{i['item_id']} {i.get('title') or ''}{role}", file=sys.stderr)
                for r in i.get("reasons") or []:
                    print(f"      {r}", file=sys.stderr)
            return 3
        if status != 200 or not isinstance(body, dict):
            print(f"refused ({status}): {body}", file=sys.stderr)
            return 1
        out.write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {out}: {len(body['items'])} item(s), bundle {body['bundle_id']}, key {body['origin']['key_id']}")
        return 0
    file = Path(args.file)
    if args.command == "preview" or not args.yes:
        status, body = _upload(api, "/content-port/preview", file, _form(args))
        if status != 200:
            print(f"refused ({status}): {body}", file=sys.stderr)
            return 1
        _print_plan(body)
        if args.command == "import":
            print("Nothing imported: pass --yes to import (name conflicts to replace with --overwrite KEY)")
        return 0
    status, body = _upload(api, "/content-port/import", file, _form(args))
    if status != 200:
        print(f"refused ({status}): {body}", file=sys.stderr)
        return 1
    print(f"imported {body['import_id']}: {body['created']} created, {body['updated']} updated, "
          f"{body['unchanged']} unchanged, {body['skipped']} skipped (none is proven here yet)")
    for i in body["items"]:
        print(f"  {i['action']:<9} {i['key']}  {i['name']}")
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--admin", help="email/UPN of the platform administrator to act as (not for verify)")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("key", help="this site's content-port public key")
    ls = sub.add_parser("list", help="portable content and whether it is proven")
    ls.add_argument("--proven-only", action="store_true")
    ex = sub.add_parser("export", help="export proven content as a signed bundle")
    ex.add_argument("--item", action="append", required=True, type=_item_ref, metavar="TYPE:ID")
    ex.add_argument("--out", required=True, help="the bundle file to write")
    ex.add_argument("--force", action="store_true", help="replace --out if it exists")
    for name in ("preview", "import"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--tenant", help="the tenant it lands in (default: the administrator's)")
        p.add_argument("--shared", action="store_true", help="land as shared content")
        if name == "import":
            p.add_argument("--overwrite", action="append", metavar="KEY", help="a conflicting item to replace")
            p.add_argument("--yes", action="store_true", help="import (without it: preview only)")
    v = sub.add_parser("verify", help="check a bundle's signature offline")
    v.add_argument("file")
    v.add_argument("--public-key", required=True, help="the origin site's public key (base64)")
    return ap


def main(argv: list[str] | None = None, connect=None) -> int:
    args = parser().parse_args(argv)
    if args.command == "verify":
        try:
            doc = strict_loads(Path(args.file).read_bytes())
        except (OSError, ValueError) as exc:
            print(f"cannot read {args.file}: {exc}", file=sys.stderr)
            return 1
        problems = verify_offline(doc, args.public_key)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("verifies" if not problems else "does NOT verify")
        return 0 if not problems else 1
    if not args.admin:
        print("--admin is required", file=sys.stderr)
        return 2
    if connect is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _inprocess_api import connect  # the shared harness: identity override, fresh CSRF
    connected = connect(args.admin)
    if connected is None:
        return 2
    _client, api = connected
    return run(api, args)


if __name__ == "__main__":
    sys.exit(main())
