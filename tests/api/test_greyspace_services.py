"""Greyspace slices 4-8 in the stack generator: mail, NTP, HTTPS/CA, NPC traffic, the host
render, bin/gs breadcrumbs and corpus ingest (ADR 0007). Pure Python; the CI ``greyspace``
job runs the same stack for real (greyspace/scripts/check-t0.sh)."""

from __future__ import annotations

import base64
import gzip
import importlib.util
import io
import json
from pathlib import Path

import pytest
from app.greyspace import config, fixture, ingest, npc
from app.greyspace.manifest import parse_manifest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def t0():
    return parse_manifest(fixture.t0_manifest_doc())


def _compose(out) -> dict:
    return json.loads(out.files["compose.yaml"])


class TestServices:
    def test_every_image_is_pinned_by_digest(self, t0):
        out = config.render(t0, config.BlockParams(npc_profile="office-day"))
        for name, svc in _compose(out)["services"].items():
            image = svc["image"]
            if image.startswith("truenorth/"):
                assert "build" in svc, name  # built from greyspace/images (FROM pinned by digest)
            else:
                assert "@sha256:" in image, name
        for dockerfile in (ROOT / "greyspace/images").glob("*/Dockerfile"):
            froms = [ln for ln in dockerfile.read_text().splitlines() if ln.startswith("FROM ")]
            assert froms and all("@sha256:" in ln for ln in froms), dockerfile

    def test_mail_has_mx_for_every_site_zone_and_webmail_names(self, t0):
        out = config.render(t0, config.BlockParams())
        assert "@ IN MX 10 mail.gs-infra.net." in out.files["dns/auth/zones/db.quillmail.com"]
        assert "@ IN MX 10 mail.gs-infra.net." in out.files["dns/auth/zones/db.maplewire-news.com"]
        assert "webmail IN A 198.18.0.25" in out.files["dns/auth/zones/db.quillmail.com"]
        infra = out.files["dns/auth/zones/db.gs-infra.net"]
        for line in ("mail IN A 198.18.0.25", "ntp IN A 198.18.0.123", "pki IN A 198.18.0.80", "intel IN A 198.18.0.80"):
            assert line in infra
        mail = _compose(out)["services"]["mail"]
        assert mail["environment"]["MP_SMTP_BIND_ADDR"] == "0.0.0.0:25"
        assert mail["networks"]["gs-public"]["ipv4_address"] == "198.18.0.25"
        assert out.summary["infra_names"]["mail.gs-infra.net"] == "198.18.0.25"

    def test_https_comes_from_a_greyspace_ca_with_a_certificate_per_zone(self, t0):
        out = config.render(t0, config.BlockParams(trust_ca=True))
        zones = out.files["ca/zones.txt"].split()
        assert zones[0] == "default" and "maplewire-news.com" in zones and "gs-infra.net" in zones
        assert "update-cdn-sync.net" in zones  # threat domains get certificates too
        conf = out.files["web/nginx.conf"]
        assert "listen 443 ssl" in conf and "ssl_certificate /certs/$gs_cert.crt;" in conf
        assert "www.maplewire-news.com maplewire-news.com;" in conf
        web = _compose(out)["services"]["webfarm"]
        assert "gs-certs:/certs:ro" in web["volumes"]
        assert web["depends_on"] == {"ca": {"condition": "service_completed_successfully"}}
        assert out.summary["https"] is True

    def test_without_trust_ca_there_is_no_ca_and_no_tls(self, t0):
        out = config.render(t0, config.BlockParams(trust_ca=False))
        assert "ca" not in _compose(out)["services"] and "ca/zones.txt" not in out.files
        assert "listen 443" not in out.files["web/nginx.conf"]
        assert "pki IN A" not in out.files["dns/auth/zones/db.gs-infra.net"]

    def test_ntp_serves_its_own_clock_only(self, t0):
        out = config.render(t0, config.BlockParams())
        assert "local stratum 3" in out.files["ntp/chrony.conf"] and "server " not in out.files["ntp/chrony.conf"]
        assert _compose(out)["services"]["ntp"]["command"][:3] == ["chronyd", "-d", "-x"]

    def test_the_corpus_may_not_use_greyspaces_own_zone(self):
        doc = fixture.t0_manifest_doc()
        doc["sites"][0]["aliases"] = ["shop.gs-infra.net"]
        doc["sites"][0]["fqdn"] = "gs-infra.net"
        with pytest.raises(config.ConfigError, match="gs-infra.net is Greyspace's own zone"):
            config.render(parse_manifest(doc), config.BlockParams())


