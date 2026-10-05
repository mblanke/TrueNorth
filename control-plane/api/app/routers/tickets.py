"""TrueNorth Range — Trouble tickets router.

Jira-style tickets: students report range problems, staff track internal work, and
a kanban board shows it all by status.

Permissions required per endpoint:

==================================================  ==========================
Endpoint                                            Permission(s)
==================================================  ==========================
GET    /tickets                                     TICKET_CREATE
POST   /tickets                                     TICKET_CREATE
GET    /tickets/board                               TICKET_WORK
GET    /tickets/assignees                           TICKET_WORK
GET    /tickets/queues                              TICKET_CREATE
POST   /tickets/queues                              TICKET_ADMIN
PUT    /tickets/queues/{queue_id}                   TICKET_ADMIN
DELETE /tickets/queues/{queue_id}                   TICKET_ADMIN
GET    /tickets/{ticket_id}                         TICKET_CREATE (+ own ticket)
PATCH  /tickets/{ticket_id}                         TICKET_CREATE (+ own ticket)
DELETE /tickets/{ticket_id}                         TICKET_ADMIN
POST   /tickets/{ticket_id}/move                    TICKET_WORK
GET    /tickets/{ticket_id}/comments                TICKET_CREATE (+ own ticket)
POST   /tickets/{ticket_id}/comments                TICKET_CREATE (+ own ticket)
GET    /tickets/{ticket_id}/attachments             TICKET_CREATE (+ own ticket)
POST   /tickets/{ticket_id}/attachments             TICKET_CREATE (+ own ticket)
GET    /tickets/attachments/{attachment_id}         TICKET_CREATE (+ own ticket)
DELETE /tickets/attachments/{attachment_id}         uploader or TICKET_WORK
GET    /tickets/{ticket_id}/activity                TICKET_CREATE (+ own ticket)
==================================================  ==========================

Without TICKET_WORK a caller sees only tickets they reported — anyone else's is a
404, never a 403 — and never sees internal comments. A reporter may edit the subject
and description, close a resolved ticket, or reopen one; everything else is triage.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import object_store
from ..auth import CurrentUser
from ..db import get_db
from ..models import AuditLog, Exercise, Range, User, UserRole
from ..models_tickets import SupportQueue, Ticket, TicketActivity, TicketAttachment, TicketComment
from ..rbac import Permission, require_permission, user_has_permission
from ..tenancy import get_owned, tenant_uuid

logger = logging.getLogger("truenorth.api.tickets")

router = APIRouter(prefix="/tickets", tags=["tickets"])

TICKET_BUCKET = "tickets"
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
TYPE_RE = r"^(incident|bug|task|request)$"
STATUS_RE = r"^(open|in_progress|waiting|resolved|closed)$"
PRIORITY_RE = r"^(low|medium|high|critical)$"
STAFF_ROLES = (UserRole.admin, UserRole.instructor, UserRole.range_ops)
TRACKED_FIELDS = ("status", "priority", "type", "assignee_id", "queue_id", "subject")


# ── Schemas ─────────────────────────────────────────────────────────────
class QueueIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = ""
    is_default: bool = False


class QueueUpdate(BaseModel):
    # Omit a field to keep it; an explicit null is refused (422), never written to a NOT NULL column.
    name: str = Field(default=None, min_length=1, max_length=255)
    description: str = Field(default=None)
    is_default: bool = Field(default=None)


class QueueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    description: str
    is_default: bool


class TicketIn(BaseModel):
    subject: str = Field(..., min_length=1, max_length=500)
    description: str = Field("", max_length=50_000)
    type: str = Field("incident", pattern=TYPE_RE)
    priority: str = Field("medium", pattern=PRIORITY_RE)
    category: str = Field("other", max_length=50)
    labels: str = Field("", max_length=500)
    queue_id: uuid.UUID | None = None
    range_id: uuid.UUID | None = None
    exercise_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None  # staff only


class TicketUpdate(BaseModel):
    subject: str | None = Field(None, min_length=1, max_length=500)
    description: str | None = Field(None, max_length=50_000)
    type: str | None = Field(None, pattern=TYPE_RE)
    status: str | None = Field(None, pattern=STATUS_RE)
    priority: str | None = Field(None, pattern=PRIORITY_RE)
    category: str | None = Field(None, max_length=50)
    labels: str | None = Field(None, max_length=500)
    queue_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    unassign: bool = False
    range_id: uuid.UUID | None = None
    exercise_id: uuid.UUID | None = None


class MoveIn(BaseModel):
    status: str = Field(..., pattern=STATUS_RE)
    board_order: float = 0.0


class TicketListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    number: int
    key: str = ""
    type: str
    subject: str
    status: str
    priority: str
    category: str
    labels: str
    queue_id: uuid.UUID | None = None
    queue_name: str = ""
    reporter_id: uuid.UUID
    reporter_name: str = ""
    assignee_id: uuid.UUID | None = None
    assignee_name: str = ""
    range_id: uuid.UUID | None = None
    exercise_id: uuid.UUID | None = None
    board_order: float
    created_at: datetime
    updated_at: datetime


class TicketOut(TicketListOut):
    description: str
    range_name: str = ""
    exercise_name: str = ""
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    can_work: bool = False


class CommentIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=50_000)
    is_internal: bool = False


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    author_id: uuid.UUID
    author_name: str = ""
    body: str
    is_internal: bool
    created_at: datetime


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    uploaded_by: uuid.UUID
    created_at: datetime


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    actor_id: uuid.UUID
    actor_name: str = ""
    field: str
    old_value: str
    new_value: str
    created_at: datetime


class AssigneeOut(BaseModel):
    id: uuid.UUID
    display_name: str
    role: str


class BoardColumn(BaseModel):
    status: str
    tickets: list[TicketListOut]


# ── Helpers ─────────────────────────────────────────────────────────────
def _audit(db: Session, user: CurrentUser, action: str, rid: str) -> None:
    db.add(AuditLog(user_id=uuid.UUID(user.id), action=action, resource_type="ticket", resource_id=rid))


def _staff(user: CurrentUser) -> bool:
    return user_has_permission(user, Permission.TICKET_WORK)


def _now() -> datetime:
    return datetime.now(UTC)


def _names(db: Session, user: CurrentUser, ids: set) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = db.query(User.id, User.display_name).filter(User.id.in_(ids), User.tenant_id == tenant_uuid(user)).all()
    names = {r.id: r.display_name for r in rows}
    # The caller may have no users row (AUTH_DISABLED dev identity); name them anyway.
    names.setdefault(uuid.UUID(user.id), user.display_name)
    return names


def _ticket(db: Session, ticket_id: uuid.UUID, user: CurrentUser) -> Ticket:
    """A ticket in the caller's tenant that the caller may see, or 404."""
    t = get_owned(db, Ticket, ticket_id, user, not_found="Ticket not found")
    if not _staff(user) and str(t.reporter_id) != user.id:
        raise HTTPException(404, "Ticket not found")
    return t


