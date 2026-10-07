"""TrueNorth Range — Wiki / knowledge base router.

Confluence-style spaces holding a tree of markdown pages, with full revision history.

Permissions required per endpoint:

==================================================  ==========================
Endpoint                                            Permission(s)
==================================================  ==========================
GET    /wiki/spaces                                 WIKI_READ
POST   /wiki/spaces                                 WIKI_ADMIN
GET    /wiki/spaces/{slug}                          WIKI_READ
PUT    /wiki/spaces/{slug}                          WIKI_ADMIN
DELETE /wiki/spaces/{slug}  (archives)              WIKI_ADMIN
GET    /wiki/spaces/{slug}/tree                     WIKI_READ
POST   /wiki/spaces/{slug}/pages                    WIKI_EDIT
GET    /wiki/pages/{page_id}                        WIKI_READ
PUT    /wiki/pages/{page_id}                        WIKI_EDIT
DELETE /wiki/pages/{page_id}                        WIKI_EDIT
GET    /wiki/pages/{page_id}/revisions              WIKI_EDIT
GET    /wiki/pages/{page_id}/revisions/{n}          WIKI_EDIT
POST   /wiki/pages/{page_id}/revisions/{n}/restore  WIKI_EDIT
GET    /wiki/search?q=                              WIKI_READ
==================================================  ==========================

Visibility: a space marked ``staff`` is invisible (404, never 403) to anyone without
WIKI_EDIT, and so are unpublished pages. Saving a page requires the ``base_revision``
the editor started from; a stale one is a 409 carrying the current page, so concurrent
editors cannot silently overwrite each other.

History is editors-only: a page's earlier revisions can predate its publication (an
unpublished draft holding an answer key, later replaced by student instructions), so
"the page is published now" says nothing about what its old revisions contain.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ..auth import CurrentUser
from ..db import get_db
from ..models import AuditLog, User
from ..models_wiki import WikiPage, WikiRevision, WikiSpace
from ..rbac import Permission, require_permission, user_has_permission
from ..tenancy import get_owned, tenant_uuid

router = APIRouter(prefix="/wiki", tags=["wiki"])

SLUG_RE = r"^[a-z0-9][a-z0-9-]*$"


# ── Schemas ─────────────────────────────────────────────────────────────
def _not_blank(v: str | None) -> str | None:
    """Titles and names are trimmed; one made only of spaces is refused."""
    if v is None:
        return v
    v = v.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


class WikiSpaceIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=SLUG_RE)
    description: str = ""
    icon: str = Field("folder", max_length=50)
    visibility: str = Field("all", pattern=r"^(all|staff)$")

    _name = field_validator("name")(_not_blank)


class WikiSpaceUpdate(BaseModel):
    # Omit a field to keep it; an explicit null is refused (422), never written to a NOT NULL column.
    name: str = Field(default=None, min_length=1, max_length=255)
    description: str = Field(default=None)
    icon: str = Field(default=None, max_length=50)
    visibility: str = Field(default=None, pattern=r"^(all|staff)$")
    is_archived: bool = Field(default=None)

    _name = field_validator("name")(_not_blank)


class WikiSpaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    description: str
    icon: str
    visibility: str
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class WikiPageIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    body: str = ""
    parent_id: uuid.UUID | None = None
    tags: str = Field("", max_length=500)
    is_published: bool = True

    _title = field_validator("title")(_not_blank)


class WikiPageUpdate(BaseModel):
    base_revision: int = Field(..., ge=1, description="revision_number the edit started from")
    title: str | None = Field(None, min_length=1, max_length=500)
    body: str | None = None
    parent_id: uuid.UUID | None = None
    move_to_root: bool = False
    tags: str | None = Field(None, max_length=500)
    is_published: bool | None = None
    ordinal: int | None = None
    edit_summary: str = Field("", max_length=500)

    _title = field_validator("title")(_not_blank)


class WikiRestoreRequest(BaseModel):
    base_revision: int = Field(..., ge=1, description="revision_number the restore was decided against")


class WikiPageRef(BaseModel):
    """A link to another page: enough to name it and route to it."""

    id: uuid.UUID
    title: str


class WikiPageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    space_id: uuid.UUID
    space_slug: str = ""
    space_archived: bool = False
    parent_id: uuid.UUID | None = None
    title: str
    slug: str
    body: str
    tags: str
    ordinal: int
    is_published: bool
    author_id: uuid.UUID
    last_editor_id: uuid.UUID
    last_editor_name: str = ""
    revision_number: int
    breadcrumbs: list[WikiPageRef] = Field(default_factory=list)
    children: list[WikiPageRef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class WikiTreeNode(BaseModel):
    id: uuid.UUID
    title: str
    slug: str
    is_published: bool
    children: list[WikiTreeNode] = Field(default_factory=list)


class WikiRevisionListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    revision_number: int
    title: str
    editor_id: uuid.UUID
    editor_name: str = ""
    edit_summary: str
    created_at: datetime


class WikiRevisionOut(WikiRevisionListOut):
    body: str


class WikiSearchHit(BaseModel):
    page_id: uuid.UUID
    space_slug: str
    space_name: str
    title: str
    snippet: str
    updated_at: datetime


# ── Helpers ─────────────────────────────────────────────────────────────
def _audit(db: Session, user: CurrentUser, action: str, rtype: str, rid: str) -> None:
    db.add(AuditLog(user_id=uuid.UUID(user.id), action=action, resource_type=rtype, resource_id=rid))


def _staff(user: CurrentUser) -> bool:
    return user_has_permission(user, Permission.WIKI_EDIT)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "page")[:200]


def _names(db: Session, user: CurrentUser, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = db.query(User.id, User.display_name).filter(User.id.in_(ids), User.tenant_id == tenant_uuid(user)).all()
    return {r.id: r.display_name for r in rows}


def _space(db: Session, slug: str, user: CurrentUser) -> WikiSpace:
    """A space by slug in the caller's tenant, honouring staff-only visibility."""
    space = (
        db.query(WikiSpace)
        .filter(
            WikiSpace.tenant_id == tenant_uuid(user),
            WikiSpace.slug == slug,
            WikiSpace.deleted_at.is_(None),
        )
        .first()
    )
    hidden = space is not None and (space.visibility == "staff" or space.is_archived)
    if not space or (hidden and not _staff(user)):
        raise HTTPException(404, "Space not found")
    return space


