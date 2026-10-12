"""The Greyspace root CA's key stays in the ca service (review of #133, 2026-10-11).

Before: make-certs.sh ran ``chmod 0644 ./*.key`` (the root key included) in one volume
that the web farm, the threat stub, the NPCs and the probe all mounted, and the root had
a 10-year life and no name constraints: any of those containers could mint a trusted
certificate for any name. Now the root key is 0600 in ``gs-ca`` (the ca service only),
each server mounts only its own zones' certificates, clients only root.crt, the root is
name-constrained to the stack's zones and lives 825 days (leaves 397).

The script tests run the generated make-certs.sh with the local openssl (skipped without
one); CI job ``greyspace`` runs it in the real stack (greyspace/scripts/check-t0.sh 6).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from app.greyspace import config, fixture
from app.greyspace.manifest import parse_manifest


@pytest.fixture(scope="module")
def rendered():
    return config.render(parse_manifest(fixture.t0_manifest_doc()), config.BlockParams(trust_ca=True,
                                                                                         npc_profile="office-day"))


def _mounts(svc: dict) -> list[str]:
    return [v for v in svc.get("volumes", []) if not v.startswith(("./", "$"))]


class TestCompose:
    def test_only_the_ca_service_mounts_the_root_key(self, rendered):
        services = json.loads(rendered.files["compose.yaml"])["services"]
        holders = sorted(name for name, svc in services.items() if any(v.startswith("gs-ca:") for v in _mounts(svc)))
        assert holders == ["ca"]
        assert not any(v.startswith("gs-certs") for svc in services.values() for v in _mounts(svc))

    def test_each_service_mounts_only_what_it_needs_read_only(self, rendered):
        services = json.loads(rendered.files["compose.yaml"])["services"]
        assert "gs-tls-web:/certs:ro" in services["webfarm"]["volumes"]
        assert "gs-tls-threat:/certs:ro" in services["threat"]["volumes"]
        assert "gs-trust:/certs:ro" in services["npc"]["volumes"]
        assert "gs-trust:/certs:ro" in services["probe"]["volumes"]
        for name in ("webfarm", "threat", "npc", "probe"):
            certs = [v for v in _mounts(services[name]) if v.endswith(":/certs:ro")]
            assert len(certs) == 1, name

    def test_no_threat_infra_means_no_threat_tls_volume(self):
        out = config.render(parse_manifest(fixture.t0_manifest_doc()),
                            config.BlockParams(trust_ca=True, threat_infra=False))
        compose = json.loads(out.files["compose.yaml"])
        assert "gs-tls-threat" not in compose["volumes"]
        assert all(line.endswith(" web") for line in out.files["ca/zones.txt"].splitlines())


def _openssl() -> str | None:
    path = shutil.which("openssl")
    if not path:
        return None
    version = subprocess.run([path, "version"], capture_output=True, text=True).stdout
    return path if version.startswith("OpenSSL 3") else None


needs_openssl = pytest.mark.skipif(_openssl() is None or shutil.which("awk") is None,
                                   reason="needs OpenSSL 3 and awk")


def _run(tmp: Path, script: str, zones: str) -> None:
    (tmp / "ca").mkdir(exist_ok=True)
    (tmp / "zones.txt").write_text(zones)
    (tmp / "make-certs.sh").write_text(script)
    env = {**os.environ, "GS_CA_DIR": str(tmp / "ca"), "GS_CA_OUT": str(tmp / "out"),
           "GS_CA_ZONES": str(tmp / "zones.txt"), "GS_OVERLAY_DIR": str(tmp / "overlay"),
           "GS_CA_KEY_GROUP": ""}  # no chown outside the container
    subprocess.run(["sh", str(tmp / "make-certs.sh")], env=env, check=True, capture_output=True, text=True)


def _x509(cert: Path, *args: str) -> str:
    return subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", *args], check=True,
                          capture_output=True, text=True).stdout


def _verify(root: Path, cert: Path) -> bool:
    return subprocess.run(["openssl", "verify", "-CAfile", str(root), str(cert)], capture_output=True).returncode == 0


ZONES = "default web\nmaplewire-news.com web\ngs-infra.net web\ndefault threat\nupdate-cdn-sync.net threat\n"


@needs_openssl
class TestScript:
    def test_the_root_key_is_private_and_handed_to_nobody(self, rendered, tmp_path):
        _run(tmp_path, rendered.files["ca/make-certs.sh"], ZONES)
        assert (tmp_path / "ca/root.key").stat().st_mode & 0o777 == 0o600
        handed = {str(p.relative_to(tmp_path / "out")) for p in (tmp_path / "out").rglob("*") if p.is_file()}
        assert handed == {
            "web/default.crt", "web/default.key", "web/maplewire-news.com.crt", "web/maplewire-news.com.key",
            "web/gs-infra.net.crt", "web/gs-infra.net.key",
            "threat/default.crt", "threat/default.key", "threat/update-cdn-sync.net.crt",
            "threat/update-cdn-sync.net.key",
            "trust/root.crt",
        }
        for key in (tmp_path / "out").rglob("*.key"):
            assert key.stat().st_mode & 0o077 == 0, key  # outside the container: owner only
        assert (tmp_path / "overlay/sites/pki.gs-infra.net/root.crt").read_bytes() == (tmp_path / "ca/root.crt").read_bytes()

    def test_the_root_is_name_constrained_and_short_lived(self, rendered, tmp_path):
        _run(tmp_path, rendered.files["ca/make-certs.sh"], ZONES)
        root = tmp_path / "ca/root.crt"
        text = _x509(root, "-text")
        assert "X509v3 Name Constraints: critical" in text
        for name in ("DNS:maplewire-news.com", "DNS:gs-infra.net", "DNS:update-cdn-sync.net", "DNS:greyspace.invalid"):
            assert name in text
        assert "pathlen:0" in text
        days = subprocess.run(["openssl", "x509", "-in", str(root), "-noout", "-checkend", str(826 * 86400)],
                              capture_output=True).returncode
        assert days != 0  # expires within 826 days
        leaf = tmp_path / "out/web/maplewire-news.com.crt"
        assert _verify(root, leaf)
        assert subprocess.run(["openssl", "x509", "-in", str(leaf), "-noout", "-checkend", str(398 * 86400)],
                              capture_output=True).returncode != 0

    def test_a_certificate_outside_the_zones_does_not_verify(self, rendered, tmp_path):
        """Even with the root key, a name outside the stack's zones is not trusted."""
        _run(tmp_path, rendered.files["ca/make-certs.sh"], ZONES)
        ca = tmp_path / "ca"
        subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(tmp_path / "e.key")],
                       check=True, capture_output=True)
        subprocess.run(["openssl", "req", "-new", "-key", str(tmp_path / "e.key"), "-subj", "/CN=bank.example.com",
                        "-out", str(tmp_path / "e.csr")], check=True, capture_output=True)
        (tmp_path / "e.ext").write_text("subjectAltName=DNS:bank.example.com\nbasicConstraints=CA:FALSE\n")
        subprocess.run(["openssl", "x509", "-req", "-in", str(tmp_path / "e.csr"), "-CA", str(ca / "root.crt"),
                        "-CAkey", str(ca / "root.key"), "-CAcreateserial", "-days", "30", "-extfile",
                        str(tmp_path / "e.ext"), "-out", str(tmp_path / "e.crt")], check=True, capture_output=True)
        assert not _verify(ca / "root.crt", tmp_path / "e.crt")

    def test_a_rerun_keeps_the_root_and_a_zone_change_remakes_it(self, rendered, tmp_path):
        script = rendered.files["ca/make-certs.sh"]
        _run(tmp_path, script, ZONES)
        first = (tmp_path / "ca/root.crt").read_bytes()
        leaf = (tmp_path / "out/web/gs-infra.net.crt").read_bytes()
        _run(tmp_path, script, ZONES)
        assert (tmp_path / "ca/root.crt").read_bytes() == first
        assert (tmp_path / "out/web/gs-infra.net.crt").read_bytes() == leaf
        _run(tmp_path, script, ZONES.replace("update-cdn-sync.net threat\n", ""))
        root = tmp_path / "ca/root.crt"
        assert root.read_bytes() != first and "update-cdn-sync.net" not in _x509(root, "-text")
        assert not (tmp_path / "out/threat/update-cdn-sync.net.crt").exists()  # stale zones are taken away
        assert _verify(root, tmp_path / "out/web/gs-infra.net.crt")  # leaves reissued under the new root

    def test_an_unknown_server_in_zones_txt_fails(self, rendered, tmp_path):
        with pytest.raises(subprocess.CalledProcessError):
            _run(tmp_path, rendered.files["ca/make-certs.sh"], "default web\nexample.net mail\n")
