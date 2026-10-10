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
* ``mail/``                             SMTP + webmail (Mailpit): MX for every site zone
* ``ntp/``                              an NTP server (chrony, local clock) at ntp.gs-infra.net
* ``ca/``                               ``trust_ca``: a Greyspace root CA and one certificate per
                                        zone, made at start-up; the web farm serves HTTPS
* ``npc/``                              ``npc_profile``: simulated users browsing, resolving
                                        and mailing (npc.py), driven by a profile
* ``bin/gs``                            the operator CLI on the stack's host: up, health,
                                        breadcrumbs (plant/remove/list/check), corpus checks

The address plan is fixed relative to the first ISP's prefix: its first /24 is the
"public" infrastructure segment (routers, DNS, web farm) the Docker network uses, so
service addresses look like internet addresses, not 172.x. Site and threat addresses
are routed to the web farm and the threat stub by the ISP routers.

``host=True`` renders the stack for a ``gs-core`` VM instead of an isolated test host:
the public network is a routed bridge (``gs-public0``, no NAT) the VM forwards range
traffic onto, and there is no probe.

Pure standard library; YAML output is JSON, which is valid YAML.
"""

from __future__ import annotations

import ipaddress
import json
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from . import npc
from .manifest import Manifest, Site, ThreatDomain, zone_of

# Every image by digest (tag kept for people). Docker Hub ones are pulled through the CI
# mirror (.github/actions/dockerhub-mirror); a gs-core VM has them pre-loaded (greyspace/host).
IMAGES = {
    "frr": "quay.io/frrouting/frr:10.1.1@sha256:8943ad2991f084de2c6a9560fbbff243e7317a7ff9f18b3da01792e2e980a8be",
    "coredns": "coredns/coredns:1.11.3@sha256:9caabbf6238b189a65d0d6e6ac138de60d6a1c419e5a341fbbb7c78382559c6e",
    "nginx": "nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10",
    "mailpit": "axllent/mailpit:v1.27.10@sha256:b1f1be18af530d939a11ee8820b379e0c88eeec204d904bfad68862adced3a5a",
}
# Built from greyspace/images/<name> (FROM pinned base images), tagged by stack version.
LOCAL_IMAGES = ("resolver", "probe", "ntp", "ca", "npc")
LOCAL_TAG = "gs1"
NPC_PROFILES = npc.PROFILE_NAMES

# Host offsets in the infrastructure /24.
ROUTER_BASE = 2  # isp routers .2, .3, ...
ROOT_DNS = 10
TLD_DNS = 11
AUTH_DNS = 12
MAIL = 25
RESOLVER = 53
THREAT = 66
WEBFARM = 80
NTP = 123
PROBE = 200
NPC = 201

# Names for Greyspace's own services, in a zone no corpus site may use.
INFRA_ZONE = "gs-infra.net"
BRIDGE = "gs-public0"  # host=True: the bridge's name on the gs-core VM


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
    mail: ipaddress.IPv4Address
    ntp: ipaddress.IPv4Address
    npc: ipaddress.IPv4Address
    gateway: ipaddress.IPv4Address  # the bridge's own address (the host side)


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
        mail=at(MAIL),
        ntp=at(NTP),
        npc=at(NPC),
        gateway=at(1),
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
    reserved = sorted(
        n for n in [*(name for s in manifest.sites for name in s.names), *(t.fqdn for t in manifest.threat_domains)]
        if zone_of(n) == INFRA_ZONE
    )
    if reserved:
        errors.append(f"{INFRA_ZONE} is Greyspace's own zone; the corpus may not use it: {reserved}")
    return errors


def infra_names(plan: AddressPlan, sites: list[Site], params: BlockParams) -> list[tuple[str, ipaddress.IPv4Address]]:
    """Greyspace's own service names: (fqdn, address). Webmail answers at ``webmail.<zone>``
    of every webmail site."""
    names = [
        (f"mail.{INFRA_ZONE}", plan.mail),
        (f"ntp.{INFRA_ZONE}", plan.ntp),
        (f"intel.{INFRA_ZONE}", plan.webfarm),
        (f"dns.{INFRA_ZONE}", plan.resolver),
    ]
    if params.trust_ca:
        names.append((f"pki.{INFRA_ZONE}", plan.webfarm))
    taken = {n for s in sites for n in s.names}
    for site in sites:
        webmail = f"webmail.{zone_of(site.fqdn)}"
        if site.category == "webmail" and webmail not in taken:
            names.append((webmail, plan.mail))
            taken.add(webmail)
    return names


# ── DNS ─────────────────────────────────────────────────────────────────────
def _serial(manifest: Manifest) -> int:
    return 1_000_000_000 + zlib.crc32(manifest.version.encode()) % 1_000_000_000


def _soa(origin: str, ns: str, serial: int) -> list[str]:
    return [
        f"$ORIGIN {origin}",
        "$TTL 3600",
        f"@ IN SOA {ns} hostmaster.{ns} {serial} 7200 3600 1209600 3600",
    ]


def _dns(
    manifest: Manifest,
    plan: AddressPlan,
    sites: list[Site],
    threats: list[ThreatDomain],
    extra: list[tuple[str, ipaddress.IPv4Address]] = (),
) -> dict[str, str]:
    serial = _serial(manifest)
    # zone -> [(relative owner, ip)]
    zones: dict[str, list[tuple[str, ipaddress.IPv4Address]]] = {}
    for site in sites:
        for name in site.names:
            zones.setdefault(zone_of(name), []).append((name, site.ip))
    for t in threats:
        zones.setdefault(zone_of(t.fqdn), []).append((t.fqdn, t.ip))
    for name, ip in extra:
        zones.setdefault(zone_of(name), []).append((name, ip))
    # Every site zone receives mail at the Greyspace mail service.
    mx_zones = {zone_of(s.fqdn) for s in sites}
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
        if zone in mx_zones:
            lines.append(f"@ IN MX 10 mail.{INFRA_ZONE}.")
        for name, ip in sorted(set(records), key=lambda r: (r[0], str(r[1]))):
            owner = "@" if name == zone else name[: -(len(zone) + 1)]
            lines.append(f"{owner} IN A {ip}")
        files[f"dns/auth/zones/db.{zone}"] = "\n".join(lines) + "\n"
    # Breadcrumbs add records to these zones at run time (bin/gs): reload them quickly.
    files["dns/auth/Corefile"] = (
        ". {\n    auto {\n        directory /etc/coredns/zones\n        reload 5s\n    }\n    errors\n    log\n}\n"
    )

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
            # A breadcrumb record appears while an exercise runs: do not keep its NXDOMAIN long.
            "    cache-max-negative-ttl: 5",
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


def _nginx(map_lines: list[str], location: str, cert_lines: list[str] | None = None) -> str:
    """nginx: virtual hosts by Host. With ``cert_lines`` (``name zone;``) also HTTPS on 443,
    one certificate per zone chosen by SNI from the Greyspace CA's output (``/certs``)."""
    server = [
        '        if ($gs_site = "") { return 404; }',
        "        root /srv;",
        "        location / {",
        f"            {location}",
        "        }",
    ]
    tls: list[str] = []
    if cert_lines is not None:
        tls = [
            "    map $ssl_server_name $gs_cert {",
            "        hostnames;",
            "        default default;",
            *[f"        {line}" for line in cert_lines],
            "    }",
            "    server {",
            "        listen 443 ssl default_server;",
            "        ssl_certificate /certs/$gs_cert.crt;",
            "        ssl_certificate_key /certs/$gs_cert.key;",
            "        ssl_protocols TLSv1.2 TLSv1.3;",
            *server,
            "    }",
        ]
    return "\n".join(
        [
            "worker_processes auto;",
            "events { worker_connections 1024; }",
            "http {",
            "    include /etc/nginx/mime.types;",
            "    default_type application/octet-stream;",
            "    server_tokens off;",
            "    sendfile on;",
            "    log_format gs '$remote_addr - [$time_iso8601] \"$host\" \"$request\" $status $body_bytes_sent '",
            "                  '$scheme \"$http_user_agent\"';",
            "    access_log /dev/stdout gs;",
            "    map $host $gs_site {",
            "        hostnames;",
            '        default "";',
            *[f"        {line}" for line in map_lines],
            "    }",
            "    server {",
            "        listen 80 default_server;",
            *server,
            "    }",
            *tls,
            "}",
            "",
        ]
    )


