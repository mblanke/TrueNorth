"""Contract: the nginx configs in front of the platform keep their security posture.

Two configs serve the SPA, the API and Keycloak to browsers:

* the edge, ``infra/platform/nginx`` (TLS on 443, production), and
* the web container's own ``control-plane/web/nginx.conf`` (plain :8080, behind the edge
  in production and published directly as :4200 in development).

The configs are parsed here (a small tokenizer; ``include /etc/nginx/snippets/...`` is
resolved from the repo) and requests are routed with nginx's location rules, so the
assertions are about what a given URL actually gets, not about strings in a file.
Syntax itself is checked with ``nginx -t`` (see the commit that introduced this file).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EDGE = REPO / "infra/platform/nginx"
EDGE_MAIN = EDGE / "nginx.conf"
EDGE_SITE = EDGE / "conf.d/truenorth.conf"
EDGE_TLS = EDGE / "conf.d/ssl-params.conf"
EDGE_REDIRECT = EDGE / "conf.d/default.conf"
SNIPPETS = EDGE / "snippets"
WEB = REPO / "control-plane/web/nginx.conf"
OPENAPI = REPO / "docs/interfaces/openapi.json"
SILENT_SSO = REPO / "control-plane/web/src/assets/silent-check-sso.html"

SECURITY_HEADERS = (
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Content-Security-Policy",
    "Referrer-Policy",
    "Permissions-Policy",
)


# ── A minimal nginx config parser ────────────────────────────────────────────


@dataclass
class Directive:
    name: str
    args: list[str]
    block: list[Directive] | None = None
    source: str = ""

    def all(self, name: str) -> list[Directive]:
        return [d for d in self.block or [] if d.name == name]

    def first(self, name: str) -> Directive | None:
        found = self.all(name)
        return found[0] if found else None

    def walk(self):
        for d in self.block or []:
            yield d
            yield from d.walk()


@dataclass
class Root(Directive):
    name: str = "root"
    args: list[str] = field(default_factory=list)


_TOKEN = re.compile(r"""\s+|#[^\n]*|"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|([{};])|([^\s{};"'#]+)""")


def _tokens(text: str):
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        assert m, f"cannot tokenize at {text[pos : pos + 40]!r}"
        pos = m.end()
        if m.group(1) is not None:
            yield ("word", m.group(1))
        elif m.group(2) is not None:
            yield ("word", m.group(2))
        elif m.group(3):
            yield ("punct", m.group(3))
        elif m.group(4):
            yield ("word", m.group(4))


def parse(path: Path) -> Root:
    toks = list(_tokens(path.read_text()))
    root = Root(block=[], source=str(path))
    stack: list[list[Directive]] = [root.block]
    words: list[str] = []
    for kind, val in toks:
        if kind == "word":
            words.append(val)
        elif val == ";":
            name, *args = words
            words = []
            if name == "include" and args[0].startswith("/etc/nginx/snippets/"):
                stack[-1].extend(parse(SNIPPETS / args[0].rsplit("/", 1)[1]).block)
            else:
                stack[-1].append(Directive(name, args, source=str(path)))
        elif val == "{":
            name, *args = words
            words = []
            d = Directive(name, args, block=[], source=str(path))
            stack[-1].append(d)
            stack.append(d.block)
        else:  # "}"
            assert not words, f"unterminated directive {words} in {path}"
            stack.pop()
    assert len(stack) == 1 and not words, f"unbalanced braces in {path}"
    return root


def servers(root: Root) -> list[Directive]:
    return [d for d in root.walk() if d.name == "server" and d.block is not None]


def ssl_server(root: Root) -> Directive:
    (srv,) = [s for s in servers(root) if any("ssl" in d.args for d in s.all("listen"))]
    return srv


def route(server: Directive, uri: str) -> Directive:
    """The location nginx picks for ``uri`` (top-level locations only)."""
    locs = server.all("location")
    for loc in locs:
        if loc.args[0] == "=" and loc.args[1] == uri:
            return loc
    prefixes = [(loc.args[-1], loc) for loc in locs if loc.args[0] == "^~" or len(loc.args) == 1]
    prefixes = [(p, loc) for p, loc in prefixes if not p.startswith("@") and uri.startswith(p)]
    best = max(prefixes, key=lambda t: len(t[0]))[1] if prefixes else None
    if best is not None and best.args[0] == "^~":
        return best
    for loc in locs:
        if loc.args[0] in ("~", "~*"):
            flags = re.IGNORECASE if loc.args[0] == "~*" else 0
            if re.search(loc.args[1], uri, flags):
                return loc
    assert best is not None, f"no location for {uri}"
    return best