def _writable(space: WikiSpace) -> None:
    """An archived space is read-only (for staff; students cannot see it at all)."""
    if space.is_archived:
        raise HTTPException(409, "This space is archived. Unarchive it to make changes.")


def _page(db: Session, page_id: uuid.UUID, user: CurrentUser) -> tuple[WikiPage, WikiSpace]:
    page = get_owned(db, WikiPage, page_id, user, not_found="Page not found")
    space = get_owned(db, WikiSpace, page.space_id, user, not_found="Page not found")
    if not _staff(user) and (space.visibility == "staff" or space.is_archived or not page.is_published):
        raise HTTPException(404, "Page not found")
    return page, space


def _visible_pages(db: Session, space: WikiSpace, user: CurrentUser):
    q = db.query(WikiPage).filter(
        WikiPage.tenant_id == tenant_uuid(user),
        WikiPage.space_id == space.id,
        WikiPage.deleted_at.is_(None),
    )
    if not _staff(user):
        q = q.filter(WikiPage.is_published.is_(True))
    return q


def _page_out(db: Session, page: WikiPage, space: WikiSpace, user: CurrentUser) -> WikiPageOut:
    siblings = _visible_pages(db, space, user).all()
    by_id = {p.id: p for p in siblings}
    crumbs: list[WikiPageRef] = []
    cursor = by_id.get(page.parent_id) if page.parent_id else None
    while cursor is not None and len(crumbs) < 50:
        crumbs.insert(0, WikiPageRef(id=cursor.id, title=cursor.title))
        cursor = by_id.get(cursor.parent_id) if cursor.parent_id else None
    children = sorted((p for p in siblings if p.parent_id == page.id), key=lambda p: (p.ordinal, p.title.lower()))
    out = WikiPageOut.model_validate(page)
    out.space_slug = space.slug
    out.space_archived = space.is_archived
    out.breadcrumbs = crumbs
    out.children = [WikiPageRef(id=c.id, title=c.title) for c in children]
    out.last_editor_name = _names(db, user, {page.last_editor_id}).get(page.last_editor_id, "")
    return out


