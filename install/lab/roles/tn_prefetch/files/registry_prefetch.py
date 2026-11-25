#!/usr/bin/env python3
# Author: Marc-Andre Blanke
# Created: 2026-11-23T22:39:00Z
"""Warm TN-DEPOT01's Nexus Docker proxies with images pinned by digest.

Runs on the build host during a prefetch window (install/lab/prefetch.yml): asking a Nexus
proxy for a manifest and its blobs makes Nexus fetch and cache them, so greyspace-host
(infra/vsphere/packer/files/linux/roles/greyspace-host.sh) can pull the same digests later
with the depot offline. Talks the registry HTTP API directly, so the build host's Docker
daemon needs no insecure-registries entry and no restart.

    registry_prefetch.py --hub http://10.30.30.30:8082 --quay http://10.30.30.30:8084 \\
        --platform linux/amd64 IMAGE@sha256:... [...]

Credentials: NEXUS_USER / NEXUS_PASSWORD in the environment (tn-prefetch). Prints one
"ok <image>" / "fail <image>: <why>" line per image and a final "images ok=N failed=M ".
Exit 1 when any image failed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
ACCEPT = ", ".join(INDEX_TYPES + MANIFEST_TYPES)
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def proxy_ref(image: str, hub: str, quay: str) -> tuple[str, str, str]:
    """(registry base URL, repository path, digest) of a pinned image on its depot proxy.

    Same mapping as tn_proxy_ref in greyspace-host.sh: Docker Hub (official images under
    library/) -> hub, quay.io -> quay; anything else, or no digest, is an error."""
    if "@" not in image:
        raise ValueError(f"{image} is not pinned by digest")
    name, digest = image.rsplit("@", 1)
    if not DIGEST.match(digest):
        raise ValueError(f"{image} is not pinned by a sha256 digest")
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: name.rindex(":")]
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = "docker.io", name
    if registry in ("docker.io", "registry-1.docker.io", "index.docker.io"):
        if not hub:
            raise ValueError(f"no Docker Hub proxy for {image}")
        return hub.rstrip("/"), path if "/" in path else f"library/{path}", digest
    if registry == "quay.io":
        if not quay:
            raise ValueError(f"no quay.io proxy for {image}")
        return quay.rstrip("/"), path, digest
    raise ValueError(f"{image} is on {registry}; the depot proxies only Docker Hub and quay.io")


class Registry:
    """Minimal Docker registry v2 client: Basic, or Bearer from the realm a 401 names."""

    def __init__(self, base: str, user: str = "", password: str = "", timeout: int = 600):
        self.base = base.rstrip("/")
        self.user, self.password, self.timeout = user, password, timeout
        self._auth: str | None = None

    def _basic(self) -> str:
        return "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()

    def _token(self, challenge: str, scope_hint: str) -> str:
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop("realm", "")
        if not realm:
            raise RuntimeError(f"registry auth challenge without a realm: {challenge}")
        params.setdefault("scope", scope_hint)
        req = urllib.request.Request(realm + "?" + urllib.parse.urlencode(params))
        if self.user:
            req.add_header("Authorization", self._basic())
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.load(resp)
        token = body.get("token") or body.get("access_token")
        if not token:
            raise RuntimeError("registry token endpoint returned no token")
        return f"Bearer {token}"

    def get(self, path: str, accept: str | None = None, scope: str = ""):
        url = f"{self.base}/v2/{path}"
        for attempt in (0, 1):
            req = urllib.request.Request(url)
            if accept:
                req.add_header("Accept", accept)
            if self._auth:
                req.add_header("Authorization", self._auth)
            try:
                return urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as err:
                if err.code != 401 or attempt:
                    raise
                challenge = err.headers.get("WWW-Authenticate", "")
                if challenge.lower().startswith("bearer"):
                    self._auth = self._token(challenge[len("bearer") :].strip(), scope)
                elif self.user:
                    self._auth = self._basic()
                else:
                    raise
        raise AssertionError("unreachable")


def _read_verified(resp, digest: str, sink: bool = False) -> bytes:
    h = hashlib.sha256()
    data = bytearray()
    while chunk := resp.read(1 << 20):
        h.update(chunk)
        if not sink:
            data.extend(chunk)
    if "sha256:" + h.hexdigest() != digest:
        raise RuntimeError(f"content of {digest} has digest sha256:{h.hexdigest()}")
    return bytes(data)


def warm(reg: Registry, path: str, digest: str, platform: str) -> int:
    """Fetch the manifest (index -> the platform's manifest), the config and every layer.
    Returns the number of blobs fetched. Every piece is checked against its digest."""
    scope = f"repository:{path}:pull"
    with reg.get(f"{path}/manifests/{digest}", ACCEPT, scope) as resp:
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
        doc = json.loads(_read_verified(resp, digest))
    if ctype in INDEX_TYPES or doc.get("manifests"):
        os_, _, arch = platform.partition("/")
        match = [
            m
            for m in doc["manifests"]
            if m.get("platform", {}).get("os") == os_ and m.get("platform", {}).get("architecture") == arch
        ]
        if not match:
            raise RuntimeError(f"no {platform} manifest in the index")
        digest = match[0]["digest"]
        with reg.get(f"{path}/manifests/{digest}", ACCEPT, scope) as resp:
            doc = json.loads(_read_verified(resp, digest))
    blobs = [doc["config"]["digest"], *(layer["digest"] for layer in doc.get("layers", []))]
    for blob in blobs:
        with reg.get(f"{path}/blobs/{blob}", None, scope) as resp:
            _read_verified(resp, blob, sink=True)
    return len(blobs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hub", required=True, help="Nexus docker-proxy (Docker Hub), http://host:8082")
    ap.add_argument("--quay", required=True, help="Nexus quay-proxy (quay.io), http://host:8084")
    ap.add_argument("--platform", default="linux/amd64")
    ap.add_argument("images", nargs="+")
    args = ap.parse_args(argv)
    user, password = os.environ.get("NEXUS_USER", ""), os.environ.get("NEXUS_PASSWORD", "")
    clients: dict[str, Registry] = {}
    ok = failed = 0
    for image in args.images:
        try:
            base, path, digest = proxy_ref(image, args.hub, args.quay)
            reg = clients.setdefault(base, Registry(base, user, password))
            n = warm(reg, path, digest, args.platform)
            print(f"ok {image} ({n} blobs)")
            ok += 1
        except Exception as err:  # noqa: BLE001 - one line per image, never a traceback
            print(f"fail {image}: {err}")
            failed += 1
    print(f"images ok={ok} failed={failed} ")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