def add_headers(block: Directive) -> dict[str, str]:
    return {d.args[0].lower(): d.args[1] for d in block.all("add_header")}


def headers_for(server: Directive, loc: Directive) -> dict[str, str]:
    """nginx's add_header inheritance: a location with any add_header uses only its own."""
    own = add_headers(loc)
    return own if own else add_headers(server)


def is_denied(loc: Directive, status: str = "404") -> bool:
    """`return 404` outright, or the management-network guard in front of the proxy."""
    if any(d.args == [status] for d in loc.all("return")):
        return True
    for cond in loc.all("if"):
        if cond.args[:3] == ["($tn_management_client", "!=", "1)"] and any(
            d.args == [status] for d in cond.all("return")
        ):
            return True
    return False


def _geo(variable: str) -> Directive:
    access = parse(EDGE / "conf.d/edge-access.conf")
    return next(d for d in access.walk() if d.name == "geo" and variable in d.args)


@pytest.fixture(scope="module")
def edge() -> Directive:
    return ssl_server(parse(EDGE_SITE))


@pytest.fixture(scope="module")
def web() -> Directive:
    (srv,) = servers(parse(WEB))
    return srv


def _csp_values() -> list[tuple[str, str]]:
    out = []
    for path in [EDGE_MAIN, *sorted((EDGE / "conf.d").glob("*.conf")), *sorted(SNIPPETS.glob("*.conf")), WEB]:
        for d in parse(path).walk():
            if d.name == "add_header" and d.args[0].lower() == "content-security-policy":
                out.append((str(path), d.args[1]))
            if d.name == "map" and d.block is not None and "$tn_csp" in d.args:
                out.extend((str(path), m.args[-1]) for m in d.block)
    return out


# ── 1. CORS ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [EDGE_SITE, WEB, *sorted(SNIPPETS.glob("*.conf"))], ids=lambda p: p.name)
def test_no_origin_reflection(path):
    for d in parse(path).walk():
        if d.name == "add_header":
            assert not d.args[0].lower().startswith("access-control-"), (
                f"{path}: CORS belongs to the API's allowlist (CORS_ORIGINS); the SPA is same-origin"
            )
    assert "$http_origin" not in path.read_text()


# ── 2. Operator surfaces ─────────────────────────────────────────────────────

INTERNAL = [
    "/api/docs",
    "/api/docs/oauth2-redirect",
    "/api/redoc",
    "/api/openapi.json",
    "/api/v1/openapi.json",
    "/api/health/deep",
    "/api/DOCS",
    "/api/metrics",
    "/api/v1/metrics/",
    "/auth/admin/",
    "/auth/admin/master/console/",
    "/auth/admin/realms/truenorth/users",
    "/auth/ADMIN/",
    "/auth/admin;jsessionid=x/master/console/",
    "/auth/realms/master/protocol/openid-connect/auth",
    "/auth/realms/master/protocol/openid-connect/token",
    "/auth/metrics",
    "/auth/health/ready",
]


@pytest.mark.parametrize("uri", INTERNAL + ["/ai/docs", "/ai/ai/scenario-draft", "/flower/"])
def test_edge_denies_operator_surfaces(edge, uri):
    loc = route(edge, uri)
    assert is_denied(loc), f"{uri} reaches {loc.args} on the edge without the management-network guard"


@pytest.mark.parametrize("uri", INTERNAL)
def test_web_container_denies_operator_surfaces(web, uri):
    assert is_denied(route(web, uri)), uri


def test_management_list_is_empty_as_shipped():
    entries = [
        line.split("#")[0].strip()
        for line in (SNIPPETS / "management-cidrs.conf").read_text().splitlines()
        if line.split("#")[0].strip()
    ]
    assert entries == [], "operator surfaces must be default-deny; sites add their own CIDRs"
    assert _geo("$tn_management_client").first("default").args == ["0"]


@pytest.mark.parametrize(
    "uri",
    ["/api/courses", "/api/health", "/api/ai/scenario-draft", "/auth/realms/truenorth/protocol/openid-connect/auth", "/"],
)
def test_ordinary_paths_are_not_caught_by_the_guard(edge, uri):
    assert not is_denied(route(edge, uri)), uri


