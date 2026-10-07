"""Greyspace runtime configuration, generated from a corpus manifest and a range's block.

``render(manifest, params)`` returns every file the Docker stack needs (``greyspace/``):

* ``compose.yaml``                     the stack: ISP routers, DNS, resolver, web farm, threat stub
* ``frr/<isp>/frr.conf`` + ``daemons``  one FRR router per ISP; eBGP full mesh, each ISP
                                        originating its own prefix
* ``dns/root/``                         root zone: delegates every TLD to the TLD server
* ``dns/tld/``                          one zone per TLD: delegates every domain to the
                                        authoritative server, with glue
* ``dns/auth/``                         one zone per registrable domain
* ``resolver/``                         Unbound, iterating from the Greyspace root hints
* ``web/``                              nginx: virtual hosts by Host, overlay (rw, per range)
                                        tried before the corpus (ro)
* ``threat/``                           the threat-actor stub: a page per domain

The address plan is fixed relative to the first ISP's prefix: its first /24 is the
"public" infrastructure segment (routers, DNS, web farm) the Docker network uses, so
service addresses look like internet addresses, not 172.x. Site and threat addresses
are routed to the web farm and the threat stub by the ISP routers.

Pure standard library; YAML output is JSON, which is valid YAML.
"""

from __future__ import annotations

import ipaddress
import json
import zlib
from dataclasses import dataclass, field

from .manifest import Manifest, Site, ThreatDomain, zone_of

IMAGES = {
    "frr": "quay.io/frrouting/frr:10.1.1",
    "coredns": "coredns/coredns:1.11.3",
    "nginx": "nginx:1.27-alpine",
}
NPC_PROFILES = ("off", "office-day", "quiet-night")

# Host offsets in the infrastructure /24.
ROUTER_BASE = 2  # isp routers .2, .3, ...
ROOT_DNS = 10
TLD_DNS = 11
AUTH_DNS = 12
RESOLVER = 53
THREAT = 66
WEBFARM = 80
PROBE = 200


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BlockParams:
    """A range's Greyspace block (template ``greyspace:`` key, or attached through the API)."""

    corpus_tier: str = "t0"
    site_packs: tuple[str, ...] | None = None  # categories; None = every category
    public_prefix: str | None = None
    npc_profile: str = "off"
    threat_infra: bool = True
    trust_ca: bool = True


@dataclass
class Rendered:
    files: dict[str, str]
    summary: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AddressPlan:
    public: ipaddress.IPv4Network  # covers every ISP prefix: the Docker network's subnet
    infra: ipaddress.IPv4Network
    routers: dict[str, ipaddress.IPv4Address]  # isp slug -> address
    root_dns: ipaddress.IPv4Address
    tld_dns: ipaddress.IPv4Address
    auth_dns: ipaddress.IPv4Address
    resolver: ipaddress.IPv4Address
    webfarm: ipaddress.IPv4Address
    threat: ipaddress.IPv4Address
    probe: ipaddress.IPv4Address


def _slug(n: int) -> str:
    return f"isp-{chr(ord('a') + n)}" if n < 26 else f"isp-{n}"


def address_plan(manifest: Manifest) -> AddressPlan:
    first = manifest.isps[0].prefix
    if first.prefixlen > 24:
        raise ConfigError(f"the first ISP prefix {first} must be /24 or larger to hold the infrastructure segment")
    infra = next(first.subnets(new_prefix=24))
    hosts = infra.network_address

    def at(offset: int) -> ipaddress.IPv4Address:
        return hosts + offset

    if len(manifest.isps) > ROOT_DNS - ROUTER_BASE:
        raise ConfigError(f"at most {ROOT_DNS - ROUTER_BASE} ISPs fit the infrastructure segment")
    # One L2 segment spans the whole public space, so Docker's isolation rules for an
    # internal network (drop anything not inside its subnet) pass routed traffic. Hosts
    # still reach sites through the ISP routers: their static routes are more specific.
    public = first
    while not all(i.prefix.subnet_of(public) for i in manifest.isps):
        public = public.supernet()
    if public.prefixlen < 8:
        raise ConfigError(f"the ISP prefixes are too far apart (they only share {public}); keep them within one /8")
    return AddressPlan(
        public=public,
        infra=infra,
        routers={_slug(n): at(ROUTER_BASE + n) for n in range(len(manifest.isps))},
        root_dns=at(ROOT_DNS),
        tld_dns=at(TLD_DNS),
        auth_dns=at(AUTH_DNS),
        resolver=at(RESOLVER),
        webfarm=at(WEBFARM),
        threat=at(THREAT),
        probe=at(PROBE),
    )


