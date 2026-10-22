"""Recording and promoting golden-image builds (ADR 0013 §2).

* ``record_build``: the template kit registers a build (PATCH /golden-images/{id} with a
  ``build``). Idempotent per ``build_id``; a second registration may add the copies of
  hosts it did not list before. A promoted build is immutable, and one ``build_id`` never
  changes its checksum: a different build needs a different id.
* ``promote``: an administrator makes one build the one ranges clone. Exactly one build
  per image is promoted (a partial unique index backs this); each promotion writes an
  ``audit_logs`` row naming the build, its checksum and the build it replaced.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import AuditLog, GoldenImage
from .models import GoldenImageBuild
from .schemas import GoldenImageBuildIn, GoldenImageBuildOut

AUDIT_RESOURCE = "golden_image"
_COPY = re.compile(r"^(?P<base>[^@]+)@(?P<host>[A-Za-z0-9_-]+)-(?P<date>\d{8})$")


class BuildConflictError(ValueError):
    """The registration contradicts what is recorded for that build."""


class BuildNotPromotableError(ValueError):
    """The build lacks what a range needs to clone it."""


def copies_of(build: GoldenImageBuild) -> dict[str, str]:
    try:
        got = json.loads(build.copies or "{}")
    except (TypeError, ValueError):
        return {}
    return {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) else {}


def _check_copies(copies: dict[str, str], base: str) -> dict[str, str]:
    """Copy names must be ``<base>@<host>-<yyyymmdd>``: of this build's template (``base``)
    and for the host they are listed under. A copy of another template is refused."""
    out: dict[str, str] = {}
    for host, name in copies.items():
        short = host.split(".", 1)[0].lower()
        m = _COPY.match(name)
        if m is None or m["host"].lower() != short:
            raise BuildConflictError(f"copy {name!r} is not a copy for host {short!r} (<id>@{short}-<yyyymmdd>)")
        if m["base"] != base:
            raise BuildConflictError(f"copy {name!r} is a copy of {m['base']!r}, not of this build's template {base!r}")
        out[short] = name
    return out


def promotable_problem(row: GoldenImageBuild) -> str | None:
    """Why a range could not clone this build if it were promoted, or None. A build must name
    at least one host copy: ranges clone only a promoted build's stamped host copies, so a
    record written before replicate (``build.sh manifest``, a fresh build) is incomplete."""
    if not copies_of(row):
        return "no host copies recorded yet: replicate the build (build.sh replicate) and register it again"
    return None


def missing_hosts(db: Session, row: GoldenImageBuild) -> list[str]:
    """Discovered vSphere hosts (active connections, minus VSPHERE_EXCLUDE_HOSTS where the API
    has it) without a copy of this build: placement will not use them for its image."""
    import os

    from ..models import HypervisorConnection, HypervisorNode

    excluded = {e.strip().split(".", 1)[0].lower() for e in os.getenv("VSPHERE_EXCLUDE_HOSTS", "").split(",")
                if e.strip()}
    names = (db.query(HypervisorNode.node_name).join(HypervisorConnection)
             .filter(HypervisorConnection.hypervisor_type == "vsphere", HypervisorConnection.is_active.is_(True))
             .all())
    have = set(copies_of(row))
    shorts = {str(n).split(".", 1)[0].lower() for (n,) in names if n}
    return sorted(shorts - have - excluded)


def _who(user: CurrentUser) -> str:
    return (user.email or user.display_name or str(user.id) or "")[:255]


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _audit(db: Session, user: CurrentUser, action: str, img: GoldenImage, detail: dict) -> None:
    # An explicit timestamp: the column's server default has one-second resolution on SQLite,
    # and the image's audit trail is listed newest first.
    db.add(AuditLog(timestamp=datetime.now(UTC), user_id=_uuid(user.id), tenant_id=_uuid(user.tenant_id),
                    action=action, resource_type=AUDIT_RESOURCE, resource_id=str(img.id),
                    detail=json.dumps(detail, sort_keys=True, default=str)))


def builds(db: Session, img: GoldenImage) -> list[GoldenImageBuild]:
    """The image's builds, newest registration first."""
    return (db.query(GoldenImageBuild).filter(GoldenImageBuild.golden_image_id == img.id)
            .order_by(GoldenImageBuild.created_at.desc(), GoldenImageBuild.build_id.desc()).all())


def promoted_build(db: Session, img: GoldenImage) -> GoldenImageBuild | None:
    return (db.query(GoldenImageBuild)
            .filter(GoldenImageBuild.golden_image_id == img.id, GoldenImageBuild.promoted.is_(True))
            .one_or_none())


