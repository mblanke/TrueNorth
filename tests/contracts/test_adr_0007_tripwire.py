"""ADR 0007 (docs/adr/0007-greyspace.md, "Greyspace: a simulated internet attached to a
range") pinned to the code, decision by decision.

Tripwire: every test's docstring quotes the ADR line it pins, and asserts the structure
or behaviour that line promises. Code that drifts from the ADR fails here. Read-only:
source text, in-process app objects and pure-stdlib rendering — no network, no live
services, no mocks of the units under test.
"""

from __future__ import annotations

import inspect
import ipaddress
import json
import os
import re
import uuid
from pathlib import Path

import pytest
from app import range_topology
from app.auth import CurrentUser
from app.greyspace import config, fixture, models, npc, service
from app.greyspace.manifest import FORMAT, TIERS, ManifestError, parse_manifest, zone_of
from app.greyspace.schemas import GreyspaceBlock
from app.models import Range, RangeState, Template, Tenant, UserRole
from app.rbac import Permission
from app.routers import greyspace as greyspace_router
from app.routers import ranges as ranges_router
from fastapi import HTTPException
from fastapi.routing import APIRoute
from scenario_engine.injectors import greyspace_breadcrumb
from worker import contracts as worker_contracts
from worker import greyspace as worker_greyspace
from worker import greyspace_host

ROOT = Path(__file__).resolve().parents[2]
# The dev principal tests/conftest.py seeds (app.auth's AUTH_DISABLED identity).
DEV_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture(scope="module")
def t0():
    return parse_manifest(fixture.t0_manifest_doc())


@pytest.fixture(scope="module")
def rendered(t0):
    return config.render(t0, config.BlockParams())


# ── Decision 1: one corpus layout and one manifest format for every tier ─────


def test_manifest_format_and_site_layout(t0):
    """ADR 0007, decision 1: "One corpus layout and one manifest format for every tier",
    format `greyspace-corpus/1`, with "manifest.json, checksums.sha256, and
    sites/<fqdn>/..." — a site outside `sites/` is refused."""
    assert FORMAT == "greyspace-corpus/1"
    doc = fixture.t0_manifest_doc()
    doc["format"] = "greyspace-corpus/2"
    with pytest.raises(ManifestError, match="greyspace-corpus/1"):
        parse_manifest(doc)
    doc = fixture.t0_manifest_doc()
    doc["sites"][0]["path"] = "etc/passwd"
    with pytest.raises(ManifestError, match="sites/"):
        parse_manifest(doc)
    # The bundled T0 fixture itself follows the layout.
    assert {zone_of(s.fqdn) for s in t0.sites} and all(s.path.startswith("sites/") for s in t0.sites)


def test_tier_table_matches_the_adr():
    """ADR 0007, decision 1 (the tier table): T0 ~100 MB "generated per build", T1 5 GB
    hard cap in `~/greyspace-corpus`, T2 ~50 GB and Full (no cap) on NetApp NFS —
    "Nothing large is committed; T0 is generated"."""
    assert set(TIERS) == {"t0", "t1", "t2", "full"}
    assert TIERS["t0"].cap_bytes == 100 * 10**6
    assert TIERS["t1"].cap_bytes == 5 * 10**9
    assert TIERS["t2"].cap_bytes == 50 * 10**9
    assert TIERS["full"].cap_bytes is None
    assert TIERS["t0"].location == "generated per build"
    assert TIERS["t1"].location == "~/greyspace-corpus"
    for tier in ("t2", "full"):
        assert TIERS[tier].location.startswith("NetApp volume") and "NFS" in TIERS[tier].location, tier


def test_tier_cap_is_enforced():
    """ADR 0007, decision 1: the tier sizes are hard caps — a corpus over its tier's cap
    is refused (T0's is 100 MB)."""
    doc = fixture.t0_manifest_doc()
    doc["sites"][0]["bytes"] = TIERS["t0"].cap_bytes + 1
    with pytest.raises(ManifestError, match="exceeds the t0 cap"):
        parse_manifest(doc)