def selected_sites(manifest: Manifest, params: BlockParams) -> list[Site]:
    if params.site_packs is None:
        return list(manifest.sites)
    packs = set(params.site_packs)
    return [s for s in manifest.sites if s.category in packs]


def validate(manifest: Manifest, params: BlockParams) -> list[str]:
    """Why this block cannot run on this corpus; empty when it can."""
    errors: list[str] = []
    if params.corpus_tier != manifest.tier:
        errors.append(f"corpus_tier {params.corpus_tier!r} does not match the manifest's tier {manifest.tier!r}")
    if params.npc_profile not in NPC_PROFILES:
        errors.append(f"npc_profile must be one of {list(NPC_PROFILES)}")
    if params.site_packs is not None:
        unknown = sorted(set(params.site_packs) - set(manifest.categories))
        if unknown:
            errors.append(f"site_packs {unknown} are not in this corpus (it has {sorted(manifest.categories)})")
        if not params.site_packs:
            errors.append("site_packs must name at least one pack, or be omitted for all")
    if params.public_prefix is not None:
        try:
            prefix = ipaddress.IPv4Network(params.public_prefix, strict=True)
        except (ValueError, TypeError):
            errors.append(f"public_prefix {params.public_prefix!r} is not an IPv4 network")
        else:
            outside = [str(i.prefix) for i in manifest.isps if not i.prefix.subnet_of(prefix)]
            if outside:
                errors.append(f"public_prefix {prefix} does not cover the corpus prefixes {outside}")
    try:
        plan = address_plan(manifest)
    except ConfigError as exc:
        errors.append(str(exc))
    else:
        clash = [s.fqdn for s in manifest.sites if s.ip in plan.infra] + [
            t.fqdn for t in manifest.threat_domains if t.ip in plan.infra
        ]
        if clash:
            errors.append(f"{plan.infra} is the infrastructure segment; these names use it: {sorted(clash)}")
    return errors


# ── DNS ─────────────────────────────────────────────────────────────────────
def _serial(manifest: Manifest) -> int:
    return 1_000_000_000 + zlib.crc32(manifest.version.encode()) % 1_000_000_000


def _soa(origin: str, ns: str, serial: int) -> list[str]:
    return [
        f"$ORIGIN {origin}",
        "$TTL 3600",
        f"@ IN SOA {ns} hostmaster.{ns} {serial} 7200 3600 1209600 3600",
    ]


