#!/usr/bin/env python3
"""Make a Keycloak realm require a second factor for privileged users. Idempotent.

Used by the installer (install/roles/tn_keycloak/tasks/mfa.yml) and by CI
(scripts/check_keycloak_mfa.sh, against the pinned Keycloak image). Standard library
only: no Keycloak SDK, so nothing here touches the MOSA adapter rule.

    keycloak_mfa.py --base http://127.0.0.1:8080/auth --realm truenorth --spec mfa-spec.json \\
        [--groups TN-Platform-Admins,TN-Instructors] [--rp-id range.example] \\
        [--exempt-users a@x,b@y] < admin-token

The admin access token is read from stdin, never from argv (argv is visible in `ps`).

What it does (each step reads the current state and changes only what differs):

1. realm attributes: acr.loa.map, OTP and WebAuthn policy, login and admin events;
2. required actions CONFIGURE_TOTP and webauthn-register enabled, not default;
3. realm role ``tn-mfa-required``, held by exactly the privileged groups;
   realm role ``tn-mfa-exempt``, held by exactly ``--exempt-users`` (test hosts only,
   decided by the installer; empty everywhere else, which strips it from everyone);
4. the ``tn-browser`` flow, built in full and only then bound as the realm browser flow,
   so a half-built flow is never in use. A flow that exists but does not match the spec
   is rebuilt under a new alias and swapped in, then the old one is deleted::

       Cookie                                   ALTERNATIVE
       Identity Provider Redirector             ALTERNATIVE
       tn-browser-forms                         ALTERNATIVE
         tn-loa1                                CONDITIONAL   level 1 (acr "pwd"), every sign-in
           Condition - Level Of Authentication  REQUIRED      1, max age 10 h
           Username Password Form               REQUIRED      amr "pwd"
           tn-mfa-by-role                       CONDITIONAL
             Condition - User Role              REQUIRED      tn-mfa-required
             Condition - User Role              REQUIRED      tn-mfa-exempt, negated
             OTP Form                           REQUIRED      amr "otp"; enrolment if none
         tn-loa2                                CONDITIONAL   level 2 (acr "mfa"), on request
           Condition - Level Of Authentication  REQUIRED      2, max age 10 h
           OTP Form                             REQUIRED      amr "otp"

   The level-1 subflow is what keeps level 2 from running on every sign-in: with no
   level reached and none requested, Keycloak 24 enters every LoA-conditioned subflow.
   OTP only: a WebAuthn/OTP ALTERNATIVE pair with neither configured ends on an error
   page on Keycloak 24 instead of enrolment. Both proven by scripts/check_keycloak_mfa.sh.
5. the AMR mapper on the ``truenorth-identity`` scope, a default scope of the web and api
   clients (the scope is created when the installer has not made it: Helm, an existing
   Keycloak);
6. last, the realm attribute ``tn.mfa.configured``, which the API checks at startup
   (app/auth_backends/keycloak_mfa_check.py, with view-realm only).

Authentication flows are not in realm-truenorth.json on purpose: nested flow executions
did not import on Keycloak 24 (scripts/check_keycloak_realm.sh).

Exit status: 0 done (``changed`` or ``ok`` printed), 1 Keycloak refused something, 2 usage.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

FLOW = "tn-browser"
AMR_SCOPE = "truenorth-identity"
AMR_MAPPER = {"name": "amr", "protocol": "openid-connect", "protocolMapper": "oidc-amr-mapper",
              "config": {"access.token.claim": "true", "id.token.claim": "true"}}
# Written last, once everything else is in place: the API's startup check
# (app/auth_backends/keycloak_mfa_check.py) reads it with realm-management view-realm only.
CONFIGURED_ATTR = "tn.mfa.configured"


class KeycloakError(RuntimeError):
    pass


class Admin:
    def __init__(self, base: str, realm: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.realm = realm
        self.token = token
        self.changed: list[str] = []

    def url(self, path: str) -> str:
        return f"{self.base}/admin/realms/{urllib.parse.quote(self.realm)}{path}"

    # Idempotent methods are retried, on a fresh connection each time (urllib opens one
    # per request), when Keycloak drops the connection or answers 502/503/504: a Keycloak
    # still settling, or behind a proxy that is. POST is never retried (it may have landed).
    RETRY_METHODS = ("GET", "PUT", "DELETE")
    RETRY_STATUS = (502, 503, 504)
    ATTEMPTS = 5

    def call(self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201, 202, 204)) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        attempts = self.ATTEMPTS if method in self.RETRY_METHODS else 1
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(
                self.url(path),
                data=data,
                method=method,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    status = resp.status
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                status = exc.code
                if status in self.RETRY_STATUS and attempt < attempts:
                    time.sleep(2 ** attempt)
                    continue
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as exc:
                if attempt == attempts:
                    raise KeycloakError(f"{method} {path}: no answer after {attempts} attempt(s) "
                                        f"({type(exc).__name__}: {exc})") from exc
                print(f"  {method} {path}: {type(exc).__name__}; retrying", file=sys.stderr)
                time.sleep(2 ** attempt)
                continue
            break
        if status not in ok:
            raise KeycloakError(f"{method} {path}: HTTP {status} {raw[:300]!r}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode()

    def get(self, path: str) -> Any:
        return self.call("GET", path)

    def note(self, what: str) -> None:
        self.changed.append(what)


def q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


# ── 1. realm attributes ───────────────────────────────────────────────
def ensure_realm(kc: Admin, spec: dict, rp_id: str | None) -> None:
    realm = kc.get("")
    want: dict[str, Any] = dict(spec["realm"])
    if rp_id:
        want["webAuthnPolicyRpId"] = rp_id
    attrs_want = {k: v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))
                  for k, v in spec.get("realm_attributes", {}).items()}
    attrs_have = realm.get("attributes") or {}
    diff = {k: v for k, v in want.items() if realm.get(k) != v and not _same_list(realm.get(k), v)}
    attr_diff = {k: v for k, v in attrs_want.items() if not _same_json(attrs_have.get(k), v)}
    if diff or attr_diff:
        body: dict[str, Any] = {"realm": kc.realm, **diff}
        if attr_diff:
            body["attributes"] = {**attrs_have, **attr_diff}
        kc.call("PUT", "", body)
        kc.note("realm settings: " + ", ".join(sorted([*diff, *attr_diff])))


def _same_list(a: Any, b: Any) -> bool:
    return isinstance(a, list) and isinstance(b, list) and sorted(map(str, a)) == sorted(map(str, b))


def _same_json(a: Any, b: str) -> bool:
    if a == b:
        return True
    try:
        return json.loads(a) == json.loads(b)
    except (TypeError, ValueError):
        return False


# ── 2. required actions ───────────────────────────────────────────────
def ensure_required_actions(kc: Admin, spec: dict) -> None:
    have = {r["alias"]: r for r in kc.get("/authentication/required-actions")}
    for alias in spec["required_actions"]:
        row = have.get(alias)
        if row is None:
            raise KeycloakError(f"required action {alias} is not registered in this Keycloak")
        if not row["enabled"] or row.get("defaultAction"):
            kc.call("PUT", f"/authentication/required-actions/{q(alias)}",
                    {**row, "enabled": True, "defaultAction": False})
            kc.note(f"required action {alias} enabled")


# ── 3. roles ──────────────────────────────────────────────────────────
def ensure_role(kc: Admin, name: str, description: str) -> dict:
    found = kc.call("GET", f"/roles/{q(name)}", ok=(200, 404))
    if not isinstance(found, dict) or "id" not in found:
        kc.call("POST", "/roles", {"name": name, "description": description})
        kc.note(f"realm role {name} created")
        found = kc.get(f"/roles/{q(name)}")
    return found


def all_groups(kc: Admin) -> dict[str, dict]:
    out: dict[str, dict] = {}

    def walk(groups: list[dict]) -> None:
        for g in groups:
            out.setdefault(g["name"], g)
            walk(g.get("subGroups") or [])

    walk(kc.get("/groups?max=1000&briefRepresentation=false"))
    return out


def ensure_group_role(kc: Admin, role: dict, groups: list[str]) -> None:
    """``role`` is held by exactly ``groups`` (a group that does not exist yet is
    reported, not created: AD brings its groups, and tn_keycloak creates local ones)."""
    present = all_groups(kc)
    holders = {g["name"] for g in kc.get(f"/roles/{q(role['name'])}/groups?max=1000")}
    for name in groups:
        if name not in present:
            print(f"warning: group {name} does not exist yet; {role['name']} applies once it does",
                  file=sys.stderr)
            continue
        if name not in holders:
            kc.call("POST", f"/groups/{present[name]['id']}/role-mappings/realm", [role])
            kc.note(f"{role['name']} -> group {name}")
    for name in holders - set(groups):
        if name in present:
            kc.call("DELETE", f"/groups/{present[name]['id']}/role-mappings/realm", [role])
            kc.note(f"{role['name']} removed from group {name}")


def find_user(kc: Admin, username: str) -> dict | None:
    rows = kc.get(f"/users?username={q(username)}&exact=true")
    return rows[0] if rows else None


def ensure_user_role(kc: Admin, role: dict, usernames: list[str]) -> None:
    """``role`` is held directly by exactly ``usernames``."""
    holders = {u["username"]: u for u in kc.get(f"/roles/{q(role['name'])}/users?max=1000")}
    want = {u.lower() for u in usernames}
    for name in want - set(holders):
        user = find_user(kc, name)
        if user is None:
            print(f"warning: user {name} does not exist; {role['name']} not granted", file=sys.stderr)
            continue
        kc.call("POST", f"/users/{user['id']}/role-mappings/realm", [role])
        kc.note(f"{role['name']} -> user {name}")
    for name, user in holders.items():
        if name not in want:
            kc.call("DELETE", f"/users/{user['id']}/role-mappings/realm", [role])
            kc.note(f"{role['name']} removed from user {name}")


# ── 4. the browser flow ───────────────────────────────────────────────
def flow_shape(kc: Admin, alias: str) -> list[tuple] | None:
    """The flow's executions as comparable tuples, or None when it does not exist."""
    flows = {f["alias"]: f for f in kc.get("/authentication/flows")}
    if alias not in flows:
        return None
    rows = kc.get(f"/authentication/flows/{q(alias)}/executions")
    shape = []
    for r in rows:
        cfg: dict[str, str] = {}
        if r.get("authenticationConfig"):
            cfg = (kc.get(f"/authentication/config/{r['authenticationConfig']}") or {}).get("config") or {}
        shape.append((r["level"], r.get("displayName") if r.get("authenticationFlow") else r.get("providerId"),
                      r["requirement"], tuple(sorted(cfg.items()))))
    return shape


