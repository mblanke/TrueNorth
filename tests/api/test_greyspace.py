"""Greyspace (ADR 0007): manifest parsing, config generation, and the range API.

The manifest and the generator are what the control plane and the runtime stack share
(greyspace/scripts/corpus.py imports them), so the DNS delegation chain, the routers'
announcements and the web farm's virtual hosts are pinned here without Docker; the CI
job ``greyspace`` runs the generated stack for real (greyspace/scripts/check-t0.sh).
"""

from __future__ import annotations

import copy
import ipaddress
import json
import uuid

import pytest
from app.greyspace import config, fixture, service
from app.greyspace.manifest import TIERS, ManifestError, parse_manifest, valid_fqdn, zone_of

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


def _doc() -> dict:
    return copy.deepcopy(fixture.t0_manifest_doc())


def _errors(doc) -> list[str]:
    with pytest.raises(ManifestError) as exc:
        parse_manifest(doc)
    return exc.value.errors


# ── Manifest ────────────────────────────────────────────────────────────────
class TestManifest:
    def test_t0_fixture_is_a_valid_manifest_within_its_cap(self):
        m = parse_manifest(_doc())
        assert m.tier == "t0" and len(m.sites) == 20
        assert len(m.threat_domains) == 2
        assert 0 < m.total_bytes <= TIERS["t0"].cap_bytes
        assert set(m.categories) == {"news", "search", "social", "webmail", "video", "gov", "dev", "shopping"}

    def test_every_tier_shares_one_format_and_only_full_is_uncapped(self):
        assert set(TIERS) == {"t0", "t1", "t2", "full"}
        assert TIERS["t1"].cap_bytes == 5 * 10**9
        assert [t for t in TIERS.values() if t.cap_bytes is None] == [TIERS["full"]]

    def test_reports_every_problem_not_just_the_first(self):
        doc = _doc()
        doc["format"] = "something-else"
        doc["tier"] = "t9"
        doc["sites"][0]["fqdn"] = "Not A Domain"
        doc["sites"][1]["ip"] = "10.0.0.1"
        errors = _errors(doc)
        assert any(e.startswith("format") for e in errors)
        assert any(e.startswith("tier") for e in errors)
        assert any("sites[0].fqdn" in e for e in errors)
        assert any("sites[1].ip" in e and "outside every ISP prefix" in e for e in errors)

    def test_duplicate_names_across_sites_aliases_and_threats_are_refused(self):
        doc = _doc()
        doc["threat_actors"][0]["domains"][0]["fqdn"] = doc["sites"][0]["fqdn"]
        assert any("declared twice" in e for e in _errors(doc))

    def test_alias_must_live_in_the_sites_zone(self):
        doc = _doc()
        doc["sites"][0]["aliases"] = ["www.elsewhere.com"]
        assert any("aliases" in e and "is not in" in e for e in _errors(doc))

    @pytest.mark.parametrize("path", ["/etc/passwd", "sites/../../etc", "elsewhere/x", "", None])
    def test_site_paths_stay_inside_sites(self, path):
        doc = _doc()
        doc["sites"][0]["path"] = path
        assert any("sites[0].path" in e for e in _errors(doc))

    def test_overlapping_isp_prefixes_and_duplicate_asn_are_refused(self):
        doc = _doc()
        doc["isps"].append({"name": "dup", "asn": 64501, "prefix": "198.18.128.0/17"})
        errors = _errors(doc)
        assert any("overlaps" in e for e in errors) and any("used twice" in e for e in errors)

    def test_tier_cap_is_enforced(self):
        doc = _doc()
        doc["sites"][0]["bytes"] = TIERS["t0"].cap_bytes + 1
        assert any("exceeds the t0 cap" in e for e in _errors(doc))

    def test_threat_role_must_be_known(self):
        doc = _doc()
        doc["threat_actors"][0]["domains"][0]["role"] = "ransom"
        assert any(".role" in e for e in _errors(doc))

    def test_video_must_belong_to_a_site(self):
        doc = _doc()
        doc["videos"][0]["site"] = "nowhere.com"
        assert any("videos[0].site" in e for e in _errors(doc))

    @pytest.mark.parametrize("doc", [None, [], "manifest"])
    def test_non_object_is_refused(self, doc):
        assert _errors(doc)

    def test_names(self):
        assert valid_fqdn("maplewire-news.com") and valid_fqdn("a.b.gc-sim.ca")
        assert not valid_fqdn("localhost") and not valid_fqdn("UPPER.com") and not valid_fqdn("x.123")
        assert not valid_fqdn("-bad.com") and not valid_fqdn("bad-.com")
        assert zone_of("revenue-agency.gc-sim.ca") == "gc-sim.ca"