# ── Decision 2: everything is generated from the manifest + block ────────────


def test_everything_is_generated_from_the_manifest(rendered):
    """ADR 0007, decision 2: "Everything is generated from the manifest + block" — FRR
    routing, CoreDNS, Unbound, an nginx web farm, a threat stub and compose, all pure
    stdlib output of config.render (no hand-built assets, light images only)."""
    files = rendered.files
    assert json.loads(files["gs.json"])["format"] == "greyspace-stack/1"
    assert json.loads(files["compose.yaml"])["services"]
    frr = sorted(p for p in files if p.startswith("frr/") and p.endswith("/frr.conf"))
    dns = sorted(p for p in files if p.startswith("dns/"))
    assert len(frr) == 2 and any("db.root" in p for p in dns) and "dns/auth/Corefile" in files
    assert "resolver/unbound.conf" in files and "resolver/root.hints" in files
    assert "web/nginx.conf" in files and any(p.startswith("threat/www/") for p in files)
    assert "ntp/chrony.conf" in files
    assert set(rendered.summary["services"]) == {
        "isp-a", "isp-b", "dns-root", "dns-tld", "dns-auth", "resolver", "webfarm", "threat", "mail", "ntp",
    }


def test_ebgp_full_mesh_and_originated_prefixes(rendered, t0):
    """ADR 0007, decision 2: FRR routers "one per ISP, eBGP full mesh, each originating
    its own prefix" — every router peers every other ISP's router and statics its prefix
    to the web farm."""
    plan = config.address_plan(t0)
    slugs = list(plan.routers)
    for n, isp in enumerate(t0.isps):
        conf = rendered.files[f"frr/{slugs[n]}/frr.conf"]
        assert f"router bgp {isp.asn}" in conf
        assert f"ip route {isp.prefix} {plan.webfarm}" in conf
        for m, peer in enumerate(t0.isps):
            if m != n:
                assert f"neighbor {plan.routers[slugs[m]]} remote-as {peer.asn}" in conf
        assert "redistribute static" in conf


def test_dns_delegation_chain(rendered, t0):
    """ADR 0007, decision 2: CoreDNS runs "a real delegation chain" — root delegates each
    TLD to the TLD server (with glue), the TLD delegates each zone to the authoritative
    server (with glue), and the Unbound resolver starts from the root hints."""
    plan = config.address_plan(t0)
    root = rendered.files["dns/root/db.root"]
    assert f"a.gtld-servers.net. IN A {plan.tld_dns}" in root
    for tld in rendered.summary["tlds"]:
        assert f"{tld}. IN NS a.gtld-servers.net." in root
        tld_zone = rendered.files[f"dns/tld/zones/db.{tld}"]
        for zone in rendered.summary["zones"]:
            if zone.split(".")[-1] != tld:
                continue
            label = zone.split(".")[0]
            assert f"{label} IN NS ns1.{zone}." in tld_zone
            assert f"ns1.{label} IN A {plan.auth_dns}" in tld_zone
            auth = rendered.files[f"dns/auth/zones/db.{zone}"]
            assert "@ IN NS ns1" in auth and f"ns1 IN A {plan.auth_dns}" in auth
    hints = rendered.files["resolver/root.hints"]
    assert f"a.root-servers.net.   3600000 IN A  {plan.root_dns}" in hints


# ── Decision 3: corpus read-only, per-range overlay first ────────────────────


def test_corpus_read_only_with_overlay_first(rendered):
    """ADR 0007, decision 3: "The corpus mounts read-only, a per-range overlay on top is
    read-write, nginx serves /overlay/... before /corpus/..."; the overlay mirrors the
    corpus layout (breadcrumbs plant into overlay/sites/...)."""
    webfarm = json.loads(rendered.files["compose.yaml"])["services"]["webfarm"]
    corpus_mounts = [v for v in webfarm["volumes"] if "corpus" in v]
    assert corpus_mounts and all(v.endswith(":ro") for v in corpus_mounts)
    assert "gs-overlay:/srv/overlay" in webfarm["volumes"]
    nginx = rendered.files["web/nginx.conf"]
    assert nginx.index("try_files /overlay/$gs_site$uri") < nginx.index("/corpus/$gs_site$uri")