def _dns(manifest: Manifest, plan: AddressPlan, sites: list[Site], threats: list[ThreatDomain]) -> dict[str, str]:
    serial = _serial(manifest)
    # zone -> [(relative owner, ip)]
    zones: dict[str, list[tuple[str, ipaddress.IPv4Address]]] = {}
    for site in sites:
        for name in site.names:
            zones.setdefault(zone_of(name), []).append((name, site.ip))
    for t in threats:
        zones.setdefault(zone_of(t.fqdn), []).append((t.fqdn, t.ip))
    # The root and TLD servers' own names, so a resolver can look them up too.
    zones.setdefault("root-servers.net", []).append(("a.root-servers.net", plan.root_dns))
    zones.setdefault("gtld-servers.net", []).append(("a.gtld-servers.net", plan.tld_dns))

    files: dict[str, str] = {}
    root = [*_soa(".", "a.root-servers.net.", serial), "@ IN NS a.root-servers.net."]
    root.append(f"a.root-servers.net. IN A {plan.root_dns}")
    root.append(f"a.gtld-servers.net. IN A {plan.tld_dns}")
    tlds = sorted({z.split(".")[-1] for z in zones})
    for tld in tlds:
        root.append(f"{tld}. IN NS a.gtld-servers.net.")
    files["dns/root/db.root"] = "\n".join(root) + "\n"
    files["dns/root/Corefile"] = ". {\n    file /etc/coredns/db.root .\n    errors\n    log\n}\n"

    for tld in tlds:
        lines = [*_soa(f"{tld}.", "a.gtld-servers.net.", serial), "@ IN NS a.gtld-servers.net."]
        for zone in sorted(z for z in zones if z.split(".")[-1] == tld):
            label = zone.split(".")[0]
            lines.append(f"{label} IN NS ns1.{zone}.")
            lines.append(f"ns1.{label} IN A {plan.auth_dns}")
        files[f"dns/tld/zones/db.{tld}"] = "\n".join(lines) + "\n"
    files["dns/tld/Corefile"] = ". {\n    auto {\n        directory /etc/coredns/zones\n    }\n    errors\n    log\n}\n"

    for zone, records in sorted(zones.items()):
        lines = [*_soa(f"{zone}.", f"ns1.{zone}.", serial), "@ IN NS ns1", f"ns1 IN A {plan.auth_dns}"]
        for name, ip in sorted(set(records), key=lambda r: (r[0], str(r[1]))):
            owner = "@" if name == zone else name[: -(len(zone) + 1)]
            lines.append(f"{owner} IN A {ip}")
        files[f"dns/auth/zones/db.{zone}"] = "\n".join(lines) + "\n"
    files["dns/auth/Corefile"] = files["dns/tld/Corefile"]

    files["resolver/root.hints"] = (
        ".                     3600000 IN NS a.root-servers.net.\n"
        f"a.root-servers.net.   3600000 IN A  {plan.root_dns}\n"
    )
    files["resolver/unbound.conf"] = "\n".join(
        [
            "server:",
            "    interface: 0.0.0.0",
            "    access-control: 0.0.0.0/0 allow",
            "    do-ip6: no",
            "    do-daemonize: no",
            "    username: \"\"",
            "    chroot: \"\"",
            "    use-syslog: no",
            "    logfile: \"\"",
            "    verbosity: 1",
            "    log-queries: yes",
            "    root-hints: /etc/unbound/root.hints",
            "    module-config: \"iterator\"",
            "    qname-minimisation: no",
            "    harden-referral-path: no",
            "    cache-max-ttl: 300",
            "",
        ]
    )
    return files


# ── Routing ─────────────────────────────────────────────────────────────────
_DAEMONS = """\
# FRR daemons for a Greyspace ISP router (generated). zebra, mgmtd and staticd always run.
bgpd=yes
vtysh_enable=yes
zebra_options="  -A 127.0.0.1 -s 90000000"
mgmtd_options="  -A 127.0.0.1"
bgpd_options="   -A 127.0.0.1"
staticd_options="-A 127.0.0.1"
"""


def _frr(manifest: Manifest, plan: AddressPlan, threats: list[ThreatDomain]) -> dict[str, str]:
    files: dict[str, str] = {}
    slugs = list(plan.routers)
    for n, isp in enumerate(manifest.isps):
        slug = slugs[n]
        me = plan.routers[slug]
        lines = [
            "frr defaults traditional",
            f"hostname {slug}",
            "log stdout informational",
            "ip forwarding",
            "!",
            f"! {isp.name}, AS{isp.asn}: originates {isp.prefix}",
            # The infrastructure segment stays on-link: the prefix route below covers it.
            f"ip route {plan.infra} eth0",
            f"ip route {isp.prefix} {plan.webfarm}",
        ]
        for t in threats:
            if t.ip in isp.prefix:
                lines.append(f"ip route {t.ip}/32 {plan.threat}")
        lines += ["!", f"router bgp {isp.asn}", f" bgp router-id {me}", " no bgp ebgp-requires-policy"]
        lines.append(" no bgp network import-check")
        for m, peer in enumerate(manifest.isps):
            if m != n:
                lines.append(f" neighbor {plan.routers[slugs[m]]} remote-as {peer.asn}")
                lines.append(f" neighbor {plan.routers[slugs[m]]} description {peer.name}")
        lines += [" address-family ipv4 unicast", "  redistribute static", " exit-address-family", "exit", "!"]
        files[f"frr/{slug}/frr.conf"] = "\n".join(lines) + "\n"
        files[f"frr/{slug}/daemons"] = _DAEMONS
        files[f"frr/{slug}/vtysh.conf"] = "service integrated-vtysh-config\n"
    return files


# ── Web ─────────────────────────────────────────────────────────────────────
_ENTRYPOINT = """\
#!/bin/sh
# Hold every site address on lo so the kernel accepts traffic the ISP routers send here.
set -eu
while read -r ip; do
  [ -n "$ip" ] && { ip addr add "$ip/32" dev lo 2>/dev/null || true; }
done < /gs/addresses.txt
exec nginx -g 'daemon off;'
"""