def _web(sites: list[Site], threats: list[ThreatDomain], params: BlockParams) -> dict[str, str]:
    files: dict[str, str] = {}
    site_map = [f"{name} {site.path};" for site in sites for name in site.names]
    # Greyspace's own sites live only in the overlay: the threat-intel feed breadcrumbs
    # add to, and the CA's certificate (trust_ca).
    infra_sites = [f"intel.{INFRA_ZONE}"] + ([f"pki.{INFRA_ZONE}"] if params.trust_ca else [])
    site_map += [f"{name} sites/{name};" for name in infra_sites]
    certs = None
    if params.trust_ca:
        certs = [f"{name} {zone_of(name)};" for site in sites for name in site.names]
        certs += [f"{name} {INFRA_ZONE};" for name in infra_sites]
    files["web/nginx.conf"] = _nginx(
        site_map,
        # The range's overlay (breadcrumbs) first, then the shared read-only corpus.
        "try_files /overlay/$gs_site$uri /overlay/$gs_site$uri/index.html "
        "/corpus/$gs_site$uri /corpus/$gs_site$uri/index.html =404;",
        certs,
    )
    files["web/addresses.txt"] = "".join(f"{ip}\n" for ip in sorted({str(s.ip) for s in sites}))
    files["web/entrypoint.sh"] = _ENTRYPOINT
    if threats:
        files["threat/nginx.conf"] = _nginx(
            [f"{t.fqdn} {t.fqdn};" for t in threats],
            "try_files /threat/$gs_site$uri /threat/$gs_site$uri/index.html =404;",
            [f"{t.fqdn} {zone_of(t.fqdn)};" for t in threats] if params.trust_ca else None,
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
_CA_SCRIPT = """\
#!/bin/sh
# The Greyspace root CA and one certificate per zone (generated; ca/zones.txt lists them).
# Runs once per stack start; the certificates live in the gs-certs volume, the CA's public
# certificate is also published at http://pki.gs-infra.net/root.crt (overlay).
set -eu
cd /certs
if [ ! -s root.key ]; then
  openssl ecparam -name prime256v1 -genkey -noout -out root.key
  openssl req -x509 -new -key root.key -sha256 -days 3650 -subj "/O=Greyspace/CN=Greyspace Root CA" -out root.crt
fi
mkdir -p /overlay/sites/pki.gs-infra.net
cp root.crt /overlay/sites/pki.gs-infra.net/root.crt
while read -r zone; do
  [ -n "$zone" ] || continue
  [ -s "$zone.crt" ] && continue
  openssl ecparam -name prime256v1 -genkey -noout -out "$zone.key"
  if [ "$zone" = default ]; then san="DNS:greyspace.invalid"; cn=greyspace.invalid; else san="DNS:$zone,DNS:*.$zone"; cn="$zone"; fi
  openssl req -new -key "$zone.key" -subj "/O=Greyspace/CN=$cn" -out "$zone.csr"
  printf 'subjectAltName=%s\\nbasicConstraints=CA:FALSE\\nextendedKeyUsage=serverAuth\\n' "$san" > "$zone.ext"
  openssl x509 -req -in "$zone.csr" -CA root.crt -CAkey root.key -CAcreateserial -days 825 -sha256 \\
    -extfile "$zone.ext" -out "$zone.crt"
  rm -f "$zone.csr" "$zone.ext"
done < /gs/zones.txt
chmod 0644 ./*.crt ./*.key
echo "greyspace ca: $(ls ./*.crt | wc -l) certificates"
"""

_NTP_CONF = """\
# Greyspace NTP (generated): serves its local clock to the simulated internet; never syncs out.
local stratum 3
allow all
bindcmdaddress 127.0.0.1
"""


def _local(name: str) -> str:
    return f"truenorth/greyspace-{name}:{LOCAL_TAG}"


def gs_cli_source() -> str:
    """``bin/gs``: the stack's operator CLI (gs_cli.py next to this file, run as a script)."""
    return (Path(__file__).with_name("gs_cli.py")).read_text(encoding="utf-8")


def _compose(
    manifest: Manifest,
    plan: AddressPlan,
    threats: list[ThreatDomain],
    params: BlockParams,
    corpus_dir: str,
    images_dir: str,
    host: bool,
) -> dict:
    net = "gs-public"

    def addr(ip: ipaddress.IPv4Address) -> dict:
        return {net: {"ipv4_address": str(ip)}}

    certs = params.trust_ca
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
        "image": _local("resolver"),
        "volumes": ["./resolver/unbound.conf:/etc/unbound/unbound.conf:ro", "./resolver/root.hints:/etc/unbound/root.hints:ro"],
        "networks": addr(plan.resolver),
        "depends_on": ["dns-root", "dns-tld", "dns-auth"],
        "restart": "unless-stopped",
    }
    if certs:
        services["ca"] = {
            # One-shot: makes the CA and the zone certificates, then exits.
            "build": f"{images_dir}/ca",
            "image": _local("ca"),
            "network_mode": "none",
            "entrypoint": ["/bin/sh", "/gs/make-certs.sh"],
            "volumes": ["gs-certs:/certs", "gs-overlay:/overlay", "./ca/make-certs.sh:/gs/make-certs.sh:ro",
                        "./ca/zones.txt:/gs/zones.txt:ro"],
            "restart": "no",
        }
    after_ca = {"depends_on": {"ca": {"condition": "service_completed_successfully"}}} if certs else {}
    tls_volume = ["gs-certs:/certs:ro"] if certs else []
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
            *tls_volume,
        ],
        "networks": addr(plan.webfarm),
        "restart": "unless-stopped",
        **after_ca,
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
                *tls_volume,
            ],
            "networks": addr(plan.threat),
            "restart": "unless-stopped",
            **after_ca,
        }
    services["mail"] = {
        # SMTP for every Greyspace domain (MX) and the webmail UI, on port 80 of
        # webmail.<zone>; every message lands in one shared mailbox store.
        "image": IMAGES["mailpit"],
        "environment": {
            "MP_SMTP_BIND_ADDR": "0.0.0.0:25",
            "MP_UI_BIND_ADDR": "0.0.0.0:80",
            "MP_MAX_MESSAGES": "5000",
            "MP_SMTP_AUTH_ACCEPT_ANY": "true",
            "MP_SMTP_AUTH_ALLOW_INSECURE": "true",
        },
        "networks": addr(plan.mail),
        "restart": "unless-stopped",
    }
    services["ntp"] = {
        "build": f"{images_dir}/ntp",
        "image": _local("ntp"),
        "command": ["chronyd", "-d", "-x", "-f", "/etc/chrony/chrony.conf"],
        "volumes": ["./ntp/chrony.conf:/etc/chrony/chrony.conf:ro"],
        "networks": addr(plan.ntp),
        "restart": "unless-stopped",
    }
    first_router = next(iter(plan.routers.values()))
    routes = " && ".join(
        [*(f"ip route replace {i.prefix} via {first_router}" for i in manifest.isps), f"ip route replace {plan.infra} dev eth0"]
    )
    if params.npc_profile != "off":
        services["npc"] = {
            # Simulated users (app/greyspace/npc.py profiles): they enter the public space
            # through the first ISP like a range's edge and resolve through the resolver.
            "image": npc.IMAGE,
            "cap_add": ["NET_ADMIN"],
            "dns": [str(plan.resolver)],
            "environment": {"GS_NPC_SPEED": "${GS_NPC_SPEED:-1}", "PYTHONUNBUFFERED": "1"},
            "volumes": ["./npc/npc.py:/gs/npc.py:ro", "./npc/config.json:/gs/config.json:ro", *tls_volume],
            "command": ["sh", "-c", f"{routes} ; exec python3 /gs/npc.py /gs/config.json"],
            "networks": addr(plan.npc),
            "depends_on": ["resolver", "webfarm", "mail"],
            "restart": "unless-stopped",
        }
    if not host:
        services["probe"] = {
            # A stand-in for a range's edge: routes the public space via the first ISP and
            # resolves through the Greyspace resolver. Started only with --profile probe.
            "build": f"{images_dir}/probe",
            "image": _local("probe"),
            "profiles": ["probe"],
            "cap_add": ["NET_ADMIN"],
            "dns": [str(plan.resolver)],
            "command": ["sh", "-c", f"{routes} ; exec sleep infinity"],
            "volumes": tls_volume,
            "networks": addr(plan.probe),
        }
    ipam = {"config": [{"subnet": str(plan.public), "ip_range": str(plan.infra), "gateway": str(plan.gateway)}]}
    if host:
        # gs-core: a routed bridge (no NAT) the VM forwards range traffic onto; bin/gs up
        # adds the host routes that send the ISP prefixes through the first router.
        network = {
            "driver": "bridge",
            "driver_opts": {
                "com.docker.network.bridge.name": BRIDGE,
                "com.docker.network.bridge.gateway_mode_ipv4": "routed",
            },
            "ipam": ipam,
        }
    else:
        network = {"internal": True, "ipam": ipam}
    return {
        "services": services,
        "networks": {net: network},
        "volumes": {"gs-overlay": {}, **({"gs-certs": {}} if certs else {})},
    }