# ── Decision 4: addressing ───────────────────────────────────────────────────


def test_infrastructure_segment_offsets(t0):
    """ADR 0007, decision 4: "the first ISP's first /24 is the infrastructure segment" —
    routers from .2, root DNS .10, TLD .11, authoritative .12, resolver .53, threat stub
    .66, web farm .80; the bridge itself is .1."""
    plan = config.address_plan(t0)
    infra = next(t0.isps[0].prefix.subnets(new_prefix=24))

    def offset(addr) -> int:
        return int(addr) - int(infra.network_address)

    assert offset(plan.gateway) == 1
    assert [offset(ip) for ip in plan.routers.values()] == [2, 3]
    for attr, want in [("root_dns", 10), ("tld_dns", 11), ("auth_dns", 12), ("mail", 25), ("resolver", 53),
                       ("threat", 66), ("webfarm", 80), ("ntp", 123)]:
        assert offset(getattr(plan, attr)) == want, attr


def test_isp_prefixes_share_one_slash_8(rendered, t0):
    """ADR 0007, decision 4: "ISP prefixes must share a /8" — prefixes spread wider are
    refused; the Docker network spans the supernet and is internal."""
    from app.greyspace.manifest import Isp, Manifest

    far = Manifest(
        tier="t0",
        version="tripwire",
        isps=(Isp("a", 64501, ipaddress.IPv4Network("198.18.0.0/16")),
              Isp("b", 64502, ipaddress.IPv4Network("45.0.0.0/16"))),
        sites=(),
    )
    with pytest.raises(config.ConfigError, match="one /8"):
        config.address_plan(far)
    plan = config.address_plan(t0)
    networks = json.loads(rendered.files["compose.yaml"])["networks"]
    net = next(n for n in networks.values() if n.get("ipam"))
    assert net["internal"] is True
    assert net["ipam"]["config"][0]["subnet"] == str(plan.public)


# ── Decision 5: the block, its row and its API ───────────────────────────────


def test_template_schema_has_a_greyspace_block():
    """ADR 0007, decision 5: the block is added "via the template's `greyspace:` key in
    scenario-engine/schemas/template.schema.json or PUT /ranges/{range_id}/greyspace"."""
    schema = json.loads((ROOT / "scenario-engine/schemas/template.schema.json").read_text(encoding="utf-8"))
    assert "greyspace" in schema["properties"]


def _permissions(route: APIRoute) -> set[Permission]:
    """The permissions the route's `require_permission` closure captured."""
    found: set[Permission] = set()
    for dep in route.dependant.dependencies:
        for cell in getattr(dep.call, "__closure__", None) or ():
            value = cell.cell_contents
            if isinstance(value, tuple) and value and all(isinstance(p, Permission) for p in value):
                found |= set(value)
    return found


def test_greyspace_api_routes_and_permissions():
    """ADR 0007, decision 5: "GET /greyspace/corpora, GET/PUT/DELETE
    /ranges/{range_id}/greyspace, GET /ranges/{range_id}/greyspace/config"; the reads
    need RANGE_READ, attach/detach RANGE_UPDATE."""
    ops = {r.operation_id: r for r in greyspace_router.router.routes if isinstance(r, APIRoute)}
    expected = {
        "greyspace_list_corpora": ("GET", "/greyspace/corpora", {Permission.RANGE_READ}),
        "greyspace_get_status": ("GET", "/ranges/{range_id}/greyspace", {Permission.RANGE_READ}),
        "greyspace_attach": ("PUT", "/ranges/{range_id}/greyspace", {Permission.RANGE_UPDATE}),
        "greyspace_detach": ("DELETE", "/ranges/{range_id}/greyspace", {Permission.RANGE_UPDATE}),
        "greyspace_get_config": ("GET", "/ranges/{range_id}/greyspace/config", {Permission.RANGE_READ}),
    }
    assert set(ops) == set(expected)
    for op, (method, path, perms) in expected.items():
        route = ops[op]
        assert path in route.path and method in route.methods, op
        assert perms <= _permissions(route), op