def expected_shape(spec_flow: list[dict], level: int = 0) -> list[tuple]:
    out: list[tuple] = []
    for node in spec_flow:
        if "flow" in node:
            out.append((level, node["flow"], node["requirement"], ()))
            out.extend(expected_shape(node["executions"], level + 1))
        else:
            cfg = tuple(sorted((node.get("config") or {}).items()))
            out.append((level, node["provider"], node["requirement"], cfg))
    return out


def build_flow(kc: Admin, alias: str, spec_flow: list[dict], suffix: str) -> None:
    kc.call("POST", "/authentication/flows", {
        "alias": alias, "description": "TrueNorth browser sign-in: second factor for privileged roles",
        "providerId": "basic-flow", "topLevel": True, "builtIn": False,
    })
    _add_executions(kc, alias, spec_flow, suffix)
    kc.note(f"flow {alias} built")


def _add_executions(kc: Admin, parent: str, nodes: list[dict], suffix: str) -> None:
    for node in nodes:
        if "flow" in node:
            sub = node["flow"] + suffix
            kc.call("POST", f"/authentication/flows/{q(parent)}/executions/flow",
                    {"alias": sub, "type": "basic-flow", "provider": "registration-page-form",
                     "description": node.get("description", "")})
            ex = _last_execution(kc, parent, display=sub)
            _set_requirement(kc, parent, ex, node["requirement"])
            _add_executions(kc, sub, node["executions"], suffix)
        else:
            kc.call("POST", f"/authentication/flows/{q(parent)}/executions/execution",
                    {"provider": node["provider"]})
            ex = _last_execution(kc, parent, provider=node["provider"])
            _set_requirement(kc, parent, ex, node["requirement"])
            if node.get("config"):
                kc.call("POST", f"/authentication/executions/{ex['id']}/config",
                        {"alias": f"{node.get('config_alias', node['provider'])}{suffix}-{ex['id'][:8]}",
                         "config": node["config"]})


