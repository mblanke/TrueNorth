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
import re
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Path, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import object_store
from ..auth import CurrentUser
from ..db import get_db
from ..models import AuditLog, Exercise, Range, User, UserRole
from ..models_tickets import SupportQueue, Ticket, TicketActivity, TicketAttachment, TicketComment
from ..notifications.inbox import existing_user_ids, notify, staff_ids
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
TRACKED_FIELDS = (
    "status",
    "priority",
    "type",
    "assignee_id",
    "queue_id",
    "subject",
    "description",
    "range_id",
    "exercise_id",
)
MAX_FILES_PER_UPLOAD = 10


# ── Schemas ─────────────────────────────────────────────────────────────
def _not_blank(v: str | None) -> str | None:
    """Subjects and names are trimmed; one made only of spaces is refused."""
    if v is None:
        return v
    v = v.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


class SupportQueueIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = ""
    is_default: bool = False

    _name = field_validator("name")(_not_blank)


class SupportQueueUpdate(BaseModel):
    # Omit a field to keep it; an explicit null is refused (422), never written to a NOT NULL column.
    name: str = Field(default=None, min_length=1, max_length=255)
    description: str = Field(default=None)
    is_default: bool = Field(default=None)

    _name = field_validator("name")(_not_blank)


class SupportQueueOut(BaseModel):
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

    _subject = field_validator("subject")(_not_blank)


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
    unlink_range: bool = False
    unlink_exercise: bool = False

    _subject = field_validator("subject")(_not_blank)


class TicketMoveIn(BaseModel):
    status: str = Field(..., pattern=STATUS_RE)
    # NaN would be stored and then break JSON encoding of the whole board.
    board_order: float = Field(0.0, allow_inf_nan=False)


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


class TicketCommentIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=50_000)
    is_internal: bool = False


class TicketCommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    author_id: uuid.UUID
    author_name: str = ""
    body: str
    is_internal: bool
    created_at: datetime


class TicketAttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    uploaded_by: uuid.UUID
    created_at: datetime


class TicketActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    actor_id: uuid.UUID
    actor_name: str = ""
    field: str
    old_value: str
    new_value: str
    created_at: datetime


class TicketAssigneeOut(BaseModel):
    id: uuid.UUID
    display_name: str
    role: str


class TicketBoardColumn(BaseModel):
    status: str
    tickets: list[TicketListOut]


# ── Helpers ─────────────────────────────────────────────────────────────
def _audit(db: Session, user: CurrentUser, action: str, rid: str, rtype: str = "ticket") -> None:
    db.add(AuditLog(user_id=uuid.UUID(user.id), action=action, resource_type=rtype, resource_id=rid))