def test_one_row_per_range_and_its_migration():
    """ADR 0007, decision 5: "One row per range in `range_greyspace`" (migration
    a2b3c4d5e6f7), tenant-indexed, dropped with its range."""
    assert models.RangeGreyspace.__tablename__ == "range_greyspace"
    assert set(models.STATUSES) == {"configured", "configuring", "deployed", "pending_infrastructure", "failed"}
    range_id = models.RangeGreyspace.__table__.columns["range_id"]
    assert range_id.primary_key
    (fk,) = range_id.foreign_keys
    assert fk.target_fullname == "ranges.id" and fk.ondelete == "CASCADE"
    migration = (ROOT / "control-plane/api/alembic/versions/a2b3c4d5e6f7_add_range_greyspace.py").read_text(
        encoding="utf-8")
    assert 'revision: str = "a2b3c4d5e6f7"' in migration
    assert "ix_range_greyspace_tenant" in migration


def test_attach_refuses_foreign_ranges_and_busy_ranges(db_session):
    """ADR 0007, decision 5: a foreign tenant's range 404s, an in-flight range 409s and
    an unrenderable block 422s listing the reasons; a lab-session range is never
    changed."""
    other = Tenant(id=uuid.uuid4(), name="Other Org", slug="other")
    db_session.add(other)
    db_session.flush()
    foreign_tpl = Template(name="foreign", yaml="nodes: []", tenant_id=other.id)
    db_session.add(foreign_tpl)
    db_session.flush()
    foreign = Range(name="foreign range", template_id=foreign_tpl.id, tenant_id=other.id)
    db_session.add(foreign)
    db_session.flush()
    operator = CurrentUser(id=str(uuid.uuid4()), email="op@truenorth.local", display_name="Operator",
                           role=UserRole.admin, tenant_id=str(DEV_ID), keycloak_id="operator")
    with pytest.raises(HTTPException) as exc:
        service.readable_range(db_session, foreign.id, operator)
    assert exc.value.status_code == 404

    tpl = Template(name="busy", yaml="nodes: []", tenant_id=DEV_ID)
    db_session.add(tpl)
    db_session.flush()
    busy = Range(name="busy range", template_id=tpl.id, tenant_id=DEV_ID, state=RangeState.provisioning)
    db_session.add(busy)
    db_session.flush()
    with pytest.raises(HTTPException) as exc:
        service.changeable_range(db_session, busy.id, operator)
    assert exc.value.status_code == 409

    found = service.problems(GreyspaceBlock(site_packs=["not-a-pack"]))
    assert found and any("site_packs" in problem for problem in found)


# ── Decision 6: the worker seam and the gs-core VM ───────────────────────────


def test_deployer_registry_seam():
    """ADR 0007, decision 6: "DEPLOYERS maps backend → host deployer" (a registry, not
    branches) — mock records `deployed`, vsphere_api is the only HOSTED backend, any
    other backend is `pending_infrastructure` pointing at gs-core on vsphere_api."""
    assert set(worker_greyspace.DEPLOYERS) == {"mock", "vsphere_api"}
    assert {"vsphere_api"} == worker_greyspace.HOSTED
    status, detail = worker_greyspace.plan("mock", {"corpus_tier": "t0"})
    assert status == "deployed" and detail["backend"] == "mock"
    status, detail = worker_greyspace.plan("vsphere_api", {"corpus_tier": "t0"})
    assert status == "configuring"
    status, detail = worker_greyspace.plan("proxmox", {"corpus_tier": "t0"})
    assert status == "pending_infrastructure"
    assert "vsphere_api" in detail["todo"]