def _conflict(db: Session, page_id: uuid.UUID, user: CurrentUser, *, rollback: bool = False) -> JSONResponse:
    """409 with the page as it is now, read fresh from the database.

    Before ``_claim`` succeeds nothing has been written, so re-reading is enough; after a
    failed commit (``rollback``) the transaction is discarded first.
    """
    if rollback:
        db.rollback()
    else:
        db.expire_all()
    page, space = _page(db, page_id, user)
    return JSONResponse(
        status_code=409,
        content={
            "detail": "This page was changed by someone else since you started editing.",
            "current": _page_out(db, page, space, user).model_dump(mode="json"),
        },
    )


def _claim(db: Session, page: WikiPage, base_revision: int, *, bump: bool) -> bool:
    """Compare-and-set on the page's revision_number, in the database, before any change.

    ``UPDATE ... WHERE revision_number = :base`` is atomic: of two saves from the same
    base, one matches and the other matches no row (on PostgreSQL the second waits for
    the first's row lock, then re-checks). A check against the page this request loaded
    cannot do that. With ``bump`` the revision advances; without it the row is still
    locked and checked, so a metadata-only save cannot overwrite a newer edit either.
    """
    result = db.execute(
        update(WikiPage)
        .where(WikiPage.id == page.id, WikiPage.revision_number == base_revision)
        .values(revision_number=base_revision + (1 if bump else 0))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    set_committed_value(page, "revision_number", base_revision + (1 if bump else 0))
    return True


def _write_revision(db: Session, page: WikiPage, user: CurrentUser, summary: str) -> None:
    db.add(
        WikiRevision(
            tenant_id=page.tenant_id,
            page_id=page.id,
            revision_number=page.revision_number,
            title=page.title,
            body=page.body,
            editor_id=uuid.UUID(user.id),
            edit_summary=summary,
        )
    )


def _check_parent(db: Session, page: WikiPage | None, space: WikiSpace, parent_id: uuid.UUID, user: CurrentUser):
    """The new parent must be in the same space and must not be the page or its descendant."""
    parent = get_owned(db, WikiPage, parent_id, user, not_found="Parent page not found")
    if parent.space_id != space.id:
        raise HTTPException(422, "Parent page is in a different space")
    if page is None:
        return
    cursor, hops = parent, 0
    while cursor is not None and hops < 500:
        if cursor.id == page.id:
            raise HTTPException(422, "A page cannot be moved under itself or one of its children")
        if cursor.parent_id is None:
            break
        # Walk up within the parent's tenant (parent came from get_owned above).
        cursor = (
            db.query(WikiPage).filter(WikiPage.id == cursor.parent_id, WikiPage.tenant_id == parent.tenant_id).first()
        )
        hops += 1


# ── Spaces ──────────────────────────────────────────────────────────────
@router.get("/spaces", response_model=list[WikiSpaceOut])
def list_spaces(
    include_archived: bool = Query(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_READ)),
) -> list[WikiSpace]:
    q = db.query(WikiSpace).filter(WikiSpace.tenant_id == tenant_uuid(user), WikiSpace.deleted_at.is_(None))
    if not _staff(user):
        q = q.filter(WikiSpace.visibility == "all")
    if not include_archived or not _staff(user):
        q = q.filter(WikiSpace.is_archived.is_(False))
    return q.order_by(WikiSpace.name).all()