def _nginx(map_lines: list[str], location: str) -> str:
    return "\n".join(
        [
            "worker_processes auto;",
            "events { worker_connections 1024; }",
            "http {",
            "    include /etc/nginx/mime.types;",
            "    default_type application/octet-stream;",
            "    server_tokens off;",
            "    sendfile on;",
            "    log_format gs '$remote_addr - [$time_iso8601] \"$host\" \"$request\" $status $body_bytes_sent';",
            "    access_log /dev/stdout gs;",
            "    map $host $gs_site {",
            "        hostnames;",
            '        default "";',
            *[f"        {line}" for line in map_lines],
            "    }",
            "    server {",
            "        listen 80 default_server;",
            '        if ($gs_site = "") { return 404; }',
            "        root /srv;",
            "        location / {",
            f"            {location}",
            "        }",
            "    }",
            "}",
            "",
        ]
    )


def _web(sites: list[Site], threats: list[ThreatDomain]) -> dict[str, str]:
    files: dict[str, str] = {}
    site_map = [f"{name} {site.path};" for site in sites for name in site.names]
    files["web/nginx.conf"] = _nginx(
        site_map,
        # The range's overlay (breadcrumbs) first, then the shared read-only corpus.
        "try_files /overlay/$gs_site$uri /overlay/$gs_site$uri/index.html "
        "/corpus/$gs_site$uri /corpus/$gs_site$uri/index.html =404;",
    )
    files["web/addresses.txt"] = "".join(f"{ip}\n" for ip in sorted({str(s.ip) for s in sites}))
    files["web/entrypoint.sh"] = _ENTRYPOINT
    if threats:
        files["threat/nginx.conf"] = _nginx(
            [f"{t.fqdn} {t.fqdn};" for t in threats],
            "try_files /threat/$gs_site$uri /threat/$gs_site$uri/index.html =404;",
        )
        files["threat/addresses.txt"] = "".join(f"{ip}\n" for ip in sorted({str(t.ip) for t in threats}))
        files["threat/entrypoint.sh"] = _ENTRYPOINT
        for t in threats:
            files[f"threat/www/{t.fqdn}/index.html"] = (
                f"<!doctype html><title>{t.fqdn}</title>"
                f"<p data-gs-threat=\"{t.role}\">Greyspace threat-actor stub ({t.actor}, {t.role}).</p>\n"
            )
    return files


