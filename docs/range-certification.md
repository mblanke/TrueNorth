# Range Certification Record: verifying an export

ADR 0013 §6, issue #244. Code: `control-plane/api/app/range_certification/`.

Each build of a range has one append-only record. Staff export it as signed JSON
(`GET /ranges/{id}/certification/export`, or `?deployment_id=` for an archived build)
or as a PDF. The JSON export is the evidence. The PDF is a readable copy that names the
record's `record_sha256`.

## What is signed

```json
{
  "format": "truenorth.range-certification.v1",
  "exported_at": "...", "exported_by": "...",
  "record_sha256": "<sha256 of canonical(record)>",
  "record": { "...": "header, verdict, every section with every entry" },
  "signature": { "alg": "Ed25519", "key_id": "...", "public_key": "<base64 raw>",
                 "signed_sha256": "...", "value": "<base64 signature>" }
}
```

- **Signed bytes:** the canonical JSON of the whole document except `signature`. Canonical
  means keys sorted, no whitespace (`,` and `:` separators), UTF-8, and non-ASCII characters
  written as they are. This is Python's
  `json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, not RFC 8785.
  A verifier in another language must produce the same bytes. The current payloads hold
  strings, integers, booleans and nulls, but no floats.
- **`record_sha256`:** the SHA-256 of the canonical JSON of `record` alone. Every export of
  the record in the same state carries the same value, whoever made it and whenever. The
  PDF prints it, and the audit row of a retention purge records it.
- **The key:** Ed25519, derived with HKDF-SHA256 from the platform secret `TN_SECRETS_KEY`
  (info `truenorth/range-certification-export/v1`). `key_id` is the first 16 hex
  characters of the SHA-256 of the raw public key.

## Trusting the key

Do not trust the `public_key` inside an export on its own: anyone can re-sign an edited
document with their own key. Instead:

1. Fetch the platform's keys from `GET /range-certification/signing-keys`
   (`range:certification_read`). It lists every key that has ever signed an export, the
   current one first (`current: true`). Retired keys stay listed, because their rows are
   never deleted.
2. Record the current `key_id` out of band, as you do for the audit-log fingerprint in
   ADR 0011.
3. Accept an export only if its `signature.key_id` is a key you trust and its signature
   verifies against that key's `public_key`.

## Rotation

`TN_SECRETS_KEY` takes a comma-separated list. The first entry signs, and every entry
verifies. When a key is rotated out completely, exports signed with it still verify:

- through `service.verify_export` inside TrueNorth, and
- with the retired public key listed at `/range-certification/signing-keys` for anyone
  outside.

## Verifying outside TrueNorth (Python)

```python
import base64, hashlib, json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

doc = json.load(open("range-certification-<deployment>.json"))
trusted = {"<key_id>": "<public_key from /range-certification/signing-keys>"}

sig = doc["signature"]
body = json.dumps({k: v for k, v in doc.items() if k != "signature"},
                  sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
Ed25519PublicKey.from_public_bytes(base64.b64decode(trusted[sig["key_id"]])).verify(
    base64.b64decode(sig["value"]), body)  # raises InvalidSignature if anything changed
rec = json.dumps(doc["record"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
assert hashlib.sha256(rec).hexdigest() == doc["record_sha256"]
```

## Retention

- A range's newest record is never purged.
- Records of earlier builds are purged `RANGE_CERT_RETENTION_MONTHS` months after they
  were opened. Set it with the installer variable `tn_range_cert_retention_months`; the
  default is 12, and `0` keeps them forever.
- Each archived record shows `purge_after`. Export it before that date to keep it.
- Each purge writes an audit row (`range_certification.purge`) with the record's
  `record_sha256`.