@router.post("/spaces", response_model=WikiSpaceOut, status_code=201)
def create_space(
    body: WikiSpaceIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_ADMIN)),
) -> WikiSpace:
    tid = tenant_uuid(user)
    clash = db.query(WikiSpace).filter(WikiSpace.tenant_id == tid, WikiSpace.slug == body.slug).first()
    if clash:
        if clash.is_archived:
            raise HTTPException(
                409, f"An archived space uses the address '{body.slug}'. Unarchive it, or pick another."
            )
        raise HTTPException(409, f"A space with slug '{body.slug}' already exists")
    space = WikiSpace(tenant_id=tid, created_by=uuid.UUID(user.id), **body.model_dump())
    db.add(space)
    db.flush()
    _audit(db, user, "wiki_space_create", "wiki_space", str(space.id))
    db.commit()
    db.refresh(space)
    return space


@router.get("/spaces/{slug}", response_model=WikiSpaceOut)
def get_space(
    slug: str = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_READ)),
) -> WikiSpace:
    return _space(db, slug, user)


@router.put("/spaces/{slug}", response_model=WikiSpaceOut)
def update_space(
    body: WikiSpaceUpdate,
    slug: str = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_ADMIN)),
) -> WikiSpace:
    space = _space(db, slug, user)
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(space, k, v)
    _audit(db, user, "wiki_space_update", "wiki_space", str(space.id))
    db.commit()
    db.refresh(space)
    return space


@router.delete("/spaces/{slug}", status_code=204, response_class=Response)
def archive_space(
    slug: str = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_ADMIN)),
):
    """Archive, not destroy: pages and their history stay restorable."""
    space = _space(db, slug, user)
    space.is_archived = True
    _audit(db, user, "wiki_space_archive", "wiki_space", str(space.id))
    db.commit()


@router.get("/spaces/{slug}/tree", response_model=list[WikiTreeNode])
def space_tree(
    slug: str = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_READ)),
) -> list[WikiTreeNode]:
    space = _space(db, slug, user)
    pages = _visible_pages(db, space, user).order_by(WikiPage.ordinal, WikiPage.title).all()
    nodes = {p.id: WikiTreeNode(id=p.id, title=p.title, slug=p.slug, is_published=p.is_published) for p in pages}
    roots: list[WikiTreeNode] = []
    for p in pages:
        parent = nodes.get(p.parent_id) if p.parent_id else None
        # A page whose parent is hidden or deleted surfaces at the root rather than vanishing.
        (parent.children if parent else roots).append(nodes[p.id])
    return roots


@router.post("/spaces/{slug}/pages", response_model=WikiPageOut, status_code=201)
def create_page(
    body: WikiPageIn,
    slug: str = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_EDIT)),
) -> WikiPageOut:
    space = _space(db, slug, user)
    _writable(space)
    if body.parent_id:
        _check_parent(db, None, space, body.parent_id, user)
    uid = uuid.UUID(user.id)
    last = (
        _visible_pages(db, space, user)
        .filter(WikiPage.parent_id == body.parent_id if body.parent_id else WikiPage.parent_id.is_(None))
        .count()
    )
    page = WikiPage(
        tenant_id=space.tenant_id,
        space_id=space.id,
        parent_id=body.parent_id,
        title=body.title,
        slug=_slugify(body.title),
        body=body.body,
        tags=body.tags,
        is_published=body.is_published,
        ordinal=last,
        author_id=uid,
        last_editor_id=uid,
        revision_number=1,
    )
    db.add(page)
    db.flush()
    _write_revision(db, page, user, "Created")
    _audit(db, user, "wiki_page_create", "wiki_page", str(page.id))
    db.commit()
    db.refresh(page)
    return _page_out(db, page, space, user)