# ── Config generation ───────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def t0():
    return parse_manifest(fixture.t0_manifest_doc())


def _records(text: str) -> list[list[str]]:
    return [line.split() for line in text.splitlines() if line and not line.startswith("$")]


class TestConfig:
    def test_renders_a_complete_stack(self, t0):
        out = config.render(t0, config.BlockParams())
        compose = json.loads(out.files["compose.yaml"])
        assert set(compose["services"]) == {
            "isp-a", "isp-b", "dns-root", "dns-tld", "dns-auth", "resolver", "webfarm", "threat", "probe",
        }
        assert compose["services"]["probe"]["profiles"] == ["probe"]
        assert "probe" not in out.summary["services"]
        net = compose["networks"]["gs-public"]
        assert net["internal"] is True  # a closed range: no route to the real internet
        assert net["ipam"]["config"] == [{"subnet": "198.18.0.0/15", "ip_range": "198.18.0.0/24"}]

    def test_corpus_is_mounted_read_only_and_overlay_read_write(self, t0):
        out = config.render(t0, config.BlockParams(), corpus_dir="/mnt/corpus")
        vols = json.loads(out.files["compose.yaml"])["services"]["webfarm"]["volumes"]
        assert "/mnt/corpus:/srv/corpus:ro" in vols
        assert "gs-overlay:/srv/overlay" in vols
        conf = out.files["web/nginx.conf"]
        assert conf.index("/overlay/$gs_site$uri") < conf.index("/corpus/$gs_site$uri")

    def test_dns_delegation_chain_root_tld_auth(self, t0):
        out = config.render(t0, config.BlockParams())
        plan = config.address_plan(t0)
        root = _records(out.files["dns/root/db.root"])
        assert ["com.", "IN", "NS", "a.gtld-servers.net."] in root
        assert ["a.gtld-servers.net.", "IN", "A", str(plan.tld_dns)] in root
        com = _records(out.files["dns/tld/zones/db.com"])
        assert ["maplewire-news", "IN", "NS", "ns1.maplewire-news.com."] in com
        assert ["ns1.maplewire-news", "IN", "A", str(plan.auth_dns)] in com
        zone = _records(out.files["dns/auth/zones/db.maplewire-news.com"])
        assert ["@", "IN", "A", "198.18.10.11"] in zone and ["www", "IN", "A", "198.18.10.11"] in zone
        # A site below its registrable domain is a record in that domain's zone.
        gc = _records(out.files["dns/auth/zones/db.gc-sim.ca"])
        assert any(r[0] == "revenue-agency" and r[2] == "A" for r in gc)
        hints = out.files["resolver/root.hints"]
        assert str(plan.root_dns) in hints and "module-config: \"iterator\"" in out.files["resolver/unbound.conf"]

    def test_every_isp_originates_its_prefix_and_peers_with_every_other(self, t0):
        out = config.render(t0, config.BlockParams())
        a, b = out.files["frr/isp-a/frr.conf"], out.files["frr/isp-b/frr.conf"]
        assert "router bgp 64501" in a and "neighbor 198.18.0.3 remote-as 64502" in a
        assert "router bgp 64502" in b and "neighbor 198.18.0.2 remote-as 64501" in b
        assert "ip route 198.18.0.0/16 198.18.0.80" in a and "ip route 198.19.0.0/16 198.18.0.80" in b
        # The infrastructure segment stays on-link, or the BGP session would follow the prefix route.
        assert "ip route 198.18.0.0/24 eth0" in a
        # Threat addresses are routed to the threat stub by the ISP that owns them.
        assert "ip route 198.19.250.10/32 198.18.0.66" in b and "198.19.250.10" not in a
        assert "bgpd=yes" in out.files["frr/isp-a/daemons"]

    def test_site_packs_filter_sites_zones_and_vhosts(self, t0):
        out = config.render(t0, config.BlockParams(site_packs=("news",)))
        assert out.summary["site_packs"] == ["news"]
        assert "db.maplewire-news.com" in " ".join(out.files)
        assert not any(p.endswith("db.codehub.dev") for p in out.files)
        assert "codehub.dev" not in out.files["web/nginx.conf"]

    def test_threat_infra_off_drops_the_stub_and_its_dns_and_routes(self, t0):
        out = config.render(t0, config.BlockParams(threat_infra=False))
        assert "threat" not in json.loads(out.files["compose.yaml"])["services"]
        assert not any(p.startswith("threat/") for p in out.files)
        assert not any(p.endswith("db.update-cdn-sync.net") for p in out.files)
        assert "198.19.250.10" not in out.files["frr/isp-b/frr.conf"]
        assert out.summary["threat_domains"] == []

    def test_web_farm_holds_every_site_address(self, t0):
        out = config.render(t0, config.BlockParams())
        held = set(out.files["web/addresses.txt"].split())
        assert held == {str(s.ip) for s in t0.sites}

    def test_is_deterministic(self, t0):
        assert config.render(t0, config.BlockParams()).files == config.render(t0, config.BlockParams()).files

    @pytest.mark.parametrize(
        ("params", "needle"),
        [
            (config.BlockParams(corpus_tier="t1"), "does not match"),
            (config.BlockParams(site_packs=("knitting",)), "not in this corpus"),
            (config.BlockParams(site_packs=()), "at least one pack"),
            (config.BlockParams(public_prefix="198.18.0.0/16"), "does not cover"),
            (config.BlockParams(public_prefix="not-a-cidr"), "not an IPv4 network"),
            (config.BlockParams(npc_profile="rave"), "npc_profile"),
        ],
    )
    def test_invalid_blocks_are_refused_with_the_reason(self, t0, params, needle):
        assert any(needle in e for e in config.validate(t0, params))
        with pytest.raises(config.ConfigError, match=needle):
            config.render(t0, params)

    def test_a_wider_public_prefix_is_accepted(self, t0):
        assert config.validate(t0, config.BlockParams(public_prefix="198.16.0.0/12")) == []

    def test_sites_may_not_use_the_infrastructure_segment(self):
        doc = _doc()
        doc["sites"][0]["ip"] = "198.18.0.53"
        m = parse_manifest(doc)
        assert any("infrastructure segment" in e for e in config.validate(m, config.BlockParams()))

    def test_far_apart_isps_are_refused(self):
        doc = _doc()
        doc["isps"][1]["prefix"] = "45.0.0.0/16"
        for s in doc["sites"]:
            s["ip"] = "198.18.10.11"
        for d in doc["threat_actors"][0]["domains"]:
            d["ip"] = "198.18.250.10"
        m = parse_manifest(doc)
        assert any("too far apart" in e for e in config.validate(m, config.BlockParams()))

    def test_infrastructure_addresses_are_inside_the_first_isp(self, t0):
        plan = config.address_plan(t0)
        first = t0.isps[0].prefix
        for ip in (plan.root_dns, plan.tld_dns, plan.auth_dns, plan.resolver, plan.webfarm, plan.threat):
            assert ip in plan.infra and ip in first
        assert plan.public == ipaddress.IPv4Network("198.18.0.0/15")