def render(
    manifest: Manifest,
    params: BlockParams,
    *,
    corpus_dir: str = "${GS_CORPUS_DIR:?set GS_CORPUS_DIR to the corpus root}",
    images_dir: str = "${GS_IMAGES_DIR:?set GS_IMAGES_DIR to greyspace/images}",
    host: bool = False,
    project: str = "greyspace",
) -> Rendered:
    """Every file of the stack for this block on this corpus. Raises ConfigError.

    ``host``: render for a gs-core VM (routed bridge, no probe). ``project``: the Compose
    project ``bin/gs`` drives (``GS_PROJECT`` overrides it at run time)."""
    errors = validate(manifest, params)
    if errors:
        raise ConfigError("; ".join(errors))
    plan = address_plan(manifest)
    sites = selected_sites(manifest, params)
    threats = list(manifest.threat_domains) if params.threat_infra else []
    extra = infra_names(plan, sites, params)

    files: dict[str, str] = {}
    files.update(_dns(manifest, plan, sites, threats, extra))
    files.update(_frr(manifest, plan, threats))
    files.update(_web(sites, threats, params))
    files["ntp/chrony.conf"] = _NTP_CONF
    cert_zones = sorted(
        {zone_of(n) for s in sites for n in s.names} | {zone_of(t.fqdn) for t in threats} | {INFRA_ZONE}
    )
    if params.trust_ca:
        files["ca/make-certs.sh"] = _CA_SCRIPT
        files["ca/zones.txt"] = "".join(f"{z}\n" for z in ["default", *cert_zones])
    if params.npc_profile != "off":
        files["npc/npc.py"] = npc.agent_source()
        files["npc/config.json"] = json.dumps(
            npc.agent_config(params.npc_profile, sites, mail_host=f"mail.{INFRA_ZONE}", https=params.trust_ca),
            indent=2,
        ) + "\n"
    compose = _compose(manifest, plan, threats, params, corpus_dir, images_dir, host)
    files["compose.yaml"] = json.dumps(compose, indent=2) + "\n"
    files["bin/gs"] = gs_cli_source()
    files["gs.json"] = json.dumps(
        {
            "format": "greyspace-stack/1",
            "project": project,
            "host": host,
            "corpus_tier": manifest.tier,
            "corpus_version": manifest.version,
            "corpus_dir": corpus_dir,
            "public": str(plan.public),
            "infrastructure": str(plan.infra),
            "first_router": str(next(iter(plan.routers.values()))),
            "isp_prefixes": [str(i.prefix) for i in manifest.isps],
            "bridge": BRIDGE,
            "resolver": str(plan.resolver),
            "webfarm": str(plan.webfarm),
            "mail": str(plan.mail),
            "ntp": str(plan.ntp),
            "infra_zone": INFRA_ZONE,
            "intel_site": f"intel.{INFRA_ZONE}",
            "sites": {n: s.path for s in sites for n in s.names},
            "probe_site": sites[0].fqdn if sites else "",
            "services": sorted(k for k, v in compose["services"].items() if "profiles" not in v and k != "ca"),
        },
        indent=2,
    ) + "\n"

    zones = sorted(p.removeprefix("dns/auth/zones/db.") for p in files if p.startswith("dns/auth/zones/"))
    tlds = sorted(p.removeprefix("dns/tld/zones/db.") for p in files if p.startswith("dns/tld/zones/"))
    summary = {
        "address_plan": {
            "public": str(plan.public),
            "infrastructure": str(plan.infra),
            "resolver": str(plan.resolver),
            "root_dns": str(plan.root_dns),
            "webfarm": str(plan.webfarm),
            "threat": str(plan.threat) if threats else None,
            "mail": str(plan.mail),
            "ntp": str(plan.ntp),
        },
        "infra_names": {name: str(ip) for name, ip in extra},
        "https": params.trust_ca,
        "npc_profile": params.npc_profile,
        "isps": [
            {"name": i.name, "asn": i.asn, "prefix": str(i.prefix), "router": str(plan.routers[s])}
            for i, s in zip(manifest.isps, plan.routers, strict=True)
        ],
        "services": sorted(k for k, v in compose["services"].items() if "profiles" not in v and k != "ca"),
        "sites": len(sites),
        "site_packs": sorted({s.category for s in sites}),
        "tlds": tlds,
        "zones": zones,
        "threat_domains": [{"fqdn": t.fqdn, "ip": str(t.ip), "role": t.role} for t in threats],
        "files": sorted(files),
    }
    return Rendered(files=files, summary=summary)