def _last_execution(kc: Admin, flow: str, *, provider: str | None = None, display: str | None = None) -> dict:
    rows = [r for r in kc.get(f"/authentication/flows/{q(flow)}/executions") if r["level"] == 0]
    for r in reversed(rows):
        if provider and r.get("providerId") == provider and not r.get("authenticationFlow"):
            return r
        if display and r.get("authenticationFlow") and r.get("displayName") == display:
            return r
    raise KeycloakError(f"execution {provider or display} not found in {flow} after adding it")


def _set_requirement(kc: Admin, flow: str, ex: dict, requirement: str) -> None:
    if ex["requirement"] != requirement:
        kc.call("PUT", f"/authentication/flows/{q(flow)}/executions", {**ex, "requirement": requirement})


def _rename_shape(shape: list[tuple] | None, suffix: str) -> list[tuple] | None:
    """Subflow display names carry the build suffix; compare without it."""
    if shape is None:
        return None
    return [(lvl, name[: -len(suffix)] if suffix and isinstance(name, str) and name.endswith(suffix) else name, req, cfg)
            for (lvl, name, req, cfg) in shape]


def ensure_flow(kc: Admin, spec: dict) -> None:
    want = expected_shape(spec["flow"])
    realm = kc.get("")
    bound = realm.get("browserFlow")
    flows = {f["alias"]: f for f in kc.get("/authentication/flows")}
    candidates = [a for a in flows if a == FLOW or a.startswith(FLOW + "--")]
    good = None
    for alias in sorted(candidates, key=lambda a: a != bound):
        suffix = alias[len(FLOW):]
        if _rename_shape(flow_shape(kc, alias), suffix) == want:
            good = alias
            break
    if good is None:
        # Build a new copy, complete, before anything points at it.
        n = 1
        while f"{FLOW}--{n}" in flows:
            n += 1
        good = FLOW if FLOW not in flows else f"{FLOW}--{n}"
        build_flow(kc, good, spec["flow"], good[len(FLOW):])
        got = _rename_shape(flow_shape(kc, good), good[len(FLOW):])
        if got != want:
            raise KeycloakError(f"flow {good} does not match the spec after building it:\n{got}\n!=\n{want}")
    if bound != good:
        kc.call("PUT", "", {"realm": kc.realm, "browserFlow": good})
        kc.note(f"browserFlow -> {good}")
    # Remove superseded copies (never the bound one, never built-ins).
    for alias in candidates:
        if alias != good:
            kc.call("DELETE", f"/authentication/flows/{flows[alias]['id']}", ok=(204, 400, 409))
            kc.note(f"flow {alias} removed")