# ── Pages ───────────────────────────────────────────────────────────────
@router.get("/pages/{page_id}", response_model=WikiPageOut)
def get_page(
    page_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_READ)),
) -> WikiPageOut:
    page, space = _page(db, page_id, user)
    return _page_out(db, page, space, user)


@router.put("/pages/{page_id}", response_model=WikiPageOut)
def update_page(
    body: WikiPageUpdate,
    page_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_EDIT)),
):
    page, space = _page(db, page_id, user)
    _writable(space)
    if body.base_revision != page.revision_number:
        return _conflict(db, page.id, user)

    new_parent = None if body.move_to_root else (body.parent_id if body.parent_id is not None else page.parent_id)
    if new_parent is not None and new_parent != page.parent_id:
        _check_parent(db, page, space, new_parent, user)
    # Every real change advances the revision, metadata included. If publishing,
    # unpublishing, tags or a move left the number alone, an editor still holding the
    # old number could save a typo fix and silently undo it (re-publish a page someone
    # had just hidden). Each change is also a history row, so it can be seen and undone.
    changes: list[str] = []
    if body.title is not None and body.title != page.title:
        changes.append("title")
    if body.body is not None and body.body != page.body:
        changes.append("content")
    if body.tags is not None and body.tags != page.tags:
        changes.append("tags")
    if body.is_published is not None and body.is_published != page.is_published:
        changes.append("published" if body.is_published else "unpublished")
    if new_parent != page.parent_id:
        changes.append("moved")
    if body.ordinal is not None and body.ordinal != page.ordinal:
        changes.append("reordered")
    if not changes:
        return _page_out(db, page, space, user)
    if not _claim(db, page, body.base_revision, bump=True):
        return _conflict(db, page.id, user)

    page.parent_id = new_parent
    if body.title is not None and body.title != page.title:
        page.title = body.title
        page.slug = _slugify(body.title)
    if body.body is not None:
        page.body = body.body
    if body.tags is not None:
        page.tags = body.tags
    if body.is_published is not None:
        page.is_published = body.is_published
    if body.ordinal is not None:
        page.ordinal = body.ordinal

    page.last_editor_id = uuid.UUID(user.id)
    summary = body.edit_summary
    if not ({"title", "content"} & set(changes)) and not summary:
        summary = ", ".join(changes).capitalize()
    _write_revision(db, page, user, summary)
    _audit(db, user, "wiki_page_update", "wiki_page", str(page.id))
    try:
        db.commit()
    except IntegrityError:  # a revision row for this number already exists: someone else won
        return _conflict(db, page.id, user, rollback=True)
    db.refresh(page)
    return _page_out(db, page, space, user)


@router.delete("/pages/{page_id}", status_code=204, response_class=Response)
def delete_page(
    page_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_EDIT)),
):
    """Soft-delete the page and everything under it; revisions are kept."""
    page, space = _page(db, page_id, user)
    _writable(space)
    pages = _visible_pages(db, space, user).all()
    doomed, frontier = {page.id}, [page.id]
    while frontier:
        nxt = [p.id for p in pages if p.parent_id in frontier and p.id not in doomed]
        doomed.update(nxt)
        frontier = nxt
    for p in pages:
        if p.id in doomed:
            p.soft_delete()
    _audit(db, user, "wiki_page_delete", "wiki_page", str(page.id))
    db.commit()


@router.get("/pages/{page_id}/revisions", response_model=list[WikiRevisionListOut])
def list_revisions(
    page_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_EDIT)),
) -> list[WikiRevisionListOut]:
    page, _ = _page(db, page_id, user)
    revs = (
        db.query(WikiRevision)
        .filter(WikiRevision.page_id == page.id, WikiRevision.tenant_id == tenant_uuid(user))
        .order_by(WikiRevision.revision_number.desc())
        .all()
    )
    names = _names(db, user, {r.editor_id for r in revs})
    out = []
    for r in revs:
        item = WikiRevisionListOut.model_validate(r)
        item.editor_name = names.get(r.editor_id, "")
        out.append(item)
    return out


