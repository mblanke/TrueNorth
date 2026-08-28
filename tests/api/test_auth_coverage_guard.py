"""Static guards on authentication coverage and role vocabulary.

Two bug classes live here, both of which have actually happened in this repo:

1. **A router mounted with no authentication at all.** `scheduling.py` shipped
   with all ten `/schedule` endpoints open; `ad_sync.py` shipped with three,
   one of which returned the LDAP server, base DN and search bases to anyone
   who asked. Nothing failed — the endpoints worked perfectly, for everybody.

2. **Role vocabulary drift.** The frontend and the Keycloak realm both used a
   `trainee` role that does not exist in `UserRole`. The realm's `defaultRoles`
   assigned it to every new user, so every federated account arrived holding a
   role the API had never heard of.

Behavioural tests only cover endpoints somebody thought to write a test for.
These cover every route by construction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.routing import APIRoute

REPO_ROOT = Path(__file__).resolve().parents[2]
API = REPO_ROOT / "control-plane/api"
sys.path.insert(0, str(API))

# Routes that are deliberately reachable without a resolved TrueNorth account.
# Anything added here needs a reason, in the comment beside it.
PUBLIC_PATHS: set[str] = {
    "/health",
    "/health/ready",
    "/health/live",
    "/health/deep",
    "/metrics",
    "/",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    # Identity intake. These are authenticated by TOKEN but must work before a
    # `users` row exists — that is the entire point of the registration flow.
    "/auth/me",
    "/registration",
    "/registration/mine",
    # LTI 1.3 launches are authenticated by signed JWT from the platform, and
    # xAPI/LRS endpoints by their own credential.
}

PUBLIC_PREFIXES: tuple[str, ...] = (
    "/lti",
    "/xapi",
    "/ws",  # websockets authenticate on the socket, not the route
    "/static",
)

# Names that indicate a route resolves an identity somewhere in its dependency
# tree. `require_permission`/`require_role` both Depend on get_current_user.
AUTH_MARKERS = (
    "get_current_user",
    "get_token_identity",
    "require_permission",
    "require_role",
    "_check",  # the inner closure require_permission/require_role return
)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _dependency_names(route: APIRoute) -> set[str]:
    """Every callable name in a route's flattened dependency tree."""
    names: set[str] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            names.add(getattr(dep.call, "__name__", ""))
        stack.extend(dep.dependencies)
    return names


# ── Auth debt ratchet ──────────────────────────────────────────────────
# Five routers once shipped with NO authentication on any route — 86 endpoints,
# including POST /hypervisors/connections (which stores hypervisor credentials)
# and /proxmox/vms/{node}/{vmid}/stop (which halts a virtual machine). They are
# now guarded at router level.
#
# This map is EMPTY and should stay that way. It exists so the ratchet remains
# in place: any new unguarded route fails immediately, and a regression cannot
# be waved through by adding a number here without someone noticing.
KNOWN_UNGUARDED: dict[str, int] = {}


def _router_prefix(path: str) -> str:
    parts = path.split("/")
    return f"/{parts[1]}" if len(parts) > 1 else "/"


def test_every_route_requires_authentication():
    import os

    os.environ.setdefault("AUTH_DISABLED", "true")
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    from app.main import app

    unguarded: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if _is_public(route.path):
            continue
        if not AUTH_MARKERS_present(route):
            methods = ",".join(sorted(route.methods - {"HEAD", "OPTIONS"}))
            unguarded.append(f"{methods} {route.path}")

    counts: dict[str, int] = {}
    novel: list[str] = []
    for entry in unguarded:
        prefix = _router_prefix(entry.split(" ", 1)[1])
        counts[prefix] = counts.get(prefix, 0) + 1
        if prefix not in KNOWN_UNGUARDED:
            novel.append(entry)

    assert not novel, (
        "NEW routes with no authentication dependency — these are callable by "
        "anyone who can reach the API:\n  "
        + "\n  ".join(sorted(novel))
        + "\n\nAdd get_current_user / require_permission / require_role, or add "
        "the path to PUBLIC_PATHS with a reason."
    )

    grown = {
        prefix: (count, KNOWN_UNGUARDED[prefix])
        for prefix, count in counts.items()
        if count > KNOWN_UNGUARDED[prefix]
    }
    assert not grown, (
        "Unguarded route count grew in a router that already carries auth debt: "
        + ", ".join(f"{p} {now} > {was}" for p, (now, was) in sorted(grown.items()))
        + "\n\nDo not add unauthenticated routes to these routers."
    )


def test_auth_debt_ratchet_is_current():
    """The recorded debt must not overstate reality — otherwise it never shrinks."""
    import os

    os.environ.setdefault("AUTH_DISABLED", "true")
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    from app.main import app

    counts: dict[str, int] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or _is_public(route.path):
            continue
        if not AUTH_MARKERS_present(route):
            prefix = _router_prefix(route.path)
            counts[prefix] = counts.get(prefix, 0) + 1

    stale = {p: (counts.get(p, 0), n) for p, n in KNOWN_UNGUARDED.items() if counts.get(p, 0) < n}
    assert not stale, (
        "KNOWN_UNGUARDED is out of date — routes were secured but the ratchet was "
        "not lowered: "
        + ", ".join(f"{p}: now {now}, recorded {was}" for p, (now, was) in sorted(stale.items()))
        + "\n\nLower these numbers so the improvement sticks."
    )


def AUTH_MARKERS_present(route: APIRoute) -> bool:  # noqa: N802 - reads as a predicate
    return bool(_dependency_names(route) & set(AUTH_MARKERS))


def test_the_guard_actually_detects_something():
    """A guard that cannot fail is not a guard."""
    from app.auth import get_current_user
    from fastapi import APIRouter, Depends, FastAPI

    probe = FastAPI()
    router = APIRouter()

    @router.get("/wide-open")
    def wide_open():
        return {}

    @router.get("/guarded")
    def guarded(user=Depends(get_current_user)):
        return {}

    probe.include_router(router)
    routes = {r.path: r for r in probe.routes if isinstance(r, APIRoute)}
    assert not AUTH_MARKERS_present(routes["/wide-open"])
    assert AUTH_MARKERS_present(routes["/guarded"])


# ── Role vocabulary ────────────────────────────────────────────────────
def test_keycloak_realm_roles_match_the_backend_enum():
    """The realm must not define a role the API has never heard of."""
    from app.models import UserRole

    realm = json.loads((REPO_ROOT / "infra/keycloak/realm-truenorth.json").read_text())
    realm_roles = {r["name"] for r in realm["roles"]["realm"]}
    backend_roles = {r.value for r in UserRole}

    unknown = realm_roles - backend_roles
    assert not unknown, (
        f"Keycloak realm defines role(s) {sorted(unknown)} that are not in UserRole "
        f"{sorted(backend_roles)}. A user granted one of these arrives holding a role "
        "the API cannot map."
    )


def test_keycloak_default_role_is_a_real_role():
    from app.models import UserRole

    realm = json.loads((REPO_ROOT / "infra/keycloak/realm-truenorth.json").read_text())
    defaults = set(realm.get("defaultRoles") or [])
    backend_roles = {r.value for r in UserRole}
    assert defaults <= backend_roles, (
        f"defaultRoles {sorted(defaults - backend_roles)} are not valid UserRole values. "
        "This is assigned to EVERY new federated user."
    )