def _default_queue(db: Session, user: CurrentUser) -> SupportQueue:
    tid = tenant_uuid(user)
    q = (
        db.query(SupportQueue)
        .filter(SupportQueue.tenant_id == tid, SupportQueue.deleted_at.is_(None))
        .order_by(SupportQueue.is_default.desc(), SupportQueue.created_at)
        .first()
    )
    if q:
        return q
    q = SupportQueue(tenant_id=tid, name="Support", slug="support", description="General support", is_default=True)
    db.add(q)
    db.flush()
    return q


def _check_assignee(db: Session, user: CurrentUser, assignee_id: uuid.UUID) -> None:
    if str(assignee_id) == user.id:
        return
    found = (
        db.query(User.id)
        .filter(
            User.id == assignee_id,
            User.tenant_id == tenant_uuid(user),
            User.role.in_(STAFF_ROLES),
            User.deleted_at.is_(None),
        )
        .first()
    )
    if not found:
        raise HTTPException(422, "Assignee must be a staff member in your organisation")


def _decorate(db: Session, user: CurrentUser, tickets: list[Ticket], cls=TicketListOut) -> list:
    names = _names(db, user, {t.reporter_id for t in tickets} | {t.assignee_id for t in tickets})
    qids = {t.queue_id for t in tickets if t.queue_id}
    queues = {}
    if qids:
        rows = (
            db.query(SupportQueue.id, SupportQueue.name)
            .filter(SupportQueue.id.in_(qids), SupportQueue.tenant_id == tenant_uuid(user))
            .all()
        )
        queues = {r.id: r.name for r in rows}
    out = []
    for t in tickets:
        item = cls.model_validate(t)
        item.key = f"TN-{t.number}"
        item.reporter_name = names.get(t.reporter_id, "")
        item.assignee_name = names.get(t.assignee_id, "") if t.assignee_id else ""
        item.queue_name = queues.get(t.queue_id, "") if t.queue_id else ""
        out.append(item)
    return out