def _revision(db: Session, page: WikiPage, n: int, user: CurrentUser) -> WikiRevision:
    rev = (
        db.query(WikiRevision)
        .filter(
            WikiRevision.page_id == page.id,
            WikiRevision.tenant_id == tenant_uuid(user),
            WikiRevision.revision_number == n,
        )
        .first()
    )
    if not rev:
        raise HTTPException(404, "Revision not found")
    return rev


@router.get("/pages/{page_id}/revisions/{n}", response_model=WikiRevisionOut)
def get_revision(
    page_id: uuid.UUID = Path(...),
    n: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_EDIT)),
) -> WikiRevisionOut:
    page, _ = _page(db, page_id, user)
    rev = _revision(db, page, n, user)
    out = WikiRevisionOut.model_validate(rev)
    out.editor_name = _names(db, user, {rev.editor_id}).get(rev.editor_id, "")
    return out


@router.post(
    "/pages/{page_id}/revisions/{n}/restore",
    response_model=WikiPageOut,
    responses={409: {"description": "The page changed since base_revision; body carries the current page"}},
)
def restore_revision(
    body: WikiRestoreRequest,
    page_id: uuid.UUID = Path(...),
    n: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_EDIT)),
):
    """Bring an old version back as a NEW revision; history is never rewritten.

    Like an edit, it names the revision it was decided against (``base_revision``): a
    restore chosen while looking at revision 4 must not wipe out a revision 5 saved
    since. A stale base is a 409 with the current page.
    """
    page, space = _page(db, page_id, user)
    _writable(space)
    rev = _revision(db, page, n, user)
    if body.base_revision != page.revision_number or not _claim(db, page, body.base_revision, bump=True):
        return _conflict(db, page.id, user)
    page.title = rev.title
    page.slug = _slugify(rev.title)
    page.body = rev.body
    page.last_editor_id = uuid.UUID(user.id)
    _write_revision(db, page, user, f"Restored revision {n}")
    _audit(db, user, "wiki_page_restore", "wiki_page", str(page.id))
    try:
        db.commit()
    except IntegrityError:
        return _conflict(db, page.id, user, rollback=True)
    db.refresh(page)
    return _page_out(db, page, space, user)


# ── Search ──────────────────────────────────────────────────────────────
def _snippet(body: str, q: str, width: int = 160) -> str:
    idx = body.lower().find(q.lower())
    if idx < 0:
        return body[:width].strip()
    start = max(0, idx - width // 3)
    text = body[start : start + width].strip()
    return ("…" if start else "") + text + ("…" if start + width < len(body) else "")


@router.get("/search", response_model=list[WikiSearchHit])
def search(
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_READ)),
) -> list[WikiSearchHit]:
    tid = tenant_uuid(user)
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    query = (
        db.query(WikiPage, WikiSpace)
        .join(WikiSpace, WikiSpace.id == WikiPage.space_id)
        .filter(
            WikiPage.tenant_id == tid,
            WikiSpace.tenant_id == tid,
            WikiPage.deleted_at.is_(None),
            WikiSpace.deleted_at.is_(None),
            WikiSpace.is_archived.is_(False),
            or_(
                WikiPage.title.ilike(pattern, escape="\\"),
                WikiPage.body.ilike(pattern, escape="\\"),
                WikiPage.tags.ilike(pattern, escape="\\"),
            ),
        )
    )
    if not _staff(user):
        query = query.filter(WikiSpace.visibility == "all", WikiPage.is_published.is_(True))
    rows = query.order_by(WikiPage.updated_at.desc()).limit(limit * 2).all()
    ql = q.lower()
    rows.sort(key=lambda r: 0 if ql in r[0].title.lower() else 1)
    return [
        WikiSearchHit(
            page_id=p.id,
            space_slug=s.slug,
            space_name=s.name,
            title=p.title,
            snippet=_snippet(p.body, q),
            updated_at=p.updated_at,
        )
        for p, s in rows[:limit]
    ]