def test_gs_core_vm_is_planned_from_the_template():
    """ADR 0007, decision 6: vsphere_api "builds the gs-core VM" cloned from the golden
    image GREYSPACE_HOST_TEMPLATE (default `greyspace-host`), one NIC at the subnet's
    last address, the T0 corpus bundled into guestinfo chunks, and the router holding
    the gateway gets the `greyspace_route`."""
    template = {
        "networks": [{"name": "greyspace", "cidr": "10.99.0.0/24", "gateway": "10.99.0.1", "vlan_id": 42}],
        "vms": [{"name": "edge01", "nics": [{"network": "greyspace", "ip": "10.99.0.1"}]}],
    }
    vm, info = greyspace_host.host_vm("abcdefgh01", {"corpus_tier": "t0"}, template)
    assert vm["node_id"] == "gs-core" and vm["role"] == "greyspace"
    assert vm["template_name"] == greyspace_host.host_template() == "greyspace-host"
    assert len(vm["nics"]) == 1 and vm["nics"][0]["ip"] == "10.99.0.254"
    bundle = vm["guestinfo"]
    count = int(bundle["guestinfo.tn.greyspace.bundle.count"])
    assert count >= 1 and f"guestinfo.tn.greyspace.bundle.{count - 1}" in bundle
    assert set(f"guestinfo.tn.greyspace.bundle.{n}" for n in range(count)) <= set(bundle)
    assert info["public"] == "198.18.0.0/15"
    router_name = greyspace_host.route_router(template, info)
    assert router_name == "edge01"
    route = template["vms"][0]["greyspace_route"]
    assert route == {"prefix": info["public"], "via": info["ip"], "resolver": info["resolver"],
                     "network": "greyspace"}


# ── Decision 7: the configure stage and its service account ──────────────────


def test_configure_stage_contract_and_steps():
    """ADR 0007, decision 7: `configure_range` is a task on queue `provision`, queued by
    after_provision for HOSTED backends; it waits for cloud-init, runs
    `bootstrap.sh configure` and `bin/gs health`."""
    contract = worker_contracts.TASKS["configure_range"]
    assert contract.queue == "provision"
    assert contract.qualified_name == "worker.tasks.configure_range"
    from worker import configure_tasks

    assert ("greyspace", worker_greyspace.configure) in configure_tasks.CONFIGURATORS
    steps = greyspace_host.configure_steps()
    assert [s.label for s in steps] == ["cloud-init", "configure", "health"]
    assert "cloud-init status --wait" in steps[0].arguments
    assert "bootstrap.sh configure" in steps[1].arguments
    assert "health --json" in steps[2].arguments


def test_service_account_password_is_never_stored(monkeypatch):
    """ADR 0007, decision 7 (amended): the service-account password is "the HMAC of the
    TN_SECRETS_KEY (first key) and range id"; it is never stored — the guestinfo bundle
    carries only its SHA-512 crypt."""
    original = os.environ["TN_SECRETS_KEY"]
    password = greyspace_host.host_password("r-1")
    assert password == greyspace_host.host_password("r-1")
    assert password != greyspace_host.host_password("r-2")
    monkeypatch.setenv("TN_SECRETS_KEY", "another-test-key-0123456789abcdef-0123456789abcdef")
    assert password != greyspace_host.host_password("r-1")
    monkeypatch.setenv("TN_SECRETS_KEY", original)
    cloud_init = greyspace_host.cloud_config("r-1")
    (user,) = cloud_init["users"]
    assert user["name"] == greyspace_host.SERVICE_USER
    assert user["hashed_passwd"].startswith("$6$")
    assert password not in json.dumps(cloud_init)


# ── Decision 8: mail, NTP, trust, digests and the reserved infra zone ────────