def _detail(db: Session, user: CurrentUser, t: Ticket) -> TicketOut:
    out: TicketOut = _decorate(db, user, [t], TicketOut)[0]
    if t.range_id:
        rng = db.query(Range.name).filter(Range.id == t.range_id, Range.tenant_id == t.tenant_id).first()
        out.range_name = rng.name if rng else ""
    if t.exercise_id:
        ex = db.query(Exercise.name).filter(Exercise.id == t.exercise_id, Exercise.tenant_id == t.tenant_id).first()
        out.exercise_name = ex.name if ex else ""
    out.can_work = _staff(user)
    return out


def _log(db: Session, t: Ticket, user: CurrentUser, field: str, old, new) -> None:
    db.add(
        TicketActivity(
            tenant_id=t.tenant_id,
            ticket_id=t.id,
            actor_id=uuid.UUID(user.id),
            field=field,
            old_value="" if old is None else str(old)[:500],
            new_value="" if new is None else str(new)[:500],
        )
    )


def _set_status(t: Ticket, status: str) -> None:
    t.status = status
    if status == "resolved":
        t.resolved_at = t.resolved_at or _now()
    elif status == "closed":
        t.closed_at = _now()
        t.resolved_at = t.resolved_at or t.closed_at
    else:
        t.resolved_at = None
        t.closed_at = None


def _apply(db: Session, t: Ticket, user: CurrentUser, changes: dict) -> None:
    """Apply field changes, writing one activity row per tracked field that moved."""
    for field, new in changes.items():
        old = getattr(t, field)
        if old == new:
            continue
        if field in TRACKED_FIELDS:
            _log(db, t, user, field, old, new)
        if field == "status":
            _set_status(t, new)
        else:
            setattr(t, field, new)


def _allocate_number(db: Session, tid: uuid.UUID) -> int:
    current = db.query(func.max(Ticket.number)).filter(Ticket.tenant_id == tid).scalar()
    return (current or 0) + 1


# ── Fixed paths first (they would otherwise match /{ticket_id}) ──────────
@router.get("/queues", response_model=list[QueueOut])
def list_queues(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> list[SupportQueue]:
    _default_queue(db, user)
    db.commit()
    return (
        db.query(SupportQueue)
        .filter(SupportQueue.tenant_id == tenant_uuid(user), SupportQueue.deleted_at.is_(None))
        .order_by(SupportQueue.name)
        .all()
    )


@router.post("/queues", response_model=QueueOut, status_code=201)
def create_queue(
    body: QueueIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_ADMIN)),
) -> SupportQueue:
    tid = tenant_uuid(user)
    if db.query(SupportQueue.id).filter(SupportQueue.tenant_id == tid, SupportQueue.slug == body.slug).first():
        raise HTTPException(409, f"A queue with slug '{body.slug}' already exists")
    if body.is_default:
        db.query(SupportQueue).filter(SupportQueue.tenant_id == tid).update({"is_default": False})
    q = SupportQueue(tenant_id=tid, **body.model_dump())
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


@router.put("/queues/{queue_id}", response_model=QueueOut)
def update_queue(
    body: QueueUpdate,
    queue_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_ADMIN)),
) -> SupportQueue:
    q = get_owned(db, SupportQueue, queue_id, user, not_found="Queue not found")
    data = body.model_dump(exclude_unset=True)
    if data.get("is_default"):
        db.query(SupportQueue).filter(SupportQueue.tenant_id == q.tenant_id).update({"is_default": False})
    for k, v in data.items():
        if v is not None:
            setattr(q, k, v)
    db.commit()
    db.refresh(q)
    return q


@router.delete("/queues/{queue_id}", status_code=204, response_class=Response)
def delete_queue(
    queue_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_ADMIN)),
):
    q = get_owned(db, SupportQueue, queue_id, user, not_found="Queue not found")
    in_use = (
        db.query(Ticket.id)
        .filter(Ticket.tenant_id == q.tenant_id, Ticket.queue_id == q.id, Ticket.deleted_at.is_(None))
        .first()
    )
    if in_use:
        raise HTTPException(409, "Move this queue's tickets elsewhere before deleting it")
    q.soft_delete()
    db.commit()


