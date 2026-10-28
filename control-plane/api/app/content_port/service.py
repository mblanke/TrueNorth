"""The content port's API side: proven status, export, preview and import.

Who may do what:

* the **proven status** of an item: ``content_review:read``, for content the caller's
  tenant may read (its own, or shared); the evidence counted is the caller's tenant's, or
  every tenant's for the platform administrator (``proven.scope_for``);
* **export**, **preview** and **import**: the platform administrator holding
  ``content_review:admin`` (``can_port``), as for feed bundles: a bundle moves content
  between sites and may land as shared content every tenant reads.

Export refuses (409) unless every selected item, and every item they need, is proven,
listing each that is not and why. Preview and import verify the bundle against the
configured trusted keys first (``signing``; none configured: 409, fail closed). Every
export, import and refused bundle is audit-logged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .. import secretbox
from ..auth import CurrentUser
from ..models import AuditLog, Tenant
from ..rbac import Permission, is_platform_admin, user_has_permission
from ..tenancy import tenant_uuid
from . import bundle as bnd
from . import importer, proven, signing
from .models import ContentPortImport, ContentPortItem
from .schemas import (
    CONTENT_MODELS,
    PortGoldenBuild,
    PortImportedFromOut,
    PortImportOut,
    PortItemRef,
    PortPreviewOut,
    ProvenConditionOut,
    ProvenDependencyOut,
    ProvenEvidenceOut,
    ProvenStatusOut,
)

RESOURCE = "content_port"


def now() -> datetime:
    return datetime.now(UTC)


def can_port(db: Session, user: CurrentUser) -> bool:
    return user_has_permission(user, Permission.CONTENT_REVIEW_ADMIN) and is_platform_admin(user, db)


def require_port(db: Session, user: CurrentUser) -> None:
    if not can_port(db, user):
        raise HTTPException(403, "Only the platform administrator ports content between sites")


def _uid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError):
        return None


def audit(db: Session, user: CurrentUser, action: str, resource_id: str, detail: str) -> None:
    db.add(AuditLog(user_id=_uid(user), tenant_id=tenant_uuid(user), action=action, resource_type=RESOURCE,
                    resource_id=resource_id[:255], detail=detail[:2000]))


# ── proven status ─────────────────────────────────────────────────────────────────
def _label(st: proven.Status) -> str:
    if st.proven:
        return "Proven"
    return "Not proven: " + "; ".join(st.reasons)


def status_out(db: Session, item_type: str, row, scope: proven.Scope, *,
               with_dependencies: bool = True, viewer: uuid.UUID | None = None) -> ProvenStatusOut:
    """``viewer``: the reading tenant. A dependency that is another tenant's private content is not
    named and its status is not shown, whoever reads (the platform administrator's evidence is
    every tenant's, but not their private names)."""
    st = proven.status(db, item_type, row, scope, viewer)
    deps = []
    if with_dependencies:
        for d in st.dependencies:
            dep_row = proven.load(db, d.item_type, d.item_id) if d.title is not None else None
            shown = dep_row is not None and (viewer is None or proven.visible(dep_row, viewer))
            deps.append(ProvenDependencyOut(
                item_type=d.item_type, item_id=d.item_id, title=d.title,
                proven=proven.status(db, d.item_type, dep_row, scope).proven if shown else None))
    link = (db.query(ContentPortItem)
            .filter(ContentPortItem.item_type == item_type, ContentPortItem.local_id == str(row.id)).first())
    imported = None
    if link is not None:
        imp = db.get(ContentPortImport, link.import_id)  # tenant-safe: the import row of a link to a visible item
        imported = PortImportedFromOut(
            origin_key_id=link.origin_key_id, exported_at=link.exported_at,
            imported_at=imp.imported_at if imp is not None else link.created_at,
            proven_at_origin=bool((link.proof or {}).get("proven")),
            golden_builds=[PortGoldenBuild.model_validate(b) for b in (link.golden_builds or [])])
    return ProvenStatusOut(
        item_type=item_type, item_id=str(row.id), title=row.name, proven=st.proven, label=_label(st),
        reasons=st.reasons,
        conditions=[ProvenConditionOut(name=c.name, title=proven.CONDITION_TITLES[c.name], met=c.met,
                                 not_applicable=c.not_applicable, reasons=c.reasons,
                                 evidence=[ProvenEvidenceOut(kind=e.kind, id=e.id, label=e.label, at=e.at)
                                           for e in c.evidence])
                    for c in st.conditions],
        dependencies=deps, golden_builds=[PortGoldenBuild.model_validate(b) for b in st.golden_builds],
        imported=imported)


def item_status(db: Session, user: CurrentUser, item_type: str, item_id) -> ProvenStatusOut:
    row = proven.any_visible(db, user, item_type, item_id)
    if row is None:
        raise HTTPException(404, f"No {proven.LABELS.get(item_type, 'item')} {item_id} here")
    return status_out(db, item_type, row, proven.scope_for(db, user), viewer=tenant_uuid(user))


def portable(db: Session, user: CurrentUser) -> list[ProvenStatusOut]:
    """Every course, range template and scenario the caller may port, with its status."""
    from ..exercise_templates.models import ExerciseTemplate
    from ..models import Course, Scenario, Template

    tenant = tenant_uuid(user)
    scope = proven.scope_for(db, user)
    out: list[ProvenStatusOut] = []
    for item_type, model in (("range_template", Template), ("scenario", Scenario), ("course", Course),
                             ("exercise_template", ExerciseTemplate)):
        q = db.query(model)
        if hasattr(model, "deleted_at"):
            q = q.filter(model.deleted_at.is_(None))
        for row in q.order_by(model.name, model.id).all():
            if proven.visible(row, tenant):
                out.append(status_out(db, item_type, row, scope, with_dependencies=False, viewer=tenant))
    return out


# ── export ─────────────────────────────────────────────────────────────────────────
def export(db: Session, user: CurrentUser, refs: list[PortItemRef]) -> dict:
    """The signed bundle of ``refs`` and everything they need, or 409 listing what is not
    proven (or cannot be carried). Audit-logged; does not commit."""
    require_port(db, user)
    tenant = tenant_uuid(user)
    roots = []
    for r in refs:
        row = proven.any_visible(db, user, r.item_type, r.item_id)
        if row is None:
            raise HTTPException(404, f"No {proven.LABELS[r.item_type]} {r.item_id} here")
        roots.append((r.item_type, row))
    items, broken = proven.closure(db, roots)
    problems: list[dict] = [{"item_type": d.item_type, "item_id": d.item_id, "title": None,
                             "reasons": ["A dependency that is missing here, deleted, or not shared with this "
                                         "item's tenant"]} for d in broken]
    statuses: dict[str, proven.Status] = {}
    scope = proven.scope_for(db, user)
    root_keys = {bnd.key(t, str(r.id)) for t, r in roots}
    for item_type, row in items:
        k = bnd.key(item_type, str(row.id))
        if not proven.visible(row, tenant):
            problems.append({"item_type": item_type, "item_id": str(row.id), "title": None,
                             "reasons": ["Another tenant's content: it cannot be exported from here"]})
            continue
        st = proven.status(db, item_type, row, scope, tenant)  # no other tenant's private name in a refusal
        statuses[k] = st
        if not st.proven:
            problems.append({"item_type": item_type, "item_id": str(row.id), "title": row.name,
                             "dependency": k not in root_keys, "reasons": st.reasons})
    if not problems:
        for item_type, row in items:
            content = bnd.content_of(db, item_type, row, lambda t, i: bnd.key(t, i))
            try:
                CONTENT_MODELS[item_type].model_validate(content)
            except ValidationError as exc:
                problems.append({"item_type": item_type, "item_id": str(row.id), "title": row.name,
                                 "reasons": [f"It cannot be carried in a bundle ({bnd._first(exc)})"]})
            if item_type in ("range_template", "scenario") and not content.get("yaml"):
                problems.append({"item_type": item_type, "item_id": str(row.id), "title": row.name,
                                 "reasons": ["Its YAML does not parse, so it cannot be carried"]})
    if problems:
        raise HTTPException(409, {"message": "Only proven content is exported; nothing was exported. Prove these "
                                             "first (a dependency must be proven too)", "items": problems})
    try:
        doc = bnd.build(db, items, statuses, now=now())
    except secretbox.SecretKeyMissingError as exc:
        raise HTTPException(409, str(exc)) from None
    audit(db, user, "content_port.export", doc["bundle_id"],
          f"{len(items)} item(s): {', '.join(bnd.key(t, str(r.id)) for t, r in items)[:1500]}; key "
          f"{doc['signature']['key_id']}; sha256 {signing.signed_sha256(doc)}")
    return doc


def signing_key(db: Session, user: CurrentUser) -> dict:
    require_port(db, user)
    try:
        return signing.public_info()
    except secretbox.SecretKeyMissingError as exc:
        raise HTTPException(409, str(exc)) from None


# ── preview and import ─────────────────────────────────────────────────────────────
def _target(db: Session, user: CurrentUser, target_tenant_id: uuid.UUID | None) -> uuid.UUID:
    tid = target_tenant_id or tenant_uuid(user)
    if db.get(Tenant, tid) is None:
        raise HTTPException(404, "No such tenant")
    return tid


def _read(db: Session, user: CurrentUser, raw: bytes):
    """Verify and parse, or HTTPException after an audit row (the caller commits it)."""
    try:
        return bnd.parse(raw, now=now())
    except signing.NoTrustedKeysError as exc:
        raise HTTPException(409, str(exc)) from None
    except bnd.BundleRejectedError as exc:
        audit(db, user, "content_port.bundle_rejected", "bundle", exc.message)
        raise HTTPException(exc.status, exc.message) from None


def _preview_out(pl: importer.Plan, items=None) -> PortPreviewOut:
    return PortPreviewOut(bundle_id=pl.bundle.bundle_id, exported_at=pl.bundle.exported_at, key_id=pl.key_id,
                      signed_sha256=pl.signed_sha256, target_tenant_id=pl.tenant_id, shared=pl.shared,
                      already_imported_at=pl.already_imported_at,
                      items=[p.out() for p in (items if items is not None else pl.items)],
                      missing_golden_builds=pl.missing_builds, can_import=pl.can_import)


def preview(db: Session, user: CurrentUser, raw: bytes, *, target_tenant_id: uuid.UUID | None,
            shared: bool) -> PortPreviewOut:
    require_port(db, user)
    tid = _target(db, user, target_tenant_id)
    doc, bundle, kid = _read(db, user, raw)
    return _preview_out(importer.plan(db, doc, bundle, kid, tenant_id=tid, shared=shared))


def import_bundle(db: Session, user: CurrentUser, raw: bytes, *, target_tenant_id: uuid.UUID | None, shared: bool,
                  overwrite: list[str]) -> PortImportOut:
    require_port(db, user)
    tid = _target(db, user, target_tenant_id)
    doc, bundle, kid = _read(db, user, raw)
    pl = importer.plan(db, doc, bundle, kid, tenant_id=tid, shared=shared)
    if pl.already_imported_at is not None:
        audit(db, user, "content_port.bundle_rejected", str(bundle.bundle_id),
              f"replay: bundle {bundle.bundle_id} was imported on {pl.already_imported_at.isoformat()}")
        raise HTTPException(409, f"This bundle was already imported on {pl.already_imported_at:%d %b %Y %H:%M}; "
                                 f"export a new one to import again")
    unknown = sorted(set(overwrite) - {p.item.key for p in pl.items if p.action == "conflict"})
    if unknown:
        raise HTTPException(422, f"overwrite names items that are not conflicts in this bundle: {', '.join(unknown[:5])}")
    imp, done = importer.apply(db, _uid(user), pl, set(overwrite), now=now())
    counts = imp.summary
    audit(db, user, "content_port.import", str(bundle.bundle_id),
          f"key {kid}; sha256 {pl.signed_sha256}; tenant {tid}{' (shared)' if shared else ''}; "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
          + (f"; overwrote {', '.join(sorted(overwrite))[:800]}" if overwrite else ""))
    return PortImportOut(import_id=imp.id, bundle_id=bundle.bundle_id, created=counts.get("create", 0),
                     updated=counts.get("update", 0), unchanged=counts.get("unchanged", 0),
                     skipped=counts.get("skipped", 0) + counts.get("refused", 0),
                     items=[p.out() for p in done], missing_golden_builds=pl.missing_builds)