def _like(term: str) -> str:
    """A LIKE pattern matching `term` literally (``%`` and ``_`` are not wildcards)."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _content_disposition(filename: str) -> str:
    """`attachment` with an ASCII fallback plus the UTF-8 name (RFC 6266 / 5987).

    Header values must be Latin-1, so a Cyrillic or CJK filename sent raw is a 500.
    """
    clean = re.sub(r"[\x00-\x1f\x7f]", "", filename)
    ascii_name = re.sub(r'[^\x20-\x7e]|["\\]', "_", clean) or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(clean, safe='')}"


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
    # The first filings in a new tenant race to create the queue; queue them (Postgres).
    _tenant_lock(db, f"queue:{tid}")
    q = (
        db.query(SupportQueue)
        .filter(SupportQueue.tenant_id == tid, SupportQueue.deleted_at.is_(None))
        .order_by(SupportQueue.is_default.desc(), SupportQueue.created_at)
        .first()
    )
    if q:
        return q
    # The slug stays taken after a soft delete (unique tenant_id+slug), so bring the old
    # row back rather than colliding with it, which would 500 every Support page.
    old = db.query(SupportQueue).filter(SupportQueue.tenant_id == tid, SupportQueue.slug == "support").first()
    if old:
        old.deleted_at = None
        old.is_default = True
        db.flush()
        return old
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


def _tell(
    db: Session, t: Ticket, user: CurrentUser, recipients, title: str, message: str = "", kind: str = "ticket"
) -> None:
    """Notify `recipients` (user ids) about ticket `t`, never the actor, only real users of the tenant."""
    notify(
        db,
        tenant_id=t.tenant_id,
        user_ids=existing_user_ids(db, t.tenant_id, recipients),
        actor_id=uuid.UUID(user.id),
        title=f"TN-{t.number}: {title}",
        message=message or t.subject,
        link=f"/support/{t.id}",
        kind=kind,
    )


def _tell_status(db: Session, t: Ticket, user: CurrentUser, old_status: str) -> None:
    """The reporter hears when someone else moves their ticket to a new status."""
    if t.status != old_status:
        _tell(db, t, user, [t.reporter_id], f"now {t.status.replace('_', ' ')}", kind="ticket_status")


def _set_status(t: Ticket, status: str) -> None:
    t.status = status
    if status == "resolved":
        t.resolved_at = t.resolved_at or _now()
        t.closed_at = None
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
        if field == "description":
            _log(db, t, user, field, "", "")  # "edited the description"; the text itself is too long to diff here
        elif field in TRACKED_FIELDS:
            _log(db, t, user, field, old, new)
        if field == "status":
            _set_status(t, new)
        else:
            setattr(t, field, new)


def _tenant_lock(db: Session, key: str) -> None:
    """Hold a Postgres advisory lock on `key` until this transaction ends.

    Ticket numbering (max+1) gave 503s when a class filed together: 6 of 10 concurrent
    filings won and 5 retries were not enough. Creating the default queue raced the same
    way (IntegrityError, a 500). With the lock, concurrent requests queue instead of
    colliding. SQLite (tests) serialises writers anyway.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


def _allocate_number(db: Session, tid: uuid.UUID) -> int:
    current = db.query(func.max(Ticket.number)).filter(Ticket.tenant_id == tid).scalar()
    return (current or 0) + 1


def _next_board_order(db: Session, tid: uuid.UUID) -> float:
    """Below every existing card, and distinct, so drag-reordering has gaps to work with."""
    current = db.query(func.max(Ticket.board_order)).filter(Ticket.tenant_id == tid).scalar()
    return float(current or 0) + 1.0


# ── Fixed paths first (they would otherwise match /{ticket_id}) ──────────
@router.get("/queues", response_model=list[SupportQueueOut])
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


@router.post("/queues", response_model=SupportQueueOut, status_code=201)
def create_queue(
    body: SupportQueueIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_ADMIN)),
) -> SupportQueue:
    tid = tenant_uuid(user)
    existing = db.query(SupportQueue).filter(SupportQueue.tenant_id == tid, SupportQueue.slug == body.slug).first()
    if existing and existing.deleted_at is None:
        raise HTTPException(409, f"A queue with slug '{body.slug}' already exists")
    if body.is_default:
        db.query(SupportQueue).filter(SupportQueue.tenant_id == tid).update({"is_default": False})
    if existing:  # a deleted queue still holds its slug; reuse the row
        q = existing
        q.deleted_at = None
        for k, v in body.model_dump().items():
            setattr(q, k, v)
    else:
        q = SupportQueue(tenant_id=tid, **body.model_dump())
        db.add(q)
    db.flush()
    _audit(db, user, "queue_create", str(q.id), "support_queue")
    db.commit()
    db.refresh(q)
    return q


@router.put("/queues/{queue_id}", response_model=SupportQueueOut)
def update_queue(
    body: SupportQueueUpdate,
    queue_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_ADMIN)),
) -> SupportQueue:
    q = get_owned(db, SupportQueue, queue_id, user, not_found="Queue not found")
    data = body.model_dump(exclude_unset=True)
    if data.get("is_default") is False and q.is_default:
        raise HTTPException(409, "Make another queue the default instead; new tickets need a default queue")
    if data.get("is_default"):
        db.query(SupportQueue).filter(SupportQueue.tenant_id == q.tenant_id).update({"is_default": False})
    for k, v in data.items():
        if v is not None:
            setattr(q, k, v)
    _audit(db, user, "queue_update", str(q.id), "support_queue")
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
    others = (
        db.query(SupportQueue)
        .filter(SupportQueue.tenant_id == q.tenant_id, SupportQueue.id != q.id, SupportQueue.deleted_at.is_(None))
        .order_by(SupportQueue.created_at)
        .all()
    )
    if not others:
        raise HTTPException(409, "Keep at least one queue: new tickets need somewhere to go")
    if q.is_default:
        others[0].is_default = True
    q.soft_delete()
    _audit(db, user, "queue_delete", str(q.id), "support_queue")
    db.commit()