@router.get("/assignees", response_model=list[AssigneeOut])
def list_assignees(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_WORK)),
) -> list[AssigneeOut]:
    rows = (
        db.query(User)
        .filter(User.tenant_id == tenant_uuid(user), User.role.in_(STAFF_ROLES), User.deleted_at.is_(None))
        .order_by(User.display_name)
        .all()
    )
    out = [AssigneeOut(id=u.id, display_name=u.display_name, role=str(u.role.value)) for u in rows]
    if not any(str(a.id) == user.id for a in out):
        out.insert(0, AssigneeOut(id=uuid.UUID(user.id), display_name=user.display_name, role=str(user.role.value)))
    return out


@router.get("/board", response_model=list[BoardColumn])
def board(
    queue_id: uuid.UUID | None = Query(None),
    assignee: str | None = Query(None, description="'me' or a user id"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_WORK)),
) -> list[BoardColumn]:
    q = db.query(Ticket).filter(Ticket.tenant_id == tenant_uuid(user), Ticket.deleted_at.is_(None))
    if queue_id:
        q = q.filter(Ticket.queue_id == queue_id)
    if assignee:
        try:
            who = uuid.UUID(user.id) if assignee == "me" else uuid.UUID(assignee)
        except ValueError as exc:
            raise HTTPException(422, "assignee must be 'me' or a user id") from exc
        q = q.filter(Ticket.assignee_id == who)
    tickets = q.order_by(Ticket.board_order, Ticket.created_at.desc()).all()
    decorated = _decorate(db, user, tickets)
    columns = []
    for status in ("open", "in_progress", "waiting", "resolved", "closed"):
        cards = [d for d in decorated if d.status == status]
        if status == "closed":
            cards = sorted(cards, key=lambda c: c.updated_at, reverse=True)[:50]
        columns.append(BoardColumn(status=status, tickets=cards))
    return columns


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> Response:
    att = get_owned(db, TicketAttachment, attachment_id, user, not_found="Attachment not found")
    _ticket(db, att.ticket_id, user)
    try:
        data = object_store.get_object(att.object_key, bucket=TICKET_BUCKET)
    except Exception as exc:  # noqa: BLE001 — object store faults are a 502, not a crash
        logger.error("Could not read %s from object storage: %s", att.object_key, exc)
        raise HTTPException(502, "Attachment storage is unavailable") from exc
    safe_name = att.filename.replace('"', "").replace("\r", "").replace("\n", "")
    return Response(
        content=data,
        media_type="application/octet-stream",  # never rendered inline by the browser
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/attachments/{attachment_id}", status_code=204, response_class=Response)
def delete_attachment(
    attachment_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
):
    att = get_owned(db, TicketAttachment, attachment_id, user, not_found="Attachment not found")
    t = _ticket(db, att.ticket_id, user)
    if not _staff(user) and str(att.uploaded_by) != user.id:
        raise HTTPException(403, "Only the uploader or support staff can remove an attachment")
    with contextlib.suppress(Exception):
        object_store.delete_object(att.object_key, bucket=TICKET_BUCKET)
    _log(db, t, user, "attachment", att.filename, "")
    db.delete(att)
    db.commit()


# ── Tickets ─────────────────────────────────────────────────────────────
@router.get("", response_model=list[TicketListOut])
def list_tickets(
    scope: str = Query("all", pattern=r"^(mine|assigned|all)$"),
    status: str | None = Query(None, description="comma-separated statuses"),
    priority: str | None = Query(None, pattern=PRIORITY_RE),
    type: str | None = Query(None, pattern=TYPE_RE),
    queue_id: uuid.UUID | None = Query(None),
    range_id: uuid.UUID | None = Query(None),
    q: str | None = Query(None, max_length=200),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> list[TicketListOut]:
    uid = uuid.UUID(user.id)
    query = db.query(Ticket).filter(Ticket.tenant_id == tenant_uuid(user), Ticket.deleted_at.is_(None))
    if not _staff(user) or scope == "mine":
        query = query.filter(Ticket.reporter_id == uid)
    elif scope == "assigned":
        query = query.filter(Ticket.assignee_id == uid)
    if status:
        query = query.filter(Ticket.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    if priority:
        query = query.filter(Ticket.priority == priority)
    if type:
        query = query.filter(Ticket.type == type)
    if queue_id:
        query = query.filter(Ticket.queue_id == queue_id)
    if range_id:
        query = query.filter(Ticket.range_id == range_id)
    if q:
        term = q.strip()
        like = f"%{term}%"
        conds = [Ticket.subject.ilike(like), Ticket.description.ilike(like), Ticket.labels.ilike(like)]
        digits = term.upper().removeprefix("TN-")
        if digits.isdigit():
            conds.append(Ticket.number == int(digits))
        query = query.filter(or_(*conds))
    tickets = query.order_by(Ticket.updated_at.desc()).limit(limit).all()
    return _decorate(db, user, tickets)


@router.post("", response_model=TicketOut, status_code=201)
def create_ticket(
    body: TicketIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> TicketOut:
    tid = tenant_uuid(user)
    data = body.model_dump()
    if data["assignee_id"] is not None:
        if not _staff(user):
            raise HTTPException(403, "Only support staff can assign tickets")
        _check_assignee(db, user, data["assignee_id"])
    if data["range_id"]:
        get_owned(db, Range, data["range_id"], user, not_found="Range not found")
    if data["exercise_id"]:
        get_owned(db, Exercise, data["exercise_id"], user, not_found="Exercise not found")
    if data["queue_id"]:
        get_owned(db, SupportQueue, data["queue_id"], user, not_found="Queue not found")
    else:
        data["queue_id"] = _default_queue(db, user).id

    # Per-tenant numbering: two concurrent filings can pick the same next number;
    # the unique (tenant_id, number) constraint rejects one and it simply retries.
    for _attempt in range(5):
        t = Ticket(tenant_id=tid, reporter_id=uuid.UUID(user.id), number=_allocate_number(db, tid), **data)
        try:
            with db.begin_nested():
                db.add(t)
                db.flush()
            break
        except IntegrityError:
            continue
    else:
        raise HTTPException(503, "Could not allocate a ticket number, please retry")

    _log(db, t, user, "created", "", f"TN-{t.number}")
    _audit(db, user, "ticket_create", str(t.id))
    db.commit()
    db.refresh(t)
    return _detail(db, user, t)


@router.get("/{ticket_id}", response_model=TicketOut)
def get_ticket(
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> TicketOut:
    return _detail(db, user, _ticket(db, ticket_id, user))


@router.patch("/{ticket_id}", response_model=TicketOut)
def update_ticket(
    body: TicketUpdate,
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> TicketOut:
    t = _ticket(db, ticket_id, user)
    data = body.model_dump(exclude_unset=True)
    unassign = data.pop("unassign", False)
    data = {k: v for k, v in data.items() if v is not None}

    if not _staff(user):
        allowed = {"subject", "description"}
        status = data.pop("status", None)
        if set(data) - allowed:
            raise HTTPException(403, "Only support staff can change those fields")
        if unassign:
            raise HTTPException(403, "Only support staff can change the assignee")
        if status is not None:
            reopen = status == "open" and t.status in ("resolved", "closed")
            close = status == "closed" and t.status == "resolved"
            if not (reopen or close):
                raise HTTPException(403, "You can close a resolved ticket or reopen it; staff handle the rest")
            data["status"] = status
    else:
        if "assignee_id" in data:
            _check_assignee(db, user, data["assignee_id"])
        if unassign:
            data["assignee_id"] = None
        if "queue_id" in data:
            get_owned(db, SupportQueue, data["queue_id"], user, not_found="Queue not found")
        if "range_id" in data:
            get_owned(db, Range, data["range_id"], user, not_found="Range not found")
        if "exercise_id" in data:
            get_owned(db, Exercise, data["exercise_id"], user, not_found="Exercise not found")

    _apply(db, t, user, data)
    _audit(db, user, "ticket_update", str(t.id))
    db.commit()
    db.refresh(t)
    return _detail(db, user, t)


@router.delete("/{ticket_id}", status_code=204, response_class=Response)
def delete_ticket(
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_ADMIN)),
):
    t = _ticket(db, ticket_id, user)
    t.soft_delete()
    _audit(db, user, "ticket_delete", str(t.id))
    db.commit()


@router.post("/{ticket_id}/move", response_model=TicketListOut)
def move_ticket(
    body: MoveIn,
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_WORK)),
) -> TicketListOut:
    """Drag-and-drop on the board: change column and/or position within it."""
    t = _ticket(db, ticket_id, user)
    _apply(db, t, user, {"status": body.status, "board_order": body.board_order})
    db.commit()
    db.refresh(t)
    return _decorate(db, user, [t])[0]


# ── Comments ────────────────────────────────────────────────────────────
@router.get("/{ticket_id}/comments", response_model=list[CommentOut])
def list_comments(
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> list[CommentOut]:
    t = _ticket(db, ticket_id, user)
    q = db.query(TicketComment).filter(TicketComment.ticket_id == t.id, TicketComment.tenant_id == t.tenant_id)
    if not _staff(user):
        q = q.filter(TicketComment.is_internal.is_(False))
    rows = q.order_by(TicketComment.created_at).all()
    names = _names(db, user, {c.author_id for c in rows})
    out = []
    for c in rows:
        item = CommentOut.model_validate(c)
        item.author_name = names.get(c.author_id, "")
        out.append(item)
    return out


@router.post("/{ticket_id}/comments", response_model=CommentOut, status_code=201)
def add_comment(
    body: CommentIn,
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> CommentOut:
    t = _ticket(db, ticket_id, user)
    if body.is_internal and not _staff(user):
        raise HTTPException(403, "Only support staff can write internal notes")
    c = TicketComment(
        tenant_id=t.tenant_id,
        ticket_id=t.id,
        author_id=uuid.UUID(user.id),
        body=body.body,
        is_internal=body.is_internal,
    )
    db.add(c)
    # The reporter answering a "waiting on you" ticket puts it back in the queue.
    if t.status == "waiting" and str(t.reporter_id) == user.id and not body.is_internal:
        _apply(db, t, user, {"status": "open"})
    t.updated_at = _now()
    db.commit()
    db.refresh(c)
    out = CommentOut.model_validate(c)
    out.author_name = user.display_name
    return out


# ── Attachments ─────────────────────────────────────────────────────────
@router.get("/{ticket_id}/attachments", response_model=list[AttachmentOut])
def list_attachments(
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> list[TicketAttachment]:
    t = _ticket(db, ticket_id, user)
    return (
        db.query(TicketAttachment)
        .filter(TicketAttachment.ticket_id == t.id, TicketAttachment.tenant_id == t.tenant_id)
        .order_by(TicketAttachment.created_at)
        .all()
    )


@router.post("/{ticket_id}/attachments", response_model=list[AttachmentOut], status_code=201)
async def upload_attachments(
    files: list[UploadFile],
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> list[TicketAttachment]:
    t = _ticket(db, ticket_id, user)
    created: list[TicketAttachment] = []
    for file in files:
        filename = (file.filename or "upload").replace("/", "_").replace("\\", "_")[:500]
        data = await file.read()
        if not data:
            raise HTTPException(422, f"{filename} is empty")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, f"{filename} exceeds the 25 MB limit")
        att = TicketAttachment(
            tenant_id=t.tenant_id,
            ticket_id=t.id,
            filename=filename,
            content_type=(file.content_type or "application/octet-stream")[:120],
            size_bytes=len(data),
            uploaded_by=uuid.UUID(user.id),
        )
        db.add(att)
        db.flush()
        att.object_key = f"{t.tenant_id}/{t.id}/{att.id}"
        object_store.put_object(att.object_key, data, att.content_type, bucket=TICKET_BUCKET)
        _log(db, t, user, "attachment", "", filename)
        created.append(att)
    db.commit()
    for att in created:
        db.refresh(att)
    return created


# ── History ─────────────────────────────────────────────────────────────
@router.get("/{ticket_id}/activity", response_model=list[ActivityOut])
def list_activity(
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> list[ActivityOut]:
    t = _ticket(db, ticket_id, user)
    rows = (
        db.query(TicketActivity)
        .filter(TicketActivity.ticket_id == t.id, TicketActivity.tenant_id == t.tenant_id)
        .order_by(TicketActivity.created_at)
        .all()
    )
    names = _names(db, user, {a.actor_id for a in rows})
    out = []
    for a in rows:
        item = ActivityOut.model_validate(a)
        item.actor_name = names.get(a.actor_id, "")
        out.append(item)
    return out