class TestNpc:
    def test_off_means_no_npc_service(self, t0):
        assert "npc" not in _compose(config.render(t0, config.BlockParams()))["services"]

    @pytest.mark.parametrize("profile", ["office-day", "quiet-night"])
    def test_a_profile_runs_the_agent_over_the_selected_sites(self, t0, profile):
        out = config.render(t0, config.BlockParams(npc_profile=profile, site_packs=("news", "search", "webmail")))
        svc = _compose(out)["services"]["npc"]
        assert svc["image"] == npc.IMAGE and svc["dns"] == ["198.18.0.53"]
        assert "python3 /gs/npc.py /gs/config.json" in svc["command"][-1]
        cfg = json.loads(out.files["npc/config.json"])
        assert cfg["profile"] == profile and cfg["mail_host"] == "mail.gs-infra.net"
        assert {s["category"] for s in cfg["sites"]} <= {"news", "search", "webmail"}
        assert out.files["npc/npc.py"] == npc.agent_source()

    def test_the_agent_marks_its_traffic(self):
        assert "GreyspaceNPC/1" in npc.agent_source()
        assert set(config.NPC_PROFILES) == {"off", "office-day", "quiet-night"}


class TestHostRender:
    def test_host_mode_is_a_routed_bridge_without_a_probe(self, t0):
        out = config.render(t0, config.BlockParams(), host=True, project="greyspace")
        compose = _compose(out)
        assert "probe" not in compose["services"]
        net = compose["networks"]["gs-public"]
        assert net["driver_opts"] == {"com.docker.network.bridge.name": "gs-public0",
                                      "com.docker.network.bridge.gateway_mode_ipv4": "routed"}
        gs = json.loads(out.files["gs.json"])
        assert gs["host"] is True and gs["first_router"] == "198.18.0.2"
        assert gs["isp_prefixes"] == ["198.18.0.0/16", "198.19.0.0/16"]


