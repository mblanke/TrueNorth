# Author: Marc-Andre Blanke
# Created: 2026-11-23T22:39:00Z
"""install/lab/roles/tn_prefetch/files/registry_prefetch.py against a local fake registry.

The registry wants a Bearer token from its own realm, as Nexus does with the DockerToken
realm. (Here, not in tests/contracts: the contract suite blocks every socket, loopback too.)
The image-to-proxy mapping itself is tested next to the script's, in
tests/contracts/test_depot_docker_mirrors.py.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _helper():
    spec = importlib.util.spec_from_file_location(
        "registry_prefetch", ROOT / "install/lab/roles/tn_prefetch/files/registry_prefetch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


@pytest.fixture
def fake_registry():
    """A registry that wants a Bearer token from its own realm (like Nexus' DockerToken)."""
    layer, cfg = b"layer-bytes", b'{"config": 1}'
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": _sha(cfg)},
            "layers": [{"digest": _sha(layer)}],
        }
    ).encode()
    arm = json.dumps({"schemaVersion": 2, "config": {"digest": _sha(b"x")}, "layers": []}).encode()
    index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"digest": _sha(arm), "platform": {"os": "linux", "architecture": "arm64"}},
                {"digest": _sha(manifest), "platform": {"os": "linux", "architecture": "amd64"}},
            ],
        }
    ).encode()
    objects = {
        f"/v2/library/nginx/manifests/{_sha(index)}": (index, "application/vnd.oci.image.index.v1+json"),
        f"/v2/library/nginx/manifests/{_sha(manifest)}": (manifest, "application/vnd.oci.image.manifest.v1+json"),
        f"/v2/library/nginx/blobs/{_sha(cfg)}": (cfg, "application/octet-stream"),
        f"/v2/library/nginx/blobs/{_sha(layer)}": (layer, "application/octet-stream"),
    }
    seen: list[str] = []
    basic = "Basic " + base64.b64encode(b"tn-prefetch:pw").decode()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            port = self.server.server_address[1]
            if self.path.startswith("/v2/token"):
                ok = self.headers.get("Authorization") == basic
                self.send_response(200 if ok else 401)
                self.end_headers()
                if ok:
                    self.wfile.write(b'{"token": "t0k"}')
                return
            if self.headers.get("Authorization") != "Bearer t0k":
                self.send_response(401)
                self.send_header("WWW-Authenticate", f'Bearer realm="http://127.0.0.1:{port}/v2/token",service="nexus"')
                self.end_headers()
                return
            seen.append(self.path)
            body, ctype = objects.get(self.path, (None, None))
            self.send_response(200 if body is not None else 404)
            if body is not None:
                self.send_header("Content-Type", ctype)
            self.end_headers()
            if body is not None:
                self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", _sha(index), seen
    srv.shutdown()


def test_helper_walks_index_manifest_and_blobs_with_a_token(fake_registry, capsys, monkeypatch):
    base, digest, seen = fake_registry
    monkeypatch.setenv("NEXUS_USER", "tn-prefetch")
    monkeypatch.setenv("NEXUS_PASSWORD", "pw")
    rc = _helper().main(["--hub", base, "--quay", base, f"nginx:1.27-alpine@{digest}"])
    out = capsys.readouterr().out
    assert rc == 0 and "images ok=1 failed=0 " in out, out
    assert [p.rsplit("/", 2)[1] for p in seen] == ["manifests", "manifests", "blobs", "blobs"]  # amd64 only
    assert "pw" not in out


def test_helper_fails_on_a_digest_that_does_not_match(fake_registry, capsys, monkeypatch):
    base, _, _ = fake_registry
    monkeypatch.setenv("NEXUS_USER", "tn-prefetch")
    monkeypatch.setenv("NEXUS_PASSWORD", "pw")
    rc = _helper().main(["--hub", base, "--quay", base, "nginx@sha256:" + "0" * 64])
    assert rc == 1 and "images ok=0 failed=1 " in capsys.readouterr().out


def test_helper_fails_without_credentials(fake_registry, capsys, monkeypatch):
    base, digest, _ = fake_registry
    monkeypatch.delenv("NEXUS_USER", raising=False)
    monkeypatch.delenv("NEXUS_PASSWORD", raising=False)
    assert _helper().main(["--hub", base, "--quay", base, f"nginx@{digest}"]) == 1