@pytest.mark.parametrize(
    "uri,upstream",
    [
        ("/auth/resources/26.0/login/keycloak.v2/css/styles.css", "keycloak_backend"),
        ("/auth/resources/26.0/login/keycloak.v2/js/menu.js", "keycloak_backend"),
        ("/api/tickets/7/attachments/screenshot.png", "api_backend"),
        ("/api/arc2/runs/x/files/page.js", "api_backend"),
        ("/main-ABCDEF.js", "web_frontend"),
    ],
)
def test_asset_extensions_reach_their_own_backend(edge, uri, upstream):
    """The edge's static-asset regex used to send /auth/... and /api/... files to the SPA."""
    loc = route(edge, uri)
    passes = [d.args[0] for d in loc.walk() if d.name == "proxy_pass"]
    assert passes == [f"http://{upstream}"], (uri, loc.args)


# ── 3. Content-Security-Policy ───────────────────────────────────────────────


def test_no_csp_allows_eval():
    values = _csp_values()
    assert values
    for path, value in values:
        assert "unsafe-eval" not in value, path


def _directive(csp: str, name: str) -> list[str]:
    for part in csp.split(";"):
        bits = part.split()
        if bits and bits[0] == name:
            return bits[1:]
    return []


def test_spa_policy_allows_no_inline_script_but_the_hashed_ones(edge):
    csp = headers_for(edge, route(edge, "/"))["content-security-policy"]
    script = _directive(csp, "script-src")
    assert "'unsafe-inline'" not in script and "'unsafe-eval'" not in script
    assert _directive(csp, "object-src") == ["'none'"]
    assert _directive(csp, "frame-ancestors") == ["'self'"]
    # keycloak-js's silent SSO target is one inline script; its hash must track the file.
    body = re.search(r"<script>(.*?)</script>", SILENT_SSO.read_text(), re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in script, (
        "silent-check-sso.html changed: update its hash in snippets/security-headers.conf"
    )
    # Angular's critical-CSS loader: onload="this.media='all'" on the stylesheet link.
    onload = base64.b64encode(hashlib.sha256(b"this.media='all'").digest()).decode()
    assert "'unsafe-hashes'" in script and f"'sha256-{onload}'" in script


def test_spa_forms_post_only_to_this_host_its_moodle_included(edge):
    """"Open in Moodle" POSTs the sign-in ticket to the installer's Moodle, which has its own
    origin on this host (https://<host>:<tn_moodle_port>); 'self' alone blocked it."""
    csp = headers_for(edge, route(edge, "/"))["content-security-policy"]
    assert _directive(csp, "form-action") == ["'self'", "https://$host:*"]


def test_silent_sso_page_and_previews_can_be_framed_by_the_spa(edge):
    sso = headers_for(edge, route(edge, "/assets/silent-check-sso.html"))
    assert sso["x-frame-options"] == "SAMEORIGIN"
    previews = headers_for(edge, route(edge, "/assets/previews/development.html"))
    assert _directive(previews["content-security-policy"], "sandbox") == ["allow-scripts"]


# ── 4. Security headers in every block that sets headers ─────────────────────


def _header_blocks(server: Directive):
    yield "server", server
    for loc in server.all("location"):
        if loc.all("add_header"):
            yield " ".join(loc.args), loc


def test_edge_every_header_block_carries_the_full_set(edge):
    blocks = list(_header_blocks(edge))
    assert len(blocks) > 3
    for name, block in blocks:
        headers = add_headers(block)
        missing = [h for h in SECURITY_HEADERS if h.lower() not in headers]
        assert not missing, f"location {name} drops {missing}: include a snippets/security-headers*.conf"
        assert "includeSubDomains" in headers["strict-transport-security"]
        assert all(d.args[-1] == "always" for d in block.all("add_header")), name


def test_edge_http_level_sets_no_headers():
    """An http-level add_header is silently dropped by every server that sets its own."""
    for path in [EDGE_MAIN, *sorted((EDGE / "conf.d").glob("*.conf"))]:
        top = parse(path)
        http = top.first("http") or top
        assert not http.all("add_header"), path


@pytest.mark.parametrize("uri", ["/api/courses", "/auth/realms/truenorth/account", "/api/schedule/feed/t.ics"])
def test_edge_proxied_backends_keep_their_own_csp(edge, uri):
    """API and Keycloak set CSP/X-Frame-Options per page; the edge only fills a gap."""
    headers = headers_for(edge, route(edge, uri))
    assert headers["content-security-policy"] == "$tn_csp_fallback"
    assert headers["x-frame-options"] == "$tn_xfo_fallback"


def test_web_container_sets_headers_once_at_server_level(web):
    for loc in web.all("location"):
        assert not loc.all("add_header"), f"location {loc.args} would drop the server-level headers"
    headers = add_headers(web)
    for h in SECURITY_HEADERS[1:]:
        assert h.lower() in headers, h


# ── 5. TLS and server identity ───────────────────────────────────────────────


def test_server_tokens_off():
    http = parse(EDGE_MAIN).first("http")
    assert http.first("server_tokens").args == ["off"]
    (web_server,) = servers(parse(WEB))
    assert web_server.first("server_tokens").args == ["off"]


def test_tls_protocols_and_ciphers(edge):
    tls = parse(EDGE_TLS)
    assert tls.first("ssl_protocols").args == ["TLSv1.2", "TLSv1.3"]
    ciphers = tls.first("ssl_ciphers").args[0].split(":")
    assert ciphers and all(c.startswith("ECDHE-") and ("GCM" in c or "CHACHA20" in c) for c in ciphers), ciphers
    assert tls.first("ssl_stapling").args == ["on"]
    assert tls.first("resolver").args[0] != "8.8.8.8"
    # The 443 server inherits these; it must not weaken or re-include them.
    for name in ("ssl_protocols", "ssl_ciphers", "include"):
        assert edge.first(name) is None, name


def test_plain_http_only_redirects():
    (srv,) = servers(parse(EDGE_REDIRECT))
    root = route(srv, "/api/courses")
    assert root.first("return").args == ["301", "https://$host$request_uri"]


# ── 6. Request limits ────────────────────────────────────────────────────────


def _upload_paths() -> list[str]:
    spec = json.loads(OPENAPI.read_text())
    out = []
    for path, ops in spec["paths"].items():
        for op in ops.values():
            if "multipart/form-data" in (op.get("requestBody") or {}).get("content", {}):
                out.append("/api" + re.sub(r"\{[^}]+\}", "x1", path))
    return sorted(set(out))


def _body_limit(server: Directive, loc: Directive, http_default: str) -> str:
    d = loc.first("client_max_body_size") or server.first("client_max_body_size")
    return d.args[0] if d else http_default


def test_every_upload_route_gets_the_upload_limit(edge, web):
    uploads = _upload_paths()
    assert len(uploads) >= 10
    for uri in uploads:
        assert _body_limit(edge, route(edge, uri), "1m") == "50M", f"edge caps {uri} below 50M"
        assert _body_limit(web, route(web, uri), "1m") == "50M", f"web caps {uri} below 50M"


@pytest.mark.parametrize("uri", ["/api/courses", "/api/tickets/1", "/api/ranges/1/documents/2"])
def test_other_api_routes_keep_the_json_limit(edge, web, uri):
    assert _body_limit(edge, route(edge, uri), "1m") == "10M"
    assert _body_limit(web, route(web, uri), "1m") == "10M"


def test_edge_default_body_limit_is_small():
    assert parse(EDGE_MAIN).first("http").first("client_max_body_size").args == ["1m"]


def test_rate_limit_zones_are_per_client_address():
    http = parse(EDGE_MAIN).first("http")
    zones = {d.args[1].split("=")[1].split(":")[0]: d.args[0] for d in http.all("limit_req_zone")}
    for zone in ("api_zone", "auth_zone", "token_zone", "feed_zone", "noise_agent_zone"):
        assert zones.get(zone) == "$binary_remote_addr", zone
    assert http.first("limit_req_status").args == ["429"]
    assert http.first("proxy_connect_timeout") and http.first("proxy_read_timeout")


@pytest.mark.parametrize(
    "uri,zone",
    [
        ("/auth/realms/truenorth/protocol/openid-connect/token", "token_zone"),
        ("/api/schedule/feed/abc.ics", "feed_zone"),
        ("/api/v1/schedule/feed/abc.ics", "feed_zone"),
        ("/api/noise/agent/plan", "noise_agent_zone"),
        ("/api/noise/agent/report", "noise_agent_zone"),
    ],
)
def test_sensitive_endpoints_have_their_own_rate(edge, uri, zone):
    loc = route(edge, uri)
    assert loc.first("limit_req").args[0] == f"zone={zone}", (uri, loc.args)


def test_noise_agent_channel_is_allowlisted(edge):
    loc = route(edge, "/api/noise/agent/plan")
    (guard,) = loc.all("if")
    assert guard.args[:3] == ["($tn_noise_agent_client", "!=", "1)"]
    assert guard.first("return").args == ["403"]
    # One default, from the operator-editable list (allow-all as shipped, documented there).
    (default,) = _geo("$tn_noise_agent_client").all("default")
    assert default.source.endswith("snippets/noise-agent-cidrs.conf") and default.args == ["1"]