def _load_gs(stack: Path):
    loader = importlib.machinery.SourceFileLoader("gs_cli_under_test", str(stack / "bin" / "gs"))
    spec = importlib.util.spec_from_loader("gs_cli_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STACK = stack
    return mod


@pytest.fixture
def stack(tmp_path, t0):
    out = config.render(t0, config.BlockParams(), project="gs-unit")
    for rel, text in out.files.items():
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    return tmp_path


class TestGsCrumbs:
    def test_web_and_feed_crumbs_go_through_the_web_farm_dns_into_the_zone(self, stack, monkeypatch):
        gs = _load_gs(stack)
        calls = []
        monkeypatch.setattr(gs, "in_webfarm", lambda script, stdin=None, check=True: calls.append((script, stdin)))
        payload = {"exercise": "ex-1", "crumbs": [
            {"id": "note", "kind": "web", "site": "www.maplewire-news.com", "path": "/n/a.txt",
             "content_b64": base64.b64encode(b"GS-ABC").decode()},
            {"id": "txt", "kind": "dns", "name": "crumb.maplewire-news.com", "type": "TXT", "value": "GS-ABC"},
            {"id": "ioc", "kind": "threat_feed", "type": "domain", "indicator": "bad.example.net", "note": "x"},
        ]}
        zone = stack / "dns/auth/zones/db.maplewire-news.com"
        serial = int(zone.read_text().split("SOA")[1].split()[2])
        planted = gs.plant(payload)
        assert [p["id"] for p in planted] == ["note", "txt", "ioc"]
        web_script, web_body = calls[0]
        assert "/srv/overlay/sites/maplewire-news.com/n/a.txt" in web_script and web_body == b"GS-ABC"
        text = zone.read_text()
        assert 'crumb IN TXT "GS-ABC" ; gs-crumb:ex-1:txt' in text
        assert int(text.split("SOA")[1].split()[2]) == serial + 1
        assert calls[1][1] == b"domain,bad.example.net,x # gs-crumb:ex-1:ioc\n"
        assert len(gs.load_ledger()) == 3

        calls.clear()
        removed = gs.remove("ex-1")
        assert len(removed) == 3 and gs.load_ledger() == []
        assert "gs-crumb:ex-1:txt" not in zone.read_text()
        assert any("rm -f" in c[0] for c in calls) and any("sed -i" in c[0] for c in calls)

    @pytest.mark.parametrize("crumb, why", [
        ({"id": "a", "kind": "web", "site": "nowhere.com", "path": "/x", "content_b64": ""}, "not a site"),
        ({"id": "a", "kind": "web", "site": "maplewire-news.com", "path": "/../../etc/x", "content_b64": ""}, "path"),
        ({"id": "a", "kind": "dns", "name": "x.unknown-zone.com", "type": "TXT", "value": "v"}, "no Greyspace zone"),
        ({"id": "a", "kind": "dns", "name": "x.maplewire-news.com", "type": "TXT", "value": 'v"; rm'}, "TXT value"),
        ({"id": "a", "kind": "threat_feed", "type": "domain", "indicator": "a\nb"}, "single-line"),
        ({"id": "a b", "kind": "web"}, "crumb id"),
    ])
    def test_unsafe_crumbs_are_refused(self, stack, monkeypatch, crumb, why):
        gs = _load_gs(stack)
        monkeypatch.setattr(gs, "in_webfarm", lambda *a, **k: pytest.fail("nothing may run"))
        with pytest.raises(gs.GsError, match=why):
            gs.plant({"exercise": "ex-1", "crumbs": [crumb]})

    def test_corpus_verify_checks_every_checksum(self, stack, tmp_path, capsys):
        gs = _load_gs(stack)
        corpus = tmp_path / "c"
        (corpus / "sites/a.com").mkdir(parents=True)
        (corpus / "sites/a.com/index.html").write_bytes(b"hi")
        (corpus / "manifest.json").write_text(json.dumps({"tier": "t1", "version": "v", "sites": [{}]}))
        import hashlib

        (corpus / "checksums.sha256").write_text(f"{hashlib.sha256(b'hi').hexdigest()}  sites/a.com/index.html\n")
        assert gs.main(["corpus", "verify", str(corpus)]) == 0
        (corpus / "sites/a.com/index.html").write_bytes(b"changed")
        assert gs.main(["corpus", "verify", str(corpus)]) == 1


class TestIngest:
    def _warc(self, tmp_path: Path, records: list[tuple[str, str, bytes]]) -> Path:
        path = tmp_path / "c.warc.gz"
        with path.open("wb") as fh:
            for rtype, uri, block in records:
                ingest.write_record(fh, rtype, uri, block, date="2026-10-07T00:00:00Z", content_type="application/http")
        return path

    def test_responses_become_site_files_and_the_rest_is_skipped(self, tmp_path):
        warc = self._warc(tmp_path, [
            ("request", "https://a.example.com/", b"GET / HTTP/1.1\r\n\r\n"),
            ("response", "https://a.example.com/", ingest.http_response(b"<h1>home</h1>", "text/html")),
            ("response", "https://a.example.com/docs/", ingest.http_response(b"docs", "text/html")),
            ("response", "https://a.example.com/x/../../../etc/passwd", ingest.http_response(b"no", "text/plain")),
            ("response", "https://a.example.com/missing", b"HTTP/1.1 404 Not Found\r\n\r\n"),
            ("response", "ftp://a.example.com/f", ingest.http_response(b"no", "text/plain")),
        ])
        out = tmp_path / "corpus"
        res = ingest.extract([warc], out)
        assert (out / "sites/a.example.com/index.html").read_bytes() == b"<h1>home</h1>"
        assert (out / "sites/a.example.com/docs/index.html").read_bytes() == b"docs"
        assert (out / "sites/a.example.com/etc/passwd").read_bytes() == b"no"  # normalised inside the site
        assert not (tmp_path / "etc").exists()
        assert res.stored == 3 and res.skipped == {"warc-type request": 1, "http status 404": 1,
                                                   "unsafe or unsupported URL": 1}

    def test_plain_warc_files_are_read_too(self, tmp_path):
        gz = self._warc(tmp_path, [("response", "http://b.example.org/a.css", ingest.http_response(b"x{}", "text/css"))])
        plain = tmp_path / "c.warc"
        plain.write_bytes(gzip.decompress(gz.read_bytes()))
        assert ingest.extract([plain], tmp_path / "o").stored == 1

    def test_the_report_counts_duplicates(self, tmp_path):
        for rel, data in (("a.com/x.css", b"same"), ("b.com/y.css", b"same"), ("b.com/index.html", b"other")):
            p = tmp_path / "sites" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        rep = ingest.report(tmp_path)
        assert rep["files"] == 3 and rep["bytes"] == 13 and rep["duplicate_bytes"] == 4
        assert rep["duplicate_groups"] == 1 and rep["sites"]["b.com"] == {"files": 2, "bytes": 9}

    def test_target_path_rejects_what_cannot_be_stored(self):
        assert ingest.target_path("https://A.Example.com/p/q.html?x=1") == ("a.example.com", "p/q.html")
        assert ingest.target_path("https://localhost/") is None
        assert ingest.target_path("https://a.com/%00x") is None

    def test_read_records_reads_back_what_write_record_wrote(self):
        buf = io.BytesIO()
        ingest.write_record(buf, "response", "https://a.com/", b"BODY", date="d", content_type="t")
        [rec] = list(ingest.read_records(io.BytesIO(gzip.decompress(buf.getvalue()))))
        assert rec.type == "response" and rec.uri == "https://a.com/" and rec.block == b"BODY"
