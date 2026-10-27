"""The content feeds' API side (ADR 0014 decision 25, issue #287): the feeds status and the
signed feed bundle import.

Settings (the installer's ``tn_feeds_*``):

* ``TN_FEEDS_MODE``: ``off`` (default), ``online`` or ``bundle``. Bundles are imported only
  in ``bundle`` mode.
* ``TN_FEEDS_BUNDLE_PUBLIC_KEY``: the Ed25519 public key (base64, 32 raw bytes) that signs
  this site's feed bundles. Without it no bundle is accepted.

A bundle is applied only when its signature verifies against that key
(``bundle_signing``: Ed25519 over a feed-bundle context and the canonical JSON, built on the
range certification helpers). An unsigned bundle, one signed by any other key or the
range-certification way, one changed after signing, one that is not exactly a feed bundle
(unknown fields included), one dated more than a day ahead, one older than the last import,
or a file the parser cannot read is refused, recorded on the status panel and in the audit
log, and nothing of it is stored.

**Each bundle replaces the last.** Its entries are the bundle-origin entries from then on:
an entry the previous bundle had and this one lacks is removed, and an entry's products are
the new bundle's, not a union. So a bad bundle is undone by importing a newer, correct one
(docs: install/README.md "Content feeds"). Entries the online pull stored are not touched.
Flags a bad entry already raised stay on their items until reviewed; Mark reviewed clears
them, and a removed entry never raises them again. The worker's content clock sync turns
entries into stale flags within the hour.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import AuditLog
from ..range_certification import signing
from ..rbac import Permission, is_platform_admin, user_has_permission
from ..tenancy import tenant_uuid
from . import bundle_signing
from .models import ContentFeedEntry, ContentFeedFlag, ContentFeedSource
from .schemas import FeedBundleIn, FeedImportOut, FeedSourceOut, FeedStatusOut

MODES = ("off", "online", "bundle")
ONLINE = ("endoflife", "cisa_kev", "nvd")
BUNDLE = "bundle"
LABELS = {
    "endoflife": "endoflife.date (releases and end of life)",
    "cisa_kev": "CISA Known Exploited Vulnerabilities",
    "nvd": "NVD CVE API 2.0",
    BUNDLE: "Signed feed bundle",
}
MAX_BUNDLE_BYTES = 50 * 1024 * 1024  # the edge's upload limit for this route (nginx 50M)
MAX_FUTURE = timedelta(hours=24)  # clock skew allowed between the bundle builder and the site


class BundleRejectedError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def now() -> datetime:
    return datetime.now(UTC)


def mode() -> str:
    value = os.getenv("TN_FEEDS_MODE", "").strip().lower() or "off"
    return value if value in MODES else "off"


def bundle_key() -> tuple[object | None, str | None]:
    """(the configured public key, None) or (None, why there is none)."""
    raw = os.getenv("TN_FEEDS_BUNDLE_PUBLIC_KEY", "").strip()
    if not raw:
        return None, "No feed bundle public key is configured (tn_feeds_bundle_public_key)"
    try:
        if len(base64.b64decode(raw, validate=True)) != 32:
            raise ValueError
        return signing.public_key_from_b64(raw), None
    except ValueError:
        return None, "The configured feed bundle public key is not a base64 Ed25519 public key"


def _uid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError):
        return None


def _audit(db: Session, user: CurrentUser, action: str, detail: str) -> None:
    db.add(AuditLog(user_id=_uid(user), tenant_id=tenant_uuid(user), action=action, resource_type="content_feeds",
                    resource_id=BUNDLE, detail=detail[:2000]))


def _record(db: Session, source: str, *, at: datetime, ok: bool, error: str = "", entries: int = 0,
            detail: dict | None = None) -> None:
    row = db.get(ContentFeedSource, source)
    if row is None:
        row = ContentFeedSource(source=source, last_error="", entries=0, cursor="", detail={})
        db.add(row)
    row.last_attempt_at = at
    row.last_error = error[:1000]
    row.entries = entries
    if ok:
        row.last_success_at = at
    if detail is not None:
        row.detail = detail


# ── status ───────────────────────────────────────────────────────────────────
def can_import(db: Session, user: CurrentUser) -> bool:
    """Feed entries are platform-wide (they flag shared content in every tenant): a platform
    administrator imports them."""
    return user_has_permission(user, Permission.CONTENT_REVIEW_ADMIN) and is_platform_admin(user, db)


def _dt(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def status(db: Session, user: CurrentUser) -> FeedStatusOut:
    from ..content_review.models import ContentReviewItem

    current = mode()
    key, problem = bundle_key()
    if current != "bundle":
        problem = "Feed bundles are imported only when feeds are in bundle mode (tn_feeds_mode: bundle)"
    rows = {r.source: r for r in db.query(ContentFeedSource).all()}
    shown = list(ONLINE) if current == "online" else [BUNDLE] if current == "bundle" else []
    shown += [s for s in (*ONLINE, BUNDLE) if s in rows and s not in shown]
    sources = []
    for name in shown:
        r = rows.get(name)
        detail = (r.detail if r is not None and isinstance(r.detail, dict) else {}) or {}
        sources.append(FeedSourceOut(
            source=name, label=LABELS[name], last_attempt_at=r.last_attempt_at if r else None,
            last_success_at=r.last_success_at if r else None, last_error=r.last_error if r else "",
            entries=r.entries if r else 0, generated_at=_dt(detail.get("generated_at")),
            key_id=detail.get("key_id")))
    flagged = (db.query(func.count(func.distinct(ContentFeedFlag.item_id)))
               .join(ContentReviewItem, ContentReviewItem.id == ContentFeedFlag.item_id)
               .filter(or_(ContentReviewItem.tenant_id == tenant_uuid(user), ContentReviewItem.tenant_id.is_(None)),
                       ContentReviewItem.missing.is_(False)).scalar() or 0)
    return FeedStatusOut(
        mode=current, can_import=key is not None and problem is None and can_import(db, user),
        bundle_key_id=signing.key_id(key) if key is not None else None,
        bundle_key_problem=None if key is not None and problem is None else problem,
        sources=sources, entries=db.query(func.count(ContentFeedEntry.id)).scalar() or 0, flagged_items=flagged)


# ── import ───────────────────────────────────────────────────────────────────
def _reject(db: Session, user: CurrentUser, at: datetime, status_code: int, message: str) -> BundleRejectedError:
    _record(db, BUNDLE, at=at, ok=False, error=message)
    _audit(db, user, "content_feeds.bundle_rejected", message)
    return BundleRejectedError(status_code, message)


def import_bundle(db: Session, user: CurrentUser, raw: bytes) -> FeedImportOut:
    """Verify, then store. Raises BundleRejectedError (after recording why; the caller commits
    that record) when the bundle is not applied."""
    at = now()
    if mode() != "bundle":
        raise BundleRejectedError(409, "Feed bundles are imported only when feeds are in bundle mode "
                                       "(tn_feeds_mode: bundle)")
    key, problem = bundle_key()
    if key is None:
        raise BundleRejectedError(409, problem or "No feed bundle public key is configured")
    if len(raw) > MAX_BUNDLE_BYTES:
        raise _reject(db, user, at, 413, f"The bundle is larger than {MAX_BUNDLE_BYTES // (1024 * 1024)} MB")
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError, MemoryError):  # not JSON, not UTF-8, nested too deep
        raise _reject(db, user, at, 422, "The file is not a feed bundle (not readable JSON)") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("signature"), dict):
        raise _reject(db, user, at, 422, "The bundle is not signed; nothing was imported")
    if not bundle_signing.verify(doc, key):
        raise _reject(db, user, at, 422, "The bundle's signature does not verify against this site's feed key; "
                                         "nothing was imported")
    try:
        bundle = FeedBundleIn.model_validate({k: v for k, v in doc.items() if k != "signature"})
    except RecursionError:
        raise _reject(db, user, at, 422, "The signed bundle is not a feed bundle (nested too deep)") from None
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise _reject(db, user, at, 422, f"The signed bundle is not a feed bundle ({where}: {first.get('msg')})") \
            from None
    generated = bundle.generated_at if bundle.generated_at.tzinfo else bundle.generated_at.replace(tzinfo=UTC)
    if generated > at + MAX_FUTURE:
        # Accepted, it would refuse every honest bundle until that date as "older".
        raise _reject(db, user, at, 422, f"This bundle is dated {generated.isoformat()}, more than a day in the "
                                         f"future; check the clock of the machine that built it")
    last = db.get(ContentFeedSource, BUNDLE)
    last_generated = _dt((last.detail or {}).get("generated_at")) if last is not None and last.detail else None
    if last_generated is not None and generated < last_generated:
        raise _reject(db, user, at, 409, f"This bundle (made {generated.isoformat()}) is older than the one last "
                                         f"imported (made {last_generated.isoformat()}); nothing was imported")
    new, updated, removed = _store(db, bundle, at)
    kid = signing.key_id(key)
    sha = bundle_signing.sha256(doc)  # of what the signature covers; the bundle's own signed_sha256 is not signed
    _record(db, BUNDLE, at=at, ok=True, entries=len(bundle.entries),
            detail={"generated_at": generated.isoformat(), "key_id": kid, "signed_sha256": sha,
                    "imported_by": str(user.id)})
    _audit(db, user, "content_feeds.bundle_import",
           f"{len(bundle.entries)} entries ({new} new, {updated} updated, {removed} removed); made "
           f"{generated.isoformat()}; key {kid}; sha256 {sha}")
    return FeedImportOut(entries=len(bundle.entries), new=new, updated=updated, removed=removed,
                         generated_at=generated, key_id=kid)


def _store(db: Session, bundle: FeedBundleIn, at: datetime) -> tuple[int, int, int]:
    """The bundle's entries become the bundle-origin entries: new ones added, changed ones
    replaced (products too), and those the last bundle had and this one lacks removed."""
    wanted = {e.key: e for e in bundle.entries}
    existing: dict[str, ContentFeedEntry] = {}
    keys = sorted(wanted)
    for i in range(0, len(keys), 500):
        for row in db.query(ContentFeedEntry).filter(ContentFeedEntry.entry_key.in_(keys[i:i + 500])):
            existing[row.entry_key] = row
    new = updated = 0
    for key, e in wanted.items():
        row = existing.get(key)
        if row is None:
            db.add(ContentFeedEntry(id=uuid.uuid4(), entry_key=key, source=e.source, kind=e.kind,
                                    products=sorted(set(e.products)), at=e.at, title=e.title, data=e.data,
                                    origin=BUNDLE, first_seen_at=at, updated_at=at))
            new += 1
            continue
        products = sorted(set(e.products))
        at_old = row.at if row.at.tzinfo else row.at.replace(tzinfo=UTC)
        if (products != sorted(row.products or []) or at_old != e.at or row.title != e.title or row.data != e.data
                or row.kind != e.kind or row.origin != BUNDLE):
            row.products, row.at, row.title, row.data, row.kind = products, e.at, e.title, e.data, e.kind
            row.origin, row.updated_at = BUNDLE, at
            updated += 1
    removed = 0
    for row in db.query(ContentFeedEntry).filter(ContentFeedEntry.origin == BUNDLE).all():
        if row.entry_key not in wanted:
            db.delete(row)
            removed += 1
    db.flush()
    return new, updated, removed