# ── Runtime builder (greyspace/scripts/corpus.py) ───────────────────────────
@pytest.fixture(scope="module")
def corpus_cli():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "greyspace" / "scripts" / "corpus.py"
    spec = importlib.util.spec_from_file_location("greyspace_corpus_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRuntimeBuilder:
    def test_t0_build_verify_render(self, corpus_cli, tmp_path):
        corpus, stack = tmp_path / "corpus", tmp_path / "stack"
        assert corpus_cli.main(["t0", "--out", str(corpus), "--no-video"]) == 0
        doc = json.loads((corpus / "manifest.json").read_text())
        assert doc["videos"] == [] and len(doc["sites"]) == 20
        assert (corpus / "sites" / "maplewire-news.com" / "index.html").is_file()
        lines = (corpus / "checksums.sha256").read_text().splitlines()
        assert len(lines) == 100 and all(len(line.split("  ")[0]) == 64 for line in lines)
        assert corpus_cli.main(["verify", "--root", str(corpus)]) == 0
        assert corpus_cli.main(["render", "--corpus", str(corpus), "--out", str(stack), "--packs", "news"]) == 0
        compose = json.loads((stack / "compose.yaml").read_text())
        assert f"{corpus}:/srv/corpus:ro" in compose["services"]["webfarm"]["volumes"]
        assert (stack / "web" / "entrypoint.sh").stat().st_mode & 0o111

    def test_verify_catches_a_changed_file(self, corpus_cli, tmp_path):
        corpus = tmp_path / "corpus"
        corpus_cli.main(["t0", "--out", str(corpus), "--no-video"])
        (corpus / "sites" / "pastebox.io" / "index.html").write_text("tampered")
        assert corpus_cli.main(["verify", "--root", str(corpus)]) == 1

    def test_index_builds_a_manifest_for_a_mirrored_tree(self, corpus_cli, tmp_path):
        root = tmp_path / "sample"
        (root / "sites" / "en.wikipedia.org" / "wiki").mkdir(parents=True)
        (root / "sites" / "en.wikipedia.org" / "wiki" / "Canada.html").write_text("<p>Canada</p>")
        (root / "sites" / "not a domain").mkdir()
        seeds = tmp_path / "seeds.txt"
        seeds.write_text("# fqdn category licence source\nen.wikipedia.org reference CC-BY-SA-4.0 https://en.wikipedia.org/\n")
        rc = corpus_cli.main(["index", "--root", str(root), "--tier", "t1", "--version", "test", "--seeds", str(seeds)])
        assert rc == 0
        m = parse_manifest(json.loads((root / "manifest.json").read_text()))
        assert [s.fqdn for s in m.sites] == ["en.wikipedia.org"]
        assert m.sites[0].licence == "CC-BY-SA-4.0" and m.sites[0].bytes > 0
        assert m.sites[0].ip not in config.address_plan(m).infra

    def test_render_refuses_a_bad_block_with_exit_2(self, corpus_cli, tmp_path):
        corpus = tmp_path / "corpus"
        corpus_cli.main(["t0", "--out", str(corpus), "--no-video"])
        assert corpus_cli.main(["render", "--corpus", str(corpus), "--out", str(tmp_path / "s"), "--packs", "nope"]) == 2


# ── The range API ───────────────────────────────────────────────────────────
def _range(db_session, *, tenant=DEV_TENANT, state="created", template_yaml="name: plain\n"):
    from app.models import Range, Template

    tpl = Template(name=f"gs-tpl-{uuid.uuid4().hex[:6]}", version="1.0", yaml=template_yaml, tenant_id=tenant)
    db_session.add(tpl)
    db_session.flush()
    rng = Range(name="gs-range", template_id=tpl.id, tenant_id=tenant, state=state)
    db_session.add(rng)
    db_session.commit()
    return rng


def _as(role: str, tenant: str = DEV_TENANT):
    from app.auth import CurrentUser, get_current_user
    from app.main import app as fastapi_app
    from app.models import UserRole

    user = CurrentUser(id=str(uuid.uuid4()), email=f"{role}@example.test", display_name=role,
                       role=UserRole(role), tenant_id=tenant, keycloak_id=f"kc-{role}")
    fastapi_app.dependency_overrides[get_current_user] = lambda: user


class TestRangeApi:
    def test_not_attached_by_default(self, client, db_session):
        rng = _range(db_session)
        body = client.get(f"/ranges/{rng.id}/greyspace").json()
        assert body["attached"] is False and body["status"] == "not_attached"
        assert body["block"] is None and body["range_state"] == "created"

    def test_attach_with_no_body_uses_defaults_and_shows_configured(self, client, db_session):
        rng = _range(db_session)
        resp = client.put(f"/ranges/{rng.id}/greyspace")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["attached"] is True and body["status"] == "configured"
        assert body["block"]["corpus_tier"] == "t0" and body["block"]["site_packs"] is None
        assert body["corpus"]["available"] is True and body["corpus"]["sites"] == 20
        assert body["problems"] == []
        assert client.get(f"/ranges/{rng.id}/greyspace").json()["status"] == "configured"

    def test_attach_with_no_body_uses_the_templates_block(self, client, db_session):
        rng = _range(
            db_session,
            template_yaml="name: t\ngreyspace:\n  corpus_tier: t0\n  site_packs: [news]\n  threat_infra: false\n",
        )
        assert client.get(f"/ranges/{rng.id}/greyspace").json()["template_block"]["site_packs"] == ["news"]
        body = client.put(f"/ranges/{rng.id}/greyspace").json()
        assert body["block"]["site_packs"] == ["news"] and body["block"]["threat_infra"] is False

    def test_attach_replaces_and_records_an_audit_entry(self, client, db_session):
        from app.models import AuditLog

        rng = _range(db_session)
        client.put(f"/ranges/{rng.id}/greyspace", json={"site_packs": ["news"]})
        body = client.put(f"/ranges/{rng.id}/greyspace", json={"site_packs": ["dev"], "threat_infra": False}).json()
        assert body["block"]["site_packs"] == ["dev"]
        actions = [a.action for a in db_session.query(AuditLog).filter(AuditLog.resource_id == str(rng.id)).all()]
        assert actions.count("greyspace.attach") == 2

    def test_config_reflects_the_block(self, client, db_session):
        rng = _range(db_session)
        assert client.get(f"/ranges/{rng.id}/greyspace/config").status_code == 404
        client.put(f"/ranges/{rng.id}/greyspace", json={"site_packs": ["news", "dev"], "threat_infra": False})
        cfg = client.get(f"/ranges/{rng.id}/greyspace/config").json()
        assert cfg["corpus_tier"] == "t0" and cfg["corpus_version"] == fixture.VERSION
        assert cfg["site_packs"] == ["dev", "news"] and cfg["threat_domains"] == []
        assert "webfarm" in cfg["services"] and "threat" not in cfg["services"]
        assert cfg["address_plan"]["resolver"] == "198.18.0.53"
        assert "compose.yaml" in cfg["files"]

    def test_on_a_built_range_it_takes_effect_at_next_provision(self, client, db_session):
        rng = _range(db_session, state="ready")
        body = client.put(f"/ranges/{rng.id}/greyspace").json()
        assert body["status"] == "configured" and "next provision" in body["detail"]["note"]

    def test_detach(self, client, db_session):
        rng = _range(db_session)
        assert client.delete(f"/ranges/{rng.id}/greyspace").status_code == 404
        client.put(f"/ranges/{rng.id}/greyspace")
        assert client.delete(f"/ranges/{rng.id}/greyspace").status_code == 204
        assert client.get(f"/ranges/{rng.id}/greyspace").json()["attached"] is False

    @pytest.mark.parametrize(
        ("body", "status"),
        [
            ({"site_packs": ["knitting"]}, 422),
            ({"public_prefix": "198.18.0.0/16"}, 422),
            ({"corpus_tier": "t7"}, 422),
            ({"npc_profile": "rave"}, 422),
            ({"unknown_key": 1}, 422),
        ],
    )
    def test_invalid_blocks_are_422(self, client, db_session, body, status):
        rng = _range(db_session)
        resp = client.put(f"/ranges/{rng.id}/greyspace", json=body)
        assert resp.status_code == status, resp.text
        assert client.get(f"/ranges/{rng.id}/greyspace").json()["attached"] is False

    def test_problems_are_listed_in_the_422(self, client, db_session):
        rng = _range(db_session)
        detail = client.put(f"/ranges/{rng.id}/greyspace", json={"site_packs": ["knitting"]}).json()["detail"]
        assert any("knitting" in p for p in detail["problems"])

    def test_a_tier_the_control_plane_cannot_read_is_accepted_but_has_no_config(self, client, db_session):
        rng = _range(db_session)
        body = client.put(f"/ranges/{rng.id}/greyspace", json={"corpus_tier": "t2"}).json()
        assert body["status"] == "configured" and body["corpus"]["available"] is False
        assert client.get(f"/ranges/{rng.id}/greyspace/config").status_code == 409

    def test_a_mounted_manifest_is_used(self, client, db_session, tmp_path, monkeypatch):
        doc = _doc()
        doc["tier"], doc["version"] = "t1", "2026.10-mac"
        (tmp_path / "t1").mkdir()
        (tmp_path / "t1" / "manifest.json").write_text(json.dumps(doc))
        monkeypatch.setenv("GREYSPACE_MANIFEST_DIR", str(tmp_path))
        rng = _range(db_session)
        client.put(f"/ranges/{rng.id}/greyspace", json={"corpus_tier": "t1"})
        assert client.get(f"/ranges/{rng.id}/greyspace/config").json()["corpus_version"] == "2026.10-mac"

    def test_a_broken_mounted_manifest_reads_as_unavailable(self, tmp_path, monkeypatch):
        (tmp_path / "t1").mkdir()
        (tmp_path / "t1" / "manifest.json").write_text("{not json")
        monkeypatch.setenv("GREYSPACE_MANIFEST_DIR", str(tmp_path))
        assert service.load_manifest("t1") is None
        assert service.load_manifest("t0") is not None  # the built-in fixture still answers

    @pytest.mark.parametrize("state", ["provisioning", "destroying", "stopping", "starting"])
    def test_refused_while_the_range_is_changing(self, client, db_session, state):
        rng = _range(db_session, state=state)
        assert client.put(f"/ranges/{rng.id}/greyspace").status_code == 409
        assert client.delete(f"/ranges/{rng.id}/greyspace").status_code == 409

    def test_corpora_lists_every_tier(self, client):
        tiers = {c["tier"]: c for c in client.get("/greyspace/corpora").json()}
        assert set(tiers) == {"t0", "t1", "t2", "full"}
        assert tiers["t0"]["available"] is True and tiers["t0"]["threat_domains"] == 2
        assert tiers["full"]["cap_bytes"] is None

    def test_unknown_range_is_404(self, client):
        rid = uuid.uuid4()
        assert client.get(f"/ranges/{rid}/greyspace").status_code == 404
        assert client.put(f"/ranges/{rid}/greyspace").status_code == 404


class TestTenancyAndRbac:
    @pytest.fixture(autouse=True)
    def _restore(self):
        yield
        from app.auth import get_current_user
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides.pop(get_current_user, None)

    def test_another_tenants_range_is_404_on_every_route(self, client, db_session):
        rng = _range(db_session, tenant=OTHER_TENANT)
        assert client.get(f"/ranges/{rng.id}/greyspace").status_code == 404
        assert client.put(f"/ranges/{rng.id}/greyspace").status_code == 404
        assert client.delete(f"/ranges/{rng.id}/greyspace").status_code == 404
        assert client.get(f"/ranges/{rng.id}/greyspace/config").status_code == 404

    def test_an_admin_of_another_tenant_cannot_read_or_change_it(self, client, db_session):
        rng = _range(db_session)
        client.put(f"/ranges/{rng.id}/greyspace")
        _as("admin", OTHER_TENANT)
        assert client.get(f"/ranges/{rng.id}/greyspace").status_code == 404
        assert client.delete(f"/ranges/{rng.id}/greyspace").status_code == 404

    @pytest.mark.parametrize("role", ["student", "observer"])
    def test_read_only_roles_can_read_but_not_attach_or_detach(self, client, db_session, role):
        rng = _range(db_session)
        client.put(f"/ranges/{rng.id}/greyspace")
        _as(role)
        assert client.get(f"/ranges/{rng.id}/greyspace").status_code == 200
        assert client.get("/greyspace/corpora").status_code == 200
        assert client.put(f"/ranges/{rng.id}/greyspace").status_code == 403
        assert client.delete(f"/ranges/{rng.id}/greyspace").status_code == 403

    @pytest.mark.parametrize("role", ["instructor", "range_ops"])
    def test_range_editors_can_attach(self, client, db_session, role):
        rng = _range(db_session)
        _as(role)
        assert client.put(f"/ranges/{rng.id}/greyspace").status_code == 200

    def test_a_lab_session_range_is_never_changed_here(self, client, db_session, monkeypatch):
        from app.lab_sessions import service as lab_service

        rng = _range(db_session)
        monkeypatch.setattr(lab_service, "lab_range_ids", lambda db, ids: set(ids))
        assert client.put(f"/ranges/{rng.id}/greyspace").status_code == 409
        _as("student")
        assert client.get(f"/ranges/{rng.id}/greyspace").status_code == 404