# ── 5. the AMR mapper, on a default scope of the user-facing clients ──
def ensure_amr_mapper(kc: Admin, clients: list[str]) -> None:
    """Tokens say how the user signed in (amr). The installer puts the mapper on the
    truenorth-identity scope (tn_kc_protocol_mappers); a realm configured only by this
    script (Helm, an existing Keycloak) gets the scope here, as a default scope."""
    scopes = {sc["name"]: sc for sc in kc.get("/client-scopes")}
    scope = scopes.get(AMR_SCOPE)
    if scope is None:
        kc.call("POST", "/client-scopes", {"name": AMR_SCOPE, "protocol": "openid-connect",
                                           "attributes": {"include.in.token.scope": "true",
                                                          "display.on.consent.screen": "false"},
                                           "protocolMappers": [AMR_MAPPER]})
        kc.note(f"client scope {AMR_SCOPE} created with the AMR mapper")
        scope = {sc["name"]: sc for sc in kc.get("/client-scopes")}[AMR_SCOPE]
    elif "oidc-amr-mapper" not in [m.get("protocolMapper") for m in scope.get("protocolMappers") or []]:
        kc.call("POST", f"/client-scopes/{scope['id']}/protocol-mappers/models", AMR_MAPPER)
        kc.note(f"AMR mapper added to {AMR_SCOPE}")
    for client_id in clients:
        found = kc.get(f"/clients?clientId={q(client_id)}")
        if not found:
            continue
        cid = found[0]["id"]
        if scope["id"] not in [d["id"] for d in kc.get(f"/clients/{cid}/default-client-scopes")]:
            kc.call("PUT", f"/clients/{cid}/default-client-scopes/{scope['id']}")
            kc.note(f"{AMR_SCOPE} -> default scope of {client_id}")


# ── optional: the API's own Admin API client (Helm; the installer makes it itself) ──
API_CLIENT_ROLES = ("view-realm", "manage-users")