@router.get("/assignees", response_model=list[TicketAssigneeOut])
def list_assignees(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_WORK)),
) -> list[TicketAssigneeOut]:
    rows = (
        db.query(User)
        .filter(User.tenant_id == tenant_uuid(user), User.role.in_(STAFF_ROLES), User.deleted_at.is_(None))
        .order_by(User.display_name)
        .all()
    )
    out = [TicketAssigneeOut(id=u.id, display_name=u.display_name, role=str(u.role.value)) for u in rows]
    if not any(str(a.id) == user.id for a in out):
        out.insert(
            0, TicketAssigneeOut(id=uuid.UUID(user.id), display_name=user.display_name, role=str(user.role.value))
        )
    return out


@router.get("/board", response_model=list[TicketBoardColumn])
def board(
    queue_id: uuid.UUID | None = Query(None),
    assignee: str | None = Query(None, description="'me' or a user id"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_WORK)),
) -> list[TicketBoardColumn]:
    q = db.query(Ticket).filter(Ticket.tenant_id == tenant_uuid(user), Ticket.deleted_at.is_(None))
    if queue_id:
        q = q.filter(Ticket.queue_id == queue_id)
    if assignee:
        try:
            who = uuid.UUID(user.id) if assignee == "me" else uuid.UUID(assignee)
        except ValueError as exc:
            raise HTTPException(422, "assignee must be 'me' or a user id") from exc
        q = q.filter(Ticket.assignee_id == who)
    active = q.filter(Ticket.status != "closed").order_by(Ticket.board_order, Ticket.created_at).all()
    closed = q.filter(Ticket.status == "closed").order_by(Ticket.updated_at.desc()).limit(50).all()
    decorated = _decorate(db, user, active + closed)
    columns = []
    for status in ("open", "in_progress", "waiting", "resolved", "closed"):
        columns.append(TicketBoardColumn(status=status, tickets=[d for d in decorated if d.status == status]))
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
    return Response(
        content=data,
        media_type="application/octet-stream",  # never rendered inline by the browser
        headers={
            "Content-Disposition": _content_disposition(att.filename),
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
    _audit(db, user, "ticket_attachment_delete", str(t.id))
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
        like = _like(term)
        conds = [
            Ticket.subject.ilike(like, escape="\\"),
            Ticket.description.ilike(like, escape="\\"),
            Ticket.labels.ilike(like, escape="\\"),
        ]
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
        ex = get_owned(db, Exercise, data["exercise_id"], user, not_found="Exercise not found")
        # Filed from an exercise: the range it runs on is the one with the problem.
        data["range_id"] = data["range_id"] or ex.range_id
    if data["queue_id"]:
        get_owned(db, SupportQueue, data["queue_id"], user, not_found="Queue not found")
    else:
        data["queue_id"] = _default_queue(db, user).id

    # Per-tenant numbering. On Postgres the advisory lock queues concurrent filers; the
    # unique (tenant_id, number) constraint plus retry is the backstop everywhere.
    _tenant_lock(db, f"tickets:{tid}")
    for _attempt in range(5):
        t = Ticket(
            tenant_id=tid,
            reporter_id=uuid.UUID(user.id),
            number=_allocate_number(db, tid),
            board_order=_next_board_order(db, tid),
            **data,
        )
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
    _tell(db, t, user, staff_ids(db, tid), "new ticket", kind="ticket_new")
    if t.assignee_id:
        _tell(db, t, user, [t.assignee_id], "assigned to you", kind="ticket_assigned")
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
    unlink = {f: data.pop(f"unlink_{f.removesuffix('_id')}", False) for f in ("range_id", "exercise_id")}
    data = {k: v for k, v in data.items() if v is not None}

    if not _staff(user):
        allowed = {"subject", "description"}
        status = data.pop("status", None)
        if set(data) - allowed:
            raise HTTPException(403, "Only support staff can change those fields")
        if unassign or any(unlink.values()):
            raise HTTPException(403, "Only support staff can change those fields")
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
        for field, drop in unlink.items():
            if drop:
                data[field] = None

    old_status, old_assignee = t.status, t.assignee_id
    _apply(db, t, user, data)
    _tell_status(db, t, user, old_status)
    if t.assignee_id and t.assignee_id != old_assignee:
        _tell(db, t, user, [t.assignee_id], "assigned to you", kind="ticket_assigned")
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
    body: TicketMoveIn,
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_WORK)),
) -> TicketListOut:
    """Drag-and-drop on the board: change column and/or position within it."""
    t = _ticket(db, ticket_id, user)
    if body.status == t.status:
        # Reordering within a column is staff housekeeping: keep updated_at as it is, so
        # the reporter's list doesn't re-sort and claim an update nobody can see.
        db.execute(
            update(Ticket)
            .where(Ticket.id == t.id, Ticket.tenant_id == t.tenant_id)
            .values(board_order=body.board_order, updated_at=Ticket.updated_at)
            .execution_options(synchronize_session=False)
        )
        db.expire(t)
    else:
        old_status = t.status
        _apply(db, t, user, {"status": body.status, "board_order": body.board_order})
        _tell_status(db, t, user, old_status)
    _audit(db, user, "ticket_move", str(t.id))
    db.commit()
    db.refresh(t)
    return _decorate(db, user, [t])[0]