def test_mail_ntp_and_trust_are_rendered(rendered, t0):
    """ADR 0007, decision 8: every site zone's "MX is Mailpit (`mail.gs-infra.net`)";
    chrony serves local time; the root CA "is published at
    `http://pki.gs-infra.net/root.crt`"; webmail answers at `webmail.<zone>`."""
    plan = config.address_plan(t0)
    site_zones = sorted({zone_of(s.fqdn) for s in t0.sites})
    threat_only = sorted({zone_of(t.fqdn) for t in t0.threat_domains} - set(site_zones))
    assert threat_only, "the T0 fixture should have threat-only zones"
    for zone in site_zones:
        assert f"@ IN MX 10 mail.{config.INFRA_ZONE}." in rendered.files[f"dns/auth/zones/db.{zone}"]
    for zone in threat_only:
        assert f"@ IN MX 10 mail.{config.INFRA_ZONE}." not in rendered.files[f"dns/auth/zones/db.{zone}"]
    names = {name for name, _ in config.infra_names(plan, config.selected_sites(t0, config.BlockParams()),
                                                     config.BlockParams())}
    webmail_zone = next(zone_of(s.fqdn) for s in t0.sites if s.category == "webmail")
    assert f"webmail.{webmail_zone}" in names
    assert "local stratum" in rendered.files["ntp/chrony.conf"]
    assert 'cp root.crt "$OVERLAY/sites/pki.gs-infra.net/root.crt"' in rendered.files["ca/make-certs.sh"]


def test_every_image_is_pinned_by_digest():
    """ADR 0007, decision 8: "Every image is pinned by digest; local images build from
    greyspace/images on pinned bases"."""
    for name, image in {**config.IMAGES, "npc": npc.IMAGE}.items():
        assert "@sha256:" in image, name
    assert set(config.LOCAL_IMAGES) == {"resolver", "probe", "ntp", "ca"}
    for dockerfile in sorted((ROOT / "greyspace/images").glob("*/Dockerfile")):
        from_line = next(line for line in dockerfile.read_text(encoding="utf-8").splitlines()
                         if line.startswith("FROM "))
        assert "@sha256:" in from_line, dockerfile


def test_gs_infra_net_is_reserved():
    """ADR 0007, decision 8: "`gs-infra.net` is Greyspace's own zone; the corpus may not
    use it"."""
    doc = fixture.t0_manifest_doc()
    doc["sites"][0]["fqdn"] = f"mail.{config.INFRA_ZONE}"
    doc["sites"][0]["aliases"] = []
    errors = config.validate(parse_manifest(doc), config.BlockParams())
    assert any("own zone" in error and config.INFRA_ZONE in error for error in errors)


# ── Decision 9: breadcrumbs ──────────────────────────────────────────────────


def test_breadcrumb_token_is_keyed_per_exercise(monkeypatch):
    """ADR 0007, decision 9 (amended 2026-10-11): the per-exercise `{{ token }}` is an
    HMAC "under a per-exercise key derived from the installation secret", so a Student
    who sees one planted token cannot compute the next."""
    token = greyspace_breadcrumb.breadcrumb_token("ex-1", "crumb-1")
    assert token == greyspace_breadcrumb.breadcrumb_token("ex-1", "crumb-1")
    assert token != greyspace_breadcrumb.breadcrumb_token("ex-2", "crumb-1")
    assert token != greyspace_breadcrumb.breadcrumb_token("ex-1", "crumb-2")
    assert re.fullmatch(r"GS-[0-9A-F]{16}", token)
    assert greyspace_breadcrumb.crumb_found("ex-1", "crumb-1", f"the answer is {token.lower()}")
    assert not greyspace_breadcrumb.crumb_found("ex-1", "crumb-1", "nothing planted here")
    monkeypatch.delenv(greyspace_breadcrumb.KEY_ENV, raising=False)
    monkeypatch.delenv(greyspace_breadcrumb.FALLBACK_KEY_ENV, raising=False)
    with pytest.raises(greyspace_breadcrumb.BreadcrumbKeyError):
        greyspace_breadcrumb.breadcrumb_token("ex-1", "crumb-1")
    monkeypatch.setenv(greyspace_breadcrumb.FALLBACK_KEY_ENV, "too-short")
    with pytest.raises(greyspace_breadcrumb.BreadcrumbKeyError):
        greyspace_breadcrumb.breadcrumb_token("ex-1", "crumb-1")