def record_build(db: Session, img: GoldenImage, body: GoldenImageBuildIn, user: CurrentUser,
                 template_name: str | None = None) -> GoldenImageBuild:
    """Record (or extend) one build of ``img``. Not committed: the caller commits. Every
    conflict is raised before anything changes. ``template_name``: the image's template as
    the same request sets it (the build's default template)."""
    row = (db.query(GoldenImageBuild)
           .filter(GoldenImageBuild.golden_image_id == img.id, GoldenImageBuild.build_id == body.build_id)
           .one_or_none())
    base = (row.template_name if row is not None else "") or body.template_name or template_name \
        or img.template_name or img.catalogue_id
    copies = _check_copies(body.copies, base)
    if row is None:
        row = GoldenImageBuild(golden_image_id=img.id, build_id=body.build_id, checksum=body.checksum,
                               build_date=body.build_date, iso_checksum=body.iso_checksum, template_name=base,
                               copies=json.dumps(copies, sort_keys=True), registered_by=_who(user))
        db.add(row)
        _audit(db, user, "golden_image.build_register", img,
               {"build_id": body.build_id, "checksum": body.checksum, "copies": copies})
        return row
    if row.checksum != body.checksum:
        raise BuildConflictError(f"build {body.build_id!r} is recorded with checksum {row.checksum}; "
                                 "a different build needs a different build id")
    have = copies_of(row)
    changed = {h: n for h, n in copies.items() if have.get(h) != n}
    clashes = {h: n for h, n in changed.items() if h in have}
    if row.promoted and (changed or (body.iso_checksum and body.iso_checksum != row.iso_checksum)):
        raise BuildConflictError(f"build {body.build_id!r} is promoted and cannot change; register a new build")
    if clashes:
        raise BuildConflictError(f"build {body.build_id!r} already names "
                                 + ", ".join(f"{have[h]!r} on {h}" for h in sorted(clashes)))
    if changed:
        row.copies = json.dumps({**have, **changed}, sort_keys=True)
        _audit(db, user, "golden_image.build_register", img,
               {"build_id": body.build_id, "checksum": body.checksum, "copies_added": changed})
    if body.iso_checksum and not row.iso_checksum:
        row.iso_checksum = body.iso_checksum
    if body.build_date and row.build_date is None:
        row.build_date = body.build_date
    return row


def promote(db: Session, img: GoldenImage, build_id: str, user: CurrentUser, reason: str = "") -> GoldenImageBuild:
    """Make ``build_id`` the build ranges of ``img`` clone. Commits; audited when it changes anything.

    Concurrent promotions of one image serialise on its row (``SELECT ... FOR UPDATE`` on
    Postgres); one that still collides on the one-promoted-build index is a
    ``BuildConflictError`` (409), never a server error, and writes nothing."""
    from sqlalchemy.exc import IntegrityError

    # tenant-safe: a row lock on the platform-wide registry image the caller (the platform
    # administrator) already resolved; it serialises concurrent promotions of that image.
    db.query(GoldenImage).filter(GoldenImage.id == img.id).with_for_update().one()
    row = (db.query(GoldenImageBuild)
           .filter(GoldenImageBuild.golden_image_id == img.id, GoldenImageBuild.build_id == build_id)
           .one_or_none())
    if row is None:
        raise LookupError(f"image {img.catalogue_id!r} has no build {build_id!r}")
    if row.promoted:
        return row  # nothing changed: the row lock ends with the request's transaction
    if problem := promotable_problem(row):
        raise BuildNotPromotableError(f"build {build_id!r} cannot be promoted: {problem}")
    before = promoted_build(db, img)
    try:
        if before is not None:
            before.promoted = False
            db.flush()  # the partial unique index allows one promoted build per image
        row.promoted = True
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise BuildConflictError(f"another promotion of {img.catalogue_id!r} happened at the same time; "
                                 "reload and try again") from exc
    row.promoted_at = datetime.now(UTC)
    row.promoted_by = _who(user)
    status_was = img.build_status
    if img.build_status != "built":
        img.build_status = "built"
    _audit(db, user, "golden_image.promote", img, {
        "catalogue_id": img.catalogue_id, "build_id": row.build_id, "checksum": row.checksum,
        "copies": copies_of(row), "previous_build_id": before.build_id if before else None,
        "build_status_was": status_was, "reason": reason,
    })
    db.commit()
    db.refresh(row)
    return row


def build_out(row: GoldenImageBuild, db: Session | None = None) -> GoldenImageBuildOut:
    problem = promotable_problem(row)
    return GoldenImageBuildOut(
        id=str(row.id), build_id=row.build_id, template_name=row.template_name or "", checksum=row.checksum,
        build_date=row.build_date, iso_checksum=row.iso_checksum or "", copies=copies_of(row),
        registered_by=row.registered_by or "", registered_at=row.created_at, promoted=bool(row.promoted),
        promoted_at=row.promoted_at, promoted_by=row.promoted_by or "",
        promotable=problem is None, not_promotable_reason=problem or "",
        missing_hosts=missing_hosts(db, row) if db is not None else [],
    )
