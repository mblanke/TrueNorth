"""Static guard: no route may be "signed in, therefore allowed" by accident.

`test_auth_coverage_guard.py` proves every route resolves an identity. It cannot tell
`Depends(get_current_user)` — any account at all, a Student included — from a route
that checks what that account may do. That gap is how the Curriculum Forge shipped with
create, upload, delete and RAG search open to every Student, and how the MESL (the
exercise-control plan, expected actions and all) was readable by the trainees it
assesses.

This guard lists every route whose dependency tree reaches `get_current_user` but no
permission or role check, and fails unless the route is in `SIGN_IN_ONLY` with a reason.
Each reason starts with one of:

- ``own:``       it only ever reads or changes the caller's own record;
- ``in-body:``   the handler checks a permission itself (e.g. `authorize_record_access`:
                 your own record, or someone else's with ``learning_record:*``) — name it;
- ``catalogue:`` a read of shared, non-sensitive reference data. Never a write.

If this fails for a route you added: give it `require_permission(...)`. Only if a signed-in
Student genuinely must reach it, add it here with an honest reason.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi.routing import APIRoute, iter_route_contexts

API = Path(__file__).resolve().parents[2] / "control-plane/api"
sys.path.insert(0, str(API))

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Dependency callables (by qualified name) that check WHAT the caller may do, not only who
# they are. The closures returned by the rbac/auth factories, plus named section checks.
PERMISSION_CHECKS = (
    "require_permission.<locals>.",
    "require_any_permission.<locals>.",
    "require_platform_admin.<locals>.",
    "require_role.<locals>.",
    # app/routers/adaptive_learning.py: authorize_record_access with learning_record:*.
    "_readable_record",
    "_writable_record",
)

REASON_PREFIXES = ("own:", "in-body:", "catalogue:")

SIGN_IN_ONLY: dict[str, str] = {
    # ── The caller's own account and inbox ──────────────────────────────
    "GET /users/me": "own: the caller's profile",
    "GET /notifications": "own: the caller's inbox (filtered on user_id)",
    "GET /notifications/unread-count": "own: the caller's inbox",
    "POST /notifications/read-all": "own: marks the caller's own notifications read",
    "POST /notifications/{notification_id}/read": "own: 404 unless the notification is the caller's",
    "GET /onboarding/state": "own: the caller's onboarding state",
    "POST /onboarding/profile": "own: the caller's onboarding profile",
    "POST /onboarding/select-path": "own: the caller's chosen developmental path",
    "POST /onboarding/skip": "own: the caller's onboarding state",
    "POST /onboarding/complete": "own: the caller's onboarding state",
    "GET /schedule/mine": "own: the caller's own bookings (ADR 0004)",
    "GET /schedule/feed-token": "own: the caller's calendar-feed token",
    "POST /schedule/feed-token": "own: rotates the caller's calendar-feed token",
    "DELETE /schedule/feed-token": "own: revokes the caller's calendar-feed token",
    # ── Learning records: own, or someone else's with learning_record:* ──
    "GET /users/{user_id}/transcript": "in-body: authorize_record_access(learning_record:read)",
    "GET /courses/{course_id}/progress/{user_id}": "in-body: authorize_record_access(learning_record:read)",
    "POST /courses/{course_id}/complete/{user_id}": (
        "in-body: authorize_record_access(learning_record:write); grade computed from recorded progress"
    ),
    "POST /courses/{course_id}/enroll": (
        "in-body: authorize_record_access(user:update) to enrol anyone but yourself; drafts need course:author"
    ),
    "GET /competency/users/{user_id}/profile": "in-body: authorize_record_access(learning_record:read)",
    "GET /competency/users/{user_id}/skill-gaps": "in-body: authorize_record_access(learning_record:read)",
    "POST /competency/users/{user_id}/assertions": "in-body: user_has_permission(learning_record:write)",
    "GET /certifications/users/{user_id}": "in-body: authorize_record_access(learning_record:read)",
    "POST /certifications/users/{user_id}": (
        "in-body: authorize_record_access(learning_record:write); a Student self-reports their own"
    ),
    "GET /integrations/activities": "in-body: own activities unless the caller holds learning_record:read",
    "POST /integrations/moodle/sso": (
        "in-body: moodle_sso.mint_ticket — Students need an active enrolment; staff need learning_record:write"
    ),
    # ── A Student's own lab (app/lab_sessions): owner, or staff with learning_record:* ──
    "GET /lab-sessions": "in-body: the caller's labs; all_students needs learning_record:read",
    "POST /lab-sessions": "in-body: launches the caller's own lab; auto-enrol only with learning_record:write",
    "GET /lab-sessions/{session_id}": "in-body: _session_for(learning_record:read) — owner or staff",
    "POST /lab-sessions/{session_id}/heartbeat": "in-body: _session_for(learning_record:write) — owner or staff",
    "POST /lab-sessions/{session_id}/reset": "in-body: _session_for(learning_record:write) — owner or staff",
    "POST /lab-sessions/{session_id}/end": "in-body: _session_for(learning_record:write) — owner or staff",
    "POST /lab-sessions/{session_id}/console": "in-body: _session_for(learning_record:write) — owner or staff",
    "POST /lab-sessions/{session_id}/evidence": (
        "in-body: _session_for(learning_record:write); Students may only add kind 'submission'"
    ),
    # ── The Student quiz flow ───────────────────────────────────────────
    "GET /quizzes": "in-body: published quizzes only, unless the caller holds course:author",
    "GET /quizzes/{quiz_id}": "in-body: drafts are 404 without course:author; no answer key in this view",
    "POST /quizzes/{quiz_id}/attempts": "own: starts the caller's attempt on a published quiz",
    "POST /quizzes/attempts/{attempt_id}/submit": "own: 404 unless the attempt is the caller's; graded server-side",
    # ── Telemetry: Students hunt in their range's telemetry ─────────────
    "GET /telemetry/{range_id}/search": (
        "in-body: get_owned(Range) — tenant-scoped; Students search telemetry to write detections (ADR 0005)"
    ),
    # ── Catalogues every role browses ───────────────────────────────────
    # Courses and learning paths: own tenant + global; drafts only with course:author
    # (courses._catalogue_scope / _visible; tests/api/test_course_catalogue_visibility.py).
    "GET /courses": "catalogue: the course catalogue Students browse",
    "GET /courses/{course_id}": "catalogue: a course and its module list",
    "GET /courses/{course_id}/outline": "catalogue: what a course teaches",
    "GET /learning-paths": "catalogue: learning paths Students browse",
    "GET /learning-paths/{lp_id}": "catalogue: one learning path",
    "GET /competency/frameworks": "catalogue: NICE / ATT&CK competency definitions",
    "GET /competency/heatmap": "catalogue: tenant-level averages only; 'individual' is the caller's own",
    "GET /qsp/qualifications": "catalogue: the QSP qualification list",
    "GET /qsp/qualifications/{qsp_code}/objectives": "catalogue: a qualification's performance objectives",
    "GET /qsp/developmental-progression": "catalogue: the developmental progression",
    "GET /qsp/curriculum-map": "catalogue: the career map, plus the caller's own position on it",
    "GET /qsp/po-coverage": "catalogue: which POs have a module delivering them",
    "GET /golden-images": "catalogue: base OS images a range can be built from",
    "GET /golden-images/resolve": "catalogue: resolves an OS alias to a golden image",
    "GET /golden-images/alias-map": "catalogue: OS alias table",
    "GET /injectors": "catalogue: the scenario engine's injector registry (code metadata, no tenant data)",
    "GET /software-catalogue": "catalogue: deploy-time software install specs from content/catalogue (no tenant data)",
}

# Sign-in-only today, fixed on another branch. Not checked for staleness, so this file
# keeps passing on both sides of that merge; delete the entry once it has landed.
PENDING_ELSEWHERE: dict[str, str] = {}


def _qualnames(route) -> set[str]:
    names: set[str] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            names.add(getattr(dep.call, "__qualname__", getattr(dep.call, "__name__", "")))
        stack.extend(dep.dependencies)
    return names


def _sign_in_only(routes) -> set[str]:
    """`METHOD path` for every route that resolves a user but checks no permission."""
    found: set[str] = set()
    for ctx in iter_route_contexts(routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        names = _qualnames(ctx)
        if "get_current_user" not in names:
            continue  # public, or authenticated some other way (test_auth_coverage_guard)
        if any(check in name for name in names for check in PERMISSION_CHECKS):
            continue
        for method in sorted(ctx.original_route.methods - {"HEAD", "OPTIONS"}):
            found.add(f"{method} {ctx.path}")
    return found


def _app():
    os.environ.setdefault("AUTH_DISABLED", "true")
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    from app.main import app

    return app


def test_every_sign_in_only_route_is_deliberate():
    found = _sign_in_only(_app().routes)
    assert len(found) > 10, f"found only {len(found)} sign-in-only routes; the walk is broken"
    novel = sorted(found - SIGN_IN_ONLY.keys() - PENDING_ELSEWHERE.keys())
    assert not novel, (
        "Routes any signed-in account (a Student included) may call, with no permission "
        "check:\n  "
        + "\n  ".join(novel)
        + "\n\nAdd require_permission(...). If every signed-in user genuinely needs it, add "
        "it to SIGN_IN_ONLY with a reason."
    )


def test_allowlist_is_current():
    """An entry that gained a permission check, or whose route is gone, must be removed."""
    found = _sign_in_only(_app().routes)
    stale = sorted(SIGN_IN_ONLY.keys() - found)
    assert not stale, "SIGN_IN_ONLY lists routes that are no longer sign-in-only:\n  " + "\n  ".join(stale)


def test_allowlist_reasons_are_honest():
    bad = {k: v for k, v in SIGN_IN_ONLY.items() if not v.startswith(REASON_PREFIXES)}
    assert not bad, f"each reason must start with one of {REASON_PREFIXES}: {bad}"
    catalogue_writes = sorted(
        k for k, v in SIGN_IN_ONLY.items() if v.startswith("catalogue:") and k.split(" ", 1)[0] in WRITE_METHODS
    )
    assert not catalogue_writes, f"a write is never a catalogue read: {catalogue_writes}"


def test_the_guard_actually_detects_something():
    """A guard that cannot fail is not a guard."""
    from app.auth import get_current_user, require_role
    from app.models import UserRole
    from app.rbac import Permission, require_permission
    from fastapi import APIRouter, Depends, FastAPI

    probe = FastAPI()
    router = APIRouter()

    @router.post("/signed-in-only")
    def signed_in_only(user=Depends(get_current_user)):
        return {}

    @router.post("/permissioned")
    def permissioned(user=Depends(require_permission(Permission.COURSE_AUTHOR))):
        return {}

    @router.post("/role-checked")
    def role_checked(user=Depends(require_role(UserRole.admin))):
        return {}

    @router.get("/public")
    def public():
        return {}

    gated = APIRouter()

    @gated.get("/via-include")
    def via_include(user=Depends(get_current_user)):
        return {}

    probe.include_router(router)
    probe.include_router(gated, dependencies=[Depends(require_permission(Permission.INFRA_READ))])
    assert _sign_in_only(probe.routes) == {"POST /signed-in-only"}