# ── Compose ─────────────────────────────────────────────────────────────────
def _compose(
    manifest: Manifest, plan: AddressPlan, threats: list[ThreatDomain], corpus_dir: str, images_dir: str
) -> dict:
    net = "gs-public"

    def addr(ip: ipaddress.IPv4Address) -> dict:
        return {net: {"ipv4_address": str(ip)}}

    services: dict[str, dict] = {}
    for slug, ip in plan.routers.items():
        services[slug] = {
            "image": IMAGES["frr"],
            "cap_add": ["NET_ADMIN", "NET_RAW", "SYS_ADMIN"],
            "sysctls": {"net.ipv4.ip_forward": "1", "net.ipv4.conf.all.rp_filter": "0"},
            "volumes": [f"./frr/{slug}:/etc/frr"],
            "networks": addr(ip),
            "restart": "unless-stopped",
        }
    for name, ip, conf in (
        ("dns-root", plan.root_dns, "./dns/root"),
        ("dns-tld", plan.tld_dns, "./dns/tld"),
        ("dns-auth", plan.auth_dns, "./dns/auth"),
    ):
        services[name] = {
            "image": IMAGES["coredns"],
            "command": ["-conf", "/etc/coredns/Corefile"],
            "volumes": [f"{conf}:/etc/coredns:ro"],
            "networks": addr(ip),
            "restart": "unless-stopped",
        }
    services["resolver"] = {
        "build": f"{images_dir}/resolver",
        "image": "truenorth/greyspace-resolver:dev",
        "volumes": ["./resolver/unbound.conf:/etc/unbound/unbound.conf:ro", "./resolver/root.hints:/etc/unbound/root.hints:ro"],
        "networks": addr(plan.resolver),
        "depends_on": ["dns-root", "dns-tld", "dns-auth"],
        "restart": "unless-stopped",
    }
    services["webfarm"] = {
        "image": IMAGES["nginx"],
        "cap_add": ["NET_ADMIN"],
        "entrypoint": ["/bin/sh", "/gs/entrypoint.sh"],
        "volumes": [
            f"{corpus_dir}:/srv/corpus:ro",
            "gs-overlay:/srv/overlay",
            "./web/nginx.conf:/etc/nginx/nginx.conf:ro",
            "./web/addresses.txt:/gs/addresses.txt:ro",
            "./web/entrypoint.sh:/gs/entrypoint.sh:ro",
        ],
        "networks": addr(plan.webfarm),
        "restart": "unless-stopped",
    }
    if threats:
        services["threat"] = {
            "image": IMAGES["nginx"],
            "cap_add": ["NET_ADMIN"],
            "entrypoint": ["/bin/sh", "/gs/entrypoint.sh"],
            "volumes": [
                "./threat/www:/srv/threat:ro",
                "./threat/nginx.conf:/etc/nginx/nginx.conf:ro",
                "./threat/addresses.txt:/gs/addresses.txt:ro",
                "./threat/entrypoint.sh:/gs/entrypoint.sh:ro",
            ],
            "networks": addr(plan.threat),
            "restart": "unless-stopped",
        }
    first_router = next(iter(plan.routers.values()))
    routes = " && ".join(
        [*(f"ip route replace {i.prefix} via {first_router}" for i in manifest.isps), f"ip route replace {plan.infra} dev eth0"]
    )
    services["probe"] = {
        # A stand-in for a range's edge: routes the public space via the first ISP and
        # resolves through the Greyspace resolver. Started only with --profile probe.
        "build": f"{images_dir}/probe",
        "image": "truenorth/greyspace-probe:dev",
        "profiles": ["probe"],
        "cap_add": ["NET_ADMIN"],
        "dns": [str(plan.resolver)],
        "command": ["sh", "-c", f"{routes} ; exec sleep infinity"],
        "networks": addr(plan.probe),
    }
    return {
        "services": services,
        "networks": {
            net: {"internal": True, "ipam": {"config": [{"subnet": str(plan.public), "ip_range": str(plan.infra)}]}}
        },
        "volumes": {"gs-overlay": {}},
    }


def render(
    manifest: Manifest,
    params: BlockParams,
    *,
    corpus_dir: str = "${GS_CORPUS_DIR:?set GS_CORPUS_DIR to the corpus root}",
    images_dir: str = "${GS_IMAGES_DIR:?set GS_IMAGES_DIR to greyspace/images}",
) -> Rendered:
    """Every file of the stack for this block on this corpus. Raises ConfigError."""
    errors = validate(manifest, params)
    if errors:
        raise ConfigError("; ".join(errors))
    plan = address_plan(manifest)
    sites = selected_sites(manifest, params)
    threats = list(manifest.threat_domains) if params.threat_infra else []

    files: dict[str, str] = {}
    files.update(_dns(manifest, plan, sites, threats))
    files.update(_frr(manifest, plan, threats))
    files.update(_web(sites, threats))
    files["compose.yaml"] = json.dumps(_compose(manifest, plan, threats, corpus_dir, images_dir), indent=2) + "\n"

    zones = sorted(p.removeprefix("dns/auth/zones/db.") for p in files if p.startswith("dns/auth/zones/"))
    tlds = sorted(p.removeprefix("dns/tld/zones/db.") for p in files if p.startswith("dns/tld/zones/"))
    compose = json.loads(files["compose.yaml"])
    summary = {
        "address_plan": {
            "public": str(plan.public),
            "infrastructure": str(plan.infra),
            "resolver": str(plan.resolver),
            "root_dns": str(plan.root_dns),
            "webfarm": str(plan.webfarm),
            "threat": str(plan.threat) if threats else None,
        },
        "isps": [
            {"name": i.name, "asn": i.asn, "prefix": str(i.prefix), "router": str(plan.routers[s])}
            for i, s in zip(manifest.isps, plan.routers, strict=True)
        ],
        "services": sorted(k for k, v in compose["services"].items() if "profiles" not in v),
        "sites": len(sites),
        "site_packs": sorted({s.category for s in sites}),
        "tlds": tlds,
        "zones": zones,
        "threat_domains": [{"fqdn": t.fqdn, "ip": str(t.ip), "role": t.role} for t in threats],
        "files": sorted(files),
    }
    return Rendered(files=files, summary=summary)