# ── Comments ────────────────────────────────────────────────────────────
@router.get("/{ticket_id}/comments", response_model=list[TicketCommentOut])
def list_comments(
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> list[TicketCommentOut]:
    t = _ticket(db, ticket_id, user)
    q = db.query(TicketComment).filter(TicketComment.ticket_id == t.id, TicketComment.tenant_id == t.tenant_id)
    if not _staff(user):
        q = q.filter(TicketComment.is_internal.is_(False))
    rows = q.order_by(TicketComment.created_at).all()
    names = _names(db, user, {c.author_id for c in rows})
    out = []
    for c in rows:
        item = TicketCommentOut.model_validate(c)
        item.author_name = names.get(c.author_id, "")
        out.append(item)
    return out


@router.post("/{ticket_id}/comments", response_model=TicketCommentOut, status_code=201)
def add_comment(
    body: TicketCommentIn,
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> TicketCommentOut:
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
    # The reporter answering puts the ticket back in the queue: a "waiting on you" ticket,
    # and also a resolved or closed one ("still broken"), which no staff view lists as
    # open work, so leaving the status would bury the reply.
    from_reporter = str(t.reporter_id) == user.id
    if t.status in ("waiting", "resolved", "closed") and from_reporter and not body.is_internal:
        _apply(db, t, user, {"status": "open"})
    preview = body.body.strip().splitlines()[0][:200] if body.body.strip() else ""
    if body.is_internal:
        _tell(db, t, user, [t.assignee_id], "internal note", preview, kind="ticket_note")
    elif from_reporter:
        # Whoever owns it; if nobody does yet, every staff member.
        who = [t.assignee_id] if t.assignee_id else staff_ids(db, t.tenant_id)
        _tell(db, t, user, who, "reporter replied", preview, kind="ticket_reply")
    else:
        _tell(db, t, user, [t.reporter_id], "new reply", preview, kind="ticket_reply")
    if not body.is_internal:
        # An internal note must leave no trace the reporter can see, not even a
        # newer "updated" time or a re-sorted list.
        t.updated_at = _now()
    _audit(db, user, "ticket_comment_internal" if body.is_internal else "ticket_comment", str(t.id))
    db.commit()
    db.refresh(c)
    out = TicketCommentOut.model_validate(c)
    out.author_name = user.display_name
    return out


# ── Attachments ─────────────────────────────────────────────────────────
@router.get("/{ticket_id}/attachments", response_model=list[TicketAttachmentOut])
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


@router.post("/{ticket_id}/attachments", response_model=list[TicketAttachmentOut], status_code=201)
async def upload_attachments(
    files: list[UploadFile],
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> list[TicketAttachment]:
    t = _ticket(db, ticket_id, user)
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(413, f"Attach at most {MAX_FILES_PER_UPLOAD} files at a time")
    # Validate every file before storing any, and never read more than the limit + 1
    # byte, so an oversized upload neither fills memory nor leaves orphaned objects.
    staged: list[tuple[str, str, bytes]] = []
    for file in files:
        filename = (file.filename or "upload").replace("/", "_").replace("\\", "_")[:500]
        data = await file.read(MAX_ATTACHMENT_BYTES + 1)
        if not data:
            raise HTTPException(422, f"{filename} is empty")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, f"{filename} exceeds the 25 MB limit")
        staged.append((filename, (file.content_type or "application/octet-stream")[:120], data))

    created: list[TicketAttachment] = []
    stored: list[str] = []
    try:
        for filename, content_type, data in staged:
            att = TicketAttachment(
                tenant_id=t.tenant_id,
                ticket_id=t.id,
                filename=filename,
                content_type=content_type,
                size_bytes=len(data),
                uploaded_by=uuid.UUID(user.id),
            )
            db.add(att)
            db.flush()
            att.object_key = f"{t.tenant_id}/{t.id}/{att.id}"
            object_store.put_object(att.object_key, data, att.content_type, bucket=TICKET_BUCKET)
            stored.append(att.object_key)
            _log(db, t, user, "attachment", "", filename)
            created.append(att)
        _audit(db, user, "ticket_attachment_upload", str(t.id))
        db.commit()
    except Exception:
        db.rollback()
        for key in stored:
            with contextlib.suppress(Exception):
                object_store.delete_object(key, bucket=TICKET_BUCKET)
        raise
    for att in created:
        db.refresh(att)
    return created


# ── History ─────────────────────────────────────────────────────────────
@router.get("/{ticket_id}/activity", response_model=list[TicketActivityOut])
def list_activity(
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_CREATE)),
) -> list[TicketActivityOut]:
    t = _ticket(db, ticket_id, user)
    rows = (
        db.query(TicketActivity)
        .filter(TicketActivity.ticket_id == t.id, TicketActivity.tenant_id == t.tenant_id)
        .order_by(TicketActivity.created_at)
        .all()
    )

    # Assignee / queue changes are stored as ids; show names, which a student
    # cannot otherwise look up.
    def _ids(field: str) -> set[uuid.UUID]:
        found = set()
        for a in rows:
            if a.field != field:
                continue
            for v in (a.old_value, a.new_value):
                with contextlib.suppress(ValueError):
                    found.add(uuid.UUID(v))
        return found

    def _labels(model, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        rows_ = db.query(model.id, model.name).filter(model.id.in_(ids), model.tenant_id == t.tenant_id).all()
        return {r.id: r.name for r in rows_}

    names = _names(db, user, {a.actor_id for a in rows} | _ids("assignee_id"))
    lookups = {
        "assignee_id": names,
        "queue_id": _labels(SupportQueue, _ids("queue_id")),
        "range_id": _labels(Range, _ids("range_id")),
        "exercise_id": _labels(Exercise, _ids("exercise_id")),
    }

    def _show(field: str, value: str) -> str:
        if field not in lookups or not value:
            return value
        try:
            key = uuid.UUID(value)
        except ValueError:
            return value
        return lookups[field].get(key, "something deleted")

    out = []
    for a in rows:
        item = TicketActivityOut.model_validate(a)
        item.actor_name = names.get(a.actor_id, "")
        item.old_value = _show(a.field, a.old_value)
        item.new_value = _show(a.field, a.new_value)
        out.append(item)
    return out