def test_crumb_channels_and_history():
    """ADR 0007, decision 9: the injector's output is dispatched "by backend
    (`CRUMB_CHANNELS`: mock records the plant; vSphere `bin/gs crumb plant` on the
    host)" and deliver_breadcrumbs keeps the last 50 operations."""
    assert set(worker_greyspace.CRUMB_CHANNELS) == {"mock", "vsphere_api"}
    plant = greyspace_host.crumb_steps({"operation": "plant", "exercise": "ex-1", "payload": {"a": 1}})
    assert any("crumb plant --b64 " in step.arguments for step in plant)
    remove = greyspace_host.crumb_steps({"operation": "remove", "exercise": "ex-1"})
    assert any("crumb remove --exercise ex-1" in step.arguments for step in remove)
    assert "[-50:]" in inspect.getsource(worker_greyspace.deliver_breadcrumbs)


# ── Decision 10: the NPC agent ───────────────────────────────────────────────


def test_npc_traffic_is_marked(t0):
    """ADR 0007, decision 10: an NPC agent "browses from inside Greyspace and marks
    itself `GreyspaceNPC/1` in its User-Agent", shipped in a pinned image."""
    profile = npc.PROFILE_NAMES[1]
    rendered_npc = config.render(t0, config.BlockParams(npc_profile=profile))
    agent = rendered_npc.files["npc/npc.py"]
    assert "Mozilla/5.0" in agent and "GreyspaceNPC/1" in agent
    assert json.loads(rendered_npc.files["npc/config.json"])["profile"] == profile
    assert "@sha256:" in npc.IMAGE


# ── Wiring ───────────────────────────────────────────────────────────────────


def test_seam_calls_wrap_the_provision_lifecycle():
    """ADR 0007, Wiring: tasks.py "reserves Greyspace before VLANs are reserved"
    (`greyspace.plan_host(...)`), calls `greyspace.after_provision(...)` when the range
    is ready and `greyspace.after_destroy(...)` after the range is destroyed."""
    source = (ROOT / "control-plane/worker/worker/tasks.py").read_text(encoding="utf-8")
    assert source.index("greyspace.plan_host(") < source.index("reserve_for_build(")
    assert source.index("greyspace.plan_host(") < source.index("greyspace.after_provision(")
    assert source.index("greyspace.after_provision(") < source.index("greyspace.after_destroy(")


def test_designer_keeps_the_template_greyspace_block():
    """ADR 0007, Wiring: "Designer saves (`POST /ranges/{range_id}/topology`) keep the
    template's `greyspace:` key unless the diagram carries one" — the Internet/Cloud
    stencil holds the block (`nodeData.greyspace`), and the designer save loop never
    clobbers keys it does not own."""
    cell = range_topology.greyspace_cell({"corpus_tier": "t0", "npc_profile": "office-day"}, 0, 0)
    assert cell["nodeType"] == "cloud" and cell["nodeData"]["greyspace"]["corpus_tier"] == "t0"
    warnings: list[str] = []
    block = range_topology._greyspace_from_cells([cell], warnings)
    assert block is not None and not warnings
    assert set(block) == set(range_topology.GREYSPACE_DEFAULTS)
    assert block["corpus_tier"] == "t0" and block["npc_profile"] == "office-day"
    assert range_topology._greyspace_from_cells([], warnings) is None
    # ranges.py's designer-save loop only overwrites its own keys; "greyspace" is not one.
    assert "greyspace" not in ranges_router._DESIGNER_KEYS