def ensure_api_client(kc: Admin, client_id: str, secret: str) -> None:
    """A confidential client whose service account holds exactly realm-management
    view-realm and manage-users: what the API's startup check (view-realm) and /ad-sync
    (manage-users) use, never the master admin. Secret from TN_API_ADMIN_CLIENT_SECRET."""
    found = kc.get(f"/clients?clientId={q(client_id)}")
    body = {"clientId": client_id, "protocol": "openid-connect", "publicClient": False,
            "serviceAccountsEnabled": True, "standardFlowEnabled": False, "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False, "redirectUris": [], "webOrigins": [], "secret": secret}
    if not found:
        kc.call("POST", "/clients", body)
        kc.note(f"client {client_id} created")
        found = kc.get(f"/clients?clientId={q(client_id)}")
    cid = found[0]["id"]
    if (kc.get(f"/clients/{cid}/client-secret") or {}).get("value") != secret:
        kc.call("PUT", f"/clients/{cid}", {**found[0], "secret": secret})
        kc.note(f"client {client_id} secret set")
    sa = kc.get(f"/clients/{cid}/service-account-user")["id"]
    rm = kc.get("/clients?clientId=realm-management")[0]["id"]
    have = {r["name"]: r for r in kc.get(f"/users/{sa}/role-mappings/clients/{rm}")}
    grant = [r for r in kc.get(f"/users/{sa}/role-mappings/clients/{rm}/available")
             if r["name"] in API_CLIENT_ROLES and r["name"] not in have]
    if grant:
        kc.call("POST", f"/users/{sa}/role-mappings/clients/{rm}", grant)
        kc.note(f"{client_id} granted {', '.join(r['name'] for r in grant)}")
    extra = [r for n, r in have.items() if n not in API_CLIENT_ROLES]
    if extra:
        kc.call("DELETE", f"/users/{sa}/role-mappings/clients/{rm}", extra)
        kc.note(f"{client_id} lost {', '.join(r['name'] for r in extra)} (least privilege)")


# ── 6. the marker the API checks at startup ───────────────────────────
def mark_configured(kc: Admin, spec: dict) -> None:
    import hashlib

    want = "1:" + hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    attrs = kc.get("").get("attributes") or {}
    if attrs.get(CONFIGURED_ATTR) != want:
        kc.call("PUT", "", {"realm": kc.realm, "attributes": {**attrs, CONFIGURED_ATTR: want}})
        kc.note(f"realm attribute {CONFIGURED_ATTR}={want}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--base", required=True, help="Keycloak base URL, e.g. http://127.0.0.1:8080/auth")
    ap.add_argument("--realm", required=True)
    ap.add_argument("--spec", required=True, help="mfa-spec.json")
    ap.add_argument("--groups", help="comma-separated privileged groups (default: the spec's)")
    ap.add_argument("--rp-id", help="WebAuthn relying-party id (the site's FQDN)")
    ap.add_argument("--ensure-api-client", metavar="CLIENT_ID",
                    help="also make the API's Admin API client (view-realm, manage-users); its secret is read "
                         "from TN_API_ADMIN_CLIENT_SECRET (Helm hook; the installer makes it itself)")
    ap.add_argument("--exempt-users", default="",
                    help="comma-separated usernames that skip the second factor (test hosts only)")
    args = ap.parse_args(argv)

    token = sys.stdin.readline().strip()
    if not token:
        print("no admin token on stdin", file=sys.stderr)
        return 2
    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)
    groups = [g.strip() for g in (args.groups.split(",") if args.groups else spec["groups"]) if g.strip()]
    exempt = [u.strip() for u in args.exempt_users.split(",") if u.strip()]

    kc = Admin(args.base, args.realm, token)
    try:
        ensure_realm(kc, spec, args.rp_id)
        ensure_required_actions(kc, spec)
        required = ensure_role(kc, spec["required_role"], "Signs in with a second factor (TrueNorth privileged roles)")
        ensure_group_role(kc, required, groups)
        exempt_role = ensure_role(kc, spec["exempt_role"], "Demo staff on a test host: no second factor")
        ensure_user_role(kc, exempt_role, exempt)
        ensure_flow(kc, spec)
        ensure_amr_mapper(kc, spec.get("amr_clients", ["truenorth-web", "truenorth-api"]))
        if args.ensure_api_client:
            secret = os.environ.get("TN_API_ADMIN_CLIENT_SECRET", "")
            if len(secret) < 16:
                raise KeycloakError("--ensure-api-client needs TN_API_ADMIN_CLIENT_SECRET (16+ characters)")
            ensure_api_client(kc, args.ensure_api_client, secret)
        mark_configured(kc, spec)
    except KeycloakError as exc:
        print(f"keycloak_mfa: {exc}", file=sys.stderr)
        return 1
    for line in kc.changed:
        print(f"  {line}")
    print("changed" if kc.changed else "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
