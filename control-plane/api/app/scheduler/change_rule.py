"""Who may change a booking: one rule for the server and for the page (#200, ADR 0014 §8; #319).

``refusal`` is what ``router._changeable`` enforces on every edit, move, cancel, accept and
decline. ``flags`` is the same rule as per-booking ``can_*`` booleans on ``EventOut``, so the
Schedule and Calendar pages show Move, Accept and Decline exactly when the server allows them
and never reimplement the rule.

- A class session follows its class, not its instructor: its lead (while they hold a lead's
  role) or an admin of its tenant, or a holder of ``schedule:admin``. Nobody else, not even an
  assistant instructor it names. A closed class's sessions change for nobody until an admin
  reopens it; a cancelled class's only wind down (cancel or complete).
- A plain booking is its instructor's: theirs, or anyone's with ``schedule:write`` while it
  names no instructor; ``schedule:admin`` changes any.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from ..auth import CurrentUser
from ..models import UserRole
from ..rbac import Permission, user_has_permission
from .models import EventState, ScheduledEvent

S = EventState
# What "Move or resize" applies to (router.update_event).
MOVABLE = (S.draft, S.scheduled, S.pending_approval)
LEAD_ROLES = (UserRole.instructor, UserRole.admin)
S_FINISHED = (S.completed, S.cancelled, S.rejected)  # nothing left to cancel or complete


def _edits_class(cls, user: CurrentUser) -> bool:
    """The class's lead or an admin of its tenant (classes.access.EDITORS), from facts in hand."""
    if str(cls.tenant_id) != str(user.tenant_id):
        return False
    if user.role == UserRole.admin:
        return True
    return str(cls.lead_instructor_id) == str(user.id) and user.role in LEAD_ROLES


def refusal(evt: ScheduledEvent, user: CurrentUser, cls, *, winding_down: bool = False) -> HTTPException | None:
    """Why ``user`` may not change ``evt`` (``cls``: its class, or None for a plain booking), or None."""
    admin = user_has_permission(user, Permission.SCHEDULE_ADMIN)
    if cls is not None:
        if cls.status != "open" and not (winding_down and cls.status == "cancelled"):
            return HTTPException(409, f"The class is {cls.status}; its sessions do not change")
        if not (_edits_class(cls, user) or admin):
            return HTTPException(403, "Only the class's lead instructor or an admin changes its sessions")
        return None
    me = uuid.UUID(str(user.id))
    if evt.instructor_id is not None and evt.instructor_id != me and not admin:
        return HTTPException(403, "This is another instructor's session; only they or an administrator can change it")
    return None


def flags(evt: ScheduledEvent, user: CurrentUser | None, cls, approval) -> dict:
    """``can_change``, ``can_move``, ``can_accept``, ``can_decline`` for ``user`` (all False
    without one, or without ``schedule:write``)."""
    none = {"can_change": False, "can_move": False, "can_accept": False, "can_decline": False,
            "can_wind_down": False}
    if user is None or not user_has_permission(user, Permission.SCHEDULE_WRITE):
        return none
    # Cancel and Complete have their own, wider rule: a cancelled class leaves a session under
    # way to its lead (``_changeable(winding_down=True)``), though nothing else about it changes.
    wind = refusal(evt, user, cls, winding_down=True) is None and evt.state not in S_FINISHED
    if refusal(evt, user, cls) is not None:
        return {**none, "can_wind_down": wind}
    proposal = (
        evt.state == S.pending_approval and approval is not None and approval.proposed_start is not None
    )
    return {"can_change": True, "can_move": evt.state in MOVABLE, "can_accept": proposal, "can_decline": proposal,
            "can_wind_down": wind}
