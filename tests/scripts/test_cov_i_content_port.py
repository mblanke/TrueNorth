"""COV-I: scripts/content_port_inprocess.py — the branches the CLI tests never reach.

Whole-slice coverage before this file: 82.8% (159/192 statements). Missing were the offline
verify refusals (bad public key, not signed, signed by another key), the preview/import form
fields (tenant, shared, overwrite), the plan printer's already-imported/reasons lines, the
key/list commands, the non-409 export refusal, the upload refusals, and main()'s --admin and
connector-refused exits.

The offline tests sign real Ed25519 documents (the ``cryptography`` package, as the script
itself uses). For run()-level refusals the ``api`` object is the HTTP boundary the script is
handed through its own ``connect`` injection point — the same seam
test_content_port_cli.py wires to the real in-process API — so the fake returns the
(status, body) pairs the API contract produces and the script runs unmodified.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import content_port_inprocess as cli  # noqa: E402

COURSE_ID = "00000000-0000-0000-0000-0000000000c1"  # --item wants TYPE:UUID


# ── offline verification ────────────────────────────────────────────────────────
def _keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    raw = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return sk, base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()[:16]


def _sign_doc(doc: dict, key_id: str | None = None) -> tuple[dict, str, str]:
    """Really sign ``doc``: (signed doc, signer's public key b64, key id)."""
    sk, pub, kid = _keypair()
    body = cli.CONTEXT + cli.canonical({k: v for k, v in doc.items() if k != "signature"})
    sig = {"alg": "Ed25519", "key_id": key_id or kid,
           "value": base64.b64encode(sk.sign(body)).decode()}
    return {**doc, "signature": sig}, pub, kid


def _signed_doc() -> tuple[dict, str, str]:
    return _sign_doc({"bundle_id": "b-1", "origin": {"site": "a"}, "items": [
        {"key": "course:1", "name": "C", "content": {"name": "C"},
         "content_sha256": hashlib.sha256(cli.canonical({"name": "C"})).hexdigest()}]})


def test_a_public_key_that_is_not_ed25519_is_refused_before_anything_is_read():
    doc, _, _ = _signed_doc()
    assert cli.verify_offline(doc, "not base64 at all!!") == [
        "the public key is not a base64 Ed25519 public key"]
    assert cli.verify_offline(doc, base64.b64encode(b"short").decode()) == [
        "the public key is not a base64 Ed25519 public key"]


def test_a_document_without_a_signature_is_not_signed():
    doc, pub, _ = _signed_doc()
    del doc["signature"]
    assert cli.verify_offline(doc, pub) == ["not signed"]
    assert cli.verify_offline({**doc, "signature": "scrawled"}, pub) == ["not signed"]


def test_a_bundle_claiming_another_key_id_is_reported():
    doc, pub, kid = _signed_doc()
    _, _, other = _keypair()
    doc["signature"]["key_id"] = other
    assert cli.verify_offline(doc, pub) == [f"signed by key '{other}', not {kid}"]


def test_a_bundle_signed_by_another_key_does_not_verify():
    doc, pub, _ = _signed_doc()
    sk2, _, _ = _keypair()
    body = cli.CONTEXT + cli.canonical({k: v for k, v in doc.items() if k != "signature"})
    doc["signature"]["value"] = base64.b64encode(sk2.sign(body)).decode()
    assert cli.verify_offline(doc, pub) == [
        "the signature does not verify (changed after signing, or another key or purpose)"]


def test_an_item_whose_content_does_not_match_its_hash_is_reported():
    doc, _, _ = _signed_doc()
    doc["items"][0]["content_sha256"] = "0" * 64
    doc, pub, _ = _sign_doc(doc)  # re-signed, so only the hash is wrong
    assert cli.verify_offline(doc, pub) == ["item course:1: content hash mismatch"]


def test_a_well_signed_bundle_verifies_offline(tmp_path, capsys):
    doc, pub, _ = _signed_doc()
    assert cli.verify_offline(doc, pub) == []
    f = tmp_path / "bundle.json"
    f.write_text(repr(doc))  # not JSON at all
    assert cli.main(["verify", str(f), "--public-key", pub]) == 1
    assert "cannot read" in capsys.readouterr().err
    f.write_text(json.dumps(doc))
    assert cli.main(["verify", str(f), "--public-key", pub]) == 0
    assert "verifies" in capsys.readouterr().out


# ── the plan printer ────────────────────────────────────────────────────────────
def test_the_plan_prints_what_importing_would_do(capsys):
    cli._print_plan({
        "bundle_id": "b-1", "exported_at": "2026-11-05", "key_id": "kid",
        "already_imported_at": "2026-11-06T09:00",
        "items": [
            {"action": "conflict", "key": "course:1", "name": "C",
             "reasons": ["newer here"], "notes": ["kept as draft"]},
            {"action": "create", "key": "scenario:2", "name": "S"},
        ],
        "missing_golden_builds": [{"catalogue_id": "g-win", "build_id": "b7",
                                   "needed_by": ["range_template:3"]}],
    })
    out = capsys.readouterr().out
    assert "Bundle b-1 made 2026-11-05, signed by kid" in out
    assert "already imported on 2026-11-06T09:00: refused" in out
    assert "conflict" in out and "course:1  C" in out  # the reason goes on its own line
    assert "! newer here" in out and "- kept as draft" in out
    assert "port this golden template first: g-win build b7 (needed by range_template:3)" in out


# ── the API boundary: key, list, export and upload refusals ─────────────────────
class _Api:
    """(status, body) per (method, path), remembering what was sent."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple] = []

    def get(self, path):
        self.calls.append(("GET", path))
        return self.routes[("GET", path)]

    def post(self, path, json=None, content=None, headers=None):
        self.calls.append(("POST", path, json, content, headers))
        return self.routes[("POST", path)]


def _args(argv):
    return cli.parser().parse_args(argv)


def test_key_prints_the_signing_key_and_refuses_a_failure(capsys):
    api = _Api({("GET", "/content-port/signing-key"): (200, {"key_id": "kid", "public_key": "PK"})})
    assert cli.run(api, _args(["key"])) == 0
    assert "key_id kid\ntn_content_port_trusted_keys: PK" in capsys.readouterr().out
    api = _Api({("GET", "/content-port/signing-key"): (503, "key service down")})
    assert cli.run(api, _args(["key"])) == 1
    assert "refused (503): key service down" in capsys.readouterr().err


def test_list_prints_proven_and_why_not_and_filters(capsys):
    items = [{"item_type": "course", "item_id": "1", "title": "C", "label": "proven",
              "proven": True},
             {"item_type": "scenario", "item_id": "2", "title": "S", "label": "no range built",
              "proven": False}]
    api = _Api({("GET", "/content-port/items"): (200, items)})
    assert cli.run(api, _args(["list"])) == 0
    out = capsys.readouterr().out
    assert "course:1  C" in out and "no range built" in out
    assert cli.run(api, _args(["list", "--proven-only"])) == 0
    out = capsys.readouterr().out
    assert "course:1  C" in out and "scenario:2" not in out
    api = _Api({("GET", "/content-port/items"): (500, "boom")})
    assert cli.run(api, _args(["list"])) == 1
    assert "refused (500): boom" in capsys.readouterr().err


def test_an_export_refusal_that_is_not_the_conflict_shape(capsys):
    api = _Api({("POST", "/content-port/export"): (409, "plain text, not a detail dict")})
    assert cli.run(api, _args(["export", "--item", f"course:{COURSE_ID}", "--out", "b.json"])) == 1
    assert "refused (409): plain text" in capsys.readouterr().err
    api = _Api({("POST", "/content-port/export"): (500, {"detail": "boom"})})
    assert cli.run(api, _args(["export", "--item", f"course:{COURSE_ID}", "--out", "b.json"])) == 1
    assert "refused (500)" in capsys.readouterr().err


def test_what_the_upload_sends_and_what_a_refused_upload_reports(tmp_path, capsys):
    f = tmp_path / "bundle.json"
    f.write_text("{}")
    api = _Api({("POST", "/content-port/preview"): (200, {"bundle_id": "b-1", "exported_at": "t",
                                                          "key_id": "kid", "items": []})})
    argv = ["import", str(f), "--tenant", "00000000-0000-0000-0000-000000000001",
            "--shared", "--overwrite", "course:1", "--overwrite", "scenario:2"]
    assert cli.run(api, _args(argv)) == 0  # no --yes: preview only
    _method, path, _j, _c, _h = api.calls[0]
    assert path == "/content-port/preview"
    boundary = _h["Content-Type"].split("boundary=")[1]
    body = _c.decode()
    assert 'name="target_tenant_id"' in body and body.count('name="overwrite"') == 2
    assert 'name="shared"' in body and f"--{boundary}--" in body

    api = _Api({("POST", "/content-port/import"): (413, "bundle too large")})
    assert cli.run(api, _args(["import", str(f), "--yes"])) == 1
    assert "refused (413): bundle too large" in capsys.readouterr().err
    api = _Api({("POST", "/content-port/preview"): (409, {"detail": {"message": "replay"}})})
    assert cli.run(api, _args(["preview", str(f)])) == 1
    assert "refused (409)" in capsys.readouterr().err


# ── main(): the admin and the connector ─────────────────────────────────────────
def test_without_an_admin_or_when_the_connector_refuses_main_exits_2(capsys):
    assert cli.main(["list"]) == 2
    assert "--admin is required" in capsys.readouterr().err
    assert cli.main(["--admin", "a@example.test", "list"], connect=lambda email: None) == 2


def test_main_runs_through_the_injected_connector(capsys):
    api = _Api({("GET", "/content-port/signing-key"): (200, {"key_id": "kid", "public_key": "PK"})})
    connected = (object(), api)
    assert cli.main(["--admin", "a@example.test", "key"], connect=lambda email: connected) == 0
    assert "tn_content_port_trusted_keys: PK" in capsys.readouterr().out
