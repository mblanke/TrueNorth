"""scripts/build_feed_bundle.py: a bundle it signs is one the API accepts (ADR 0014 §25).

The feeds are recorded payloads behind a fake ``FeedHttp``: no network.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.content_feeds.schemas import FeedBundleIn
from app.range_certification import signing

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests/worker/content_feeds_fixtures"
_spec = importlib.util.spec_from_file_location("build_feed_bundle", ROOT / "scripts" / "build_feed_bundle.py")
bfb = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("build_feed_bundle", bfb)
_spec.loader.exec_module(bfb)

NOW = datetime(2026, 10, 27, 4, 0, tzinfo=UTC)


class FakeHttp:
    def __init__(self, proxy=None):
        self.proxy = proxy

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def get_json(self, url, *, params=None, headers=None):
        if "endoflife" in url:
            return json.loads((FIX / "endoflife_ubuntu.json").read_text()) if "ubuntu" in url else []
        if "cisa" in url:
            return json.loads((FIX / "cisa_kev.json").read_text())
        return {"totalResults": 0, "vulnerabilities": []}


def test_the_new_key_is_private_and_its_public_half_is_printed(tmp_path, capsys):
    path = tmp_path / "feed.key"
    assert bfb.main(["--new-key", str(path)]) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    public = out.split("tn_feeds_bundle_public_key: ")[1].split()[0]
    assert len(base64.b64decode(public)) == 32
    assert bfb.main(["--new-key", str(path)]) == 1  # never overwritten


def test_a_built_bundle_verifies_against_the_public_key_and_is_a_feed_bundle(tmp_path, monkeypatch):
    key_path = tmp_path / "feed.key"
    bfb.new_key(key_path)
    key = bfb.load_key(key_path)
    monkeypatch.setattr(bfb, "FeedHttp", FakeHttp)
    monkeypatch.setattr(sys.modules["worker.feed_backends.nvd"], "_pause", lambda s: None)
    doc, problems, failed = bfb.build(proxy=None, since=None, now=NOW)
    assert failed == []
    signed = bfb.sign(doc, key)
    assert signing.verify_with_public_key(signed, key.public_key())
    bundle = FeedBundleIn.model_validate({k: v for k, v in signed.items() if k != "signature"})
    keys = {e.key for e in bundle.entries}
    assert "eol:ubuntu:24.04" in keys and "kev:CVE-2026-21001" in keys
    tampered = {**signed, "entries": signed["entries"][1:]}
    assert not signing.verify_with_public_key(tampered, key.public_key())
