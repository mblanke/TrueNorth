"""Open, append to and read Range Certification Records (ADR 0013 §6, issue #244).

**Hook points for the other slices.** Every write goes through ``append``; nothing here
updates or deletes (models.py says how that is enforced). A slice adds its evidence with
one call, in its own transaction, for the deployment it worked on:

* ``record_for_deployment(db, tenant_id, deployment_id)`` -> the record of one provision
  (the ``range_operations`` id the build ran under), or ``latest_record(db, tenant_id,
  range_id)`` -> the range's newest record;
* ``append(db, record, section, status, summary, detail, source=..., ...)`` with
  ``section`` one of sections.SECTIONS and ``status`` one of sections.STATUSES:

  =====================  =========================  ===================================
  slice                  section                    filled
  =====================  =========================  ===================================
  #232, S1, S2, S3       ``guest_readiness``,       **now**, from what those slices
                         ``network_allocation``,    record (``build_evidence``,
                         ``template_provenance``,   evidence.py): provisioner_output
                         ``credentials``            ``vms[]`` (``template_build``,
                                                    ``template_login``), network_
                                                    reservations (external ones too),
                                                    golden_image_builds, range_
                                                    credentials
  S4 (#242)              ``isolation``              by S4 calling ``append``: pass when
                                                    every zone is probed and every non-
                                                    exception check proven_unreachable
  S5 (#243)              ``drift``, ``teardown``    by S5 (teardown has a basic entry
                                                    now: the destroy's outcome)
  S7, any force          ``overrides`` via          (info) who, what, why
                         ``record_override``
  =====================  =========================  ===================================

  The worker cannot import the API (ADR 0003): a worker-side slice writes the same row
  shape through its table mirror, or reports to the API, which calls ``append``.

* ``append_correction(db, record, corrects_seq, ..., user, reason)``: a person corrects an
  entry. The correction is a new entry; the old one stays.

Wired now: ``open_for_provision`` (range_ops.accept: the ranges router, batch provisioning,
exercises and the scheduler; **not** Student lab sessions, which send their own builds
(lab_sessions/service.py) and so get no record yet: a follow-up), ``on_operation_settled``
(range_ops.reconcile: a provision's build evidence; a destroy's outcome),
``refresh_if_changed`` (on read: evidence that changed since, such as a check the worker
settled later),
``record_override`` (operation abandon, lease force-release, range abandon).

Retention (owner decision 2026-10-21): one record per build; a range's newest record is
current, earlier ones are archived and purged after RANGE_CERT_RETENTION_MONTHS by
retention.py, the only delete path.

Not enforced yet: a booking's ``require_certified_range`` (scheduler) is stored; the
scheduler calls ``booking_gate`` once S4 can produce a certified range.
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import AuditLog, Range, Template
from ..rbac import Permission, user_has_permission
from . import evidence, signing
from . import sections as sec
from .models import RangeCertificationEntry, RangeCertificationRecord, SigningKey

logger = logging.getLogger("truenorth.range_certification")

SYSTEM = "system"
OVERRIDE_KINDS = ("approval", "override", "force")
PLAN_BASIS = "template_yaml_sha256"  # the worker renders the topology from this (worker/render.py)
# A record opened after its build (POST .../capture on a range built before records began):
# the template may have changed since, so the hash is of the template as it was at capture.
LATE_PLAN_BASIS = "template_yaml_sha256_at_capture"


class CertificationError(ValueError):
    """An entry this record will not take (unknown section, missing identity or reason)."""


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def actor_of(user: CurrentUser | None) -> tuple[uuid.UUID | None, str]:
    if user is None:
        return None, SYSTEM
    return _uuid(user.id), (user.display_name or user.email or str(user.id))[:255]


def plan_hash(template: Template | None) -> str:
    """SHA-256 of the template YAML the build renders. No rendered plan is stored anywhere
    yet (the worker renders it in memory, tasks.provision_range); a slice that records the
    rendered topology appends its hash as evidence."""
    if template is None or not template.yaml:
        return ""  # nothing to hash; never the hash of an empty string posing as a plan
    return hashlib.sha256(template.yaml.encode("utf-8")).hexdigest()


def _sha(text: str | None) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def build_output(rng: Range, record: RangeCertificationRecord) -> dict:
    """The range's provisioner_output if this record's build wrote it, else {}. A build that
    failed before writing leaves the previous build's output in place; that output is still
    exactly what was there when this build was accepted (``prior_output_sha256``), and its
    VMs are not this deployment's evidence."""
    raw = rng.provisioner_output
    if not raw or (record.prior_output_sha256 and _sha(raw) == record.prior_output_sha256):
        return {}
    return evidence.output_of(raw)


# ── Records ───────────────────────────────────────────────────────────────────
def open_for_provision(
    db: Session, rng: Range, op, user: CurrentUser | None, *, late: bool = False,
) -> RangeCertificationRecord:
    """The record for the provision ``op`` (a RangeOperation), opened when it is accepted, in
    the same transaction. Idempotent per operation. Does not commit."""
    existing = record_for_deployment(db, rng.tenant_id, op.id)
    if existing is not None:
        return existing
    # tenant-safe: the range's own template, which range creation checked the tenant may use
    # (its own or a public one); the range itself is the caller's, already tenant-scoped.
    tmpl = db.get(Template, rng.template_id) if rng.template_id else None
    actor_id, actor = actor_of(user)
    rec = RangeCertificationRecord(
        id=uuid.uuid4(), deployment_id=op.id, generation=op.generation, tenant_id=rng.tenant_id, range_id=rng.id,
        range_name=rng.name or "", template_id=tmpl.id if tmpl else None, template_name=(tmpl.name if tmpl else "")[:255],
        template_version=(tmpl.version if tmpl else "") or "", plan_hash=plan_hash(tmpl),
        plan_hash_basis=LATE_PLAN_BASIS if late else PLAN_BASIS,
        # Late (a build accepted before records began): what came before is unknown.
        prior_output_sha256="" if late else _sha(rng.provisioner_output),
        opened_by_id=actor_id, opened_by=actor)
    db.add(rec)
    db.flush()
    return rec


def open_for_latest_build(db: Session, rng: Range, user: CurrentUser) -> RangeCertificationRecord:
    """The record of the range's newest provision, opened now if that provision was accepted
    before records began (its ``opened_at`` then postdates the build). 409: never built."""
    from ..range_ops import RangeOperation

    op = (db.query(RangeOperation)
          .filter(RangeOperation.range_id == rng.id, RangeOperation.tenant_id == rng.tenant_id,
                  RangeOperation.action == "provision")
          .order_by(RangeOperation.generation.desc()).first())
    if op is None:
        raise HTTPException(409, "This range has not been provisioned through TrueNorth; there is nothing to certify")
    return open_for_provision(db, rng, op, user, late=True)


def record_for_deployment(db: Session, tenant_id, deployment_id) -> RangeCertificationRecord | None:
    return (db.query(RangeCertificationRecord)
            .filter(RangeCertificationRecord.tenant_id == tenant_id,
                    RangeCertificationRecord.deployment_id == deployment_id)
            .first())


def records_of(db: Session, tenant_id, range_id, limit: int = 50) -> list[RangeCertificationRecord]:
    return (db.query(RangeCertificationRecord)
            .filter(RangeCertificationRecord.tenant_id == tenant_id, RangeCertificationRecord.range_id == range_id)
            .order_by(RangeCertificationRecord.generation.desc())
            .limit(limit).all())


def latest_record(db: Session, tenant_id, range_id) -> RangeCertificationRecord | None:
    found = records_of(db, tenant_id, range_id, limit=1)
    return found[0] if found else None


def entries_of(db: Session, record: RangeCertificationRecord) -> list[RangeCertificationEntry]:
    return (db.query(RangeCertificationEntry)
            .filter(RangeCertificationEntry.record_id == record.id, RangeCertificationEntry.tenant_id == record.tenant_id)
            .order_by(RangeCertificationEntry.seq).all())


# ── The one write path ────────────────────────────────────────────────────────
def append(
    db: Session, record: RangeCertificationRecord, section: str, status: str, summary: str, detail: dict | None = None,
    *, source: str, user: CurrentUser | None = None, reason: str | None = None, corrects_seq: int | None = None,
    skip_if_unchanged: bool = False,
) -> RangeCertificationEntry | None:
    """Add one entry to ``record``. Does not commit. ``skip_if_unchanged``: return None
    instead when the section's latest entry already says exactly this (repeat captures)."""
    if section not in sec.BY_NAME:
        raise CertificationError(f"unknown section {section!r}")
    if status not in sec.STATUSES or (status == sec.INFO) != (section in sec.INFO_ONLY):
        raise CertificationError(f"status {status!r} is not allowed in section {section!r}")
    actor_id, actor = actor_of(user)
    reason = (reason or "").strip() or None
    if section in sec.INFO_ONLY and user is None:
        raise CertificationError("an override needs the person behind it")
    if corrects_seq is not None and (user is None or reason is None):
        raise CertificationError("a correction needs the person behind it and a reason")
    detail = dict(detail or {})
    # Serialise appends to one record (PostgreSQL row lock; a SELECT, which the triggers allow).
    db.query(RangeCertificationRecord.id).filter(RangeCertificationRecord.id == record.id).with_for_update().first()
    current = entries_of(db, record)
    if corrects_seq is not None and not any(e.seq == corrects_seq and e.section == section for e in current):
        raise CertificationError(f"entry {corrects_seq} is not a {section} entry of this record")
    top = sec.latest(current, section)
    if skip_if_unchanged and top is not None and (top.status, top.summary, top.detail) == (status, summary[:500], detail):
        return None
    entry = RangeCertificationEntry(
        id=uuid.uuid4(), record_id=record.id, tenant_id=record.tenant_id,
        seq=(max((e.seq for e in current), default=0) + 1), section=section, status=status, summary=summary[:500],
        detail=detail, source=source[:64], actor_id=actor_id, actor=actor, reason=reason, corrects_seq=corrects_seq)
    db.add(entry)
    db.flush()
    return entry


def append_correction(
    db: Session, record: RangeCertificationRecord, corrects_seq: int, status: str, summary: str, detail: dict | None,
    *, user: CurrentUser, reason: str,
) -> RangeCertificationEntry:
    """A person's correction of entry ``corrects_seq``: a new entry in the same section that
    becomes its state. The corrected entry is kept."""
    target = next((e for e in entries_of(db, record) if e.seq == corrects_seq), None)
    if target is None:
        raise CertificationError(f"entry {corrects_seq} is not in this record")
    entry = append(db, record, target.section, status, summary, detail, source="correction", user=user,
                   reason=reason, corrects_seq=corrects_seq)
    assert entry is not None
    return entry


def record_override(
    db: Session, rng: Range, kind: str, action: str, user: CurrentUser, reason: str | None,
    detail: dict | None = None,
) -> RangeCertificationEntry | None:
    """An approval, override or force on a range, in its newest record (None: no record, or
    it could not be written). ``reason`` None: the action asks for none (said in the
    detail, never made up). In a savepoint, and never raises: recording an emergency
    action must never block it (ADR 0013 §7)."""
    if kind not in OVERRIDE_KINDS:
        raise CertificationError(f"override kind must be one of {OVERRIDE_KINDS}")
    info = {"kind": kind, "action": action, **(detail or {})}
    if not (reason or "").strip():
        info["reason_not_requested"] = True
    try:
        with db.begin_nested():
            record = latest_record(db, rng.tenant_id, rng.id)
            if record is None:
                return None
            return append(db, record, "overrides", sec.INFO, f"{kind}: {action}", info,
                          source=f"api.{action}"[:64], user=user, reason=reason)
    except Exception as exc:  # noqa: BLE001
        logger.error("range %s: override %s not recorded in its certification record (%s)", rng.id, action, exc)
        return None


# ── Evidence TrueNorth holds now ──────────────────────────────────────────────
def build_evidence(db: Session, rng: Range, record: RangeCertificationRecord,
                   build_error: str | None = None) -> dict[str, tuple[str, str, dict]]:
    """{section: (status, summary, detail)} for the build's evidence: readiness (#232),
    network allocation (S1), template provenance (S2) and credential state (S3). Reads only."""
    from ..network_inventory import NetworkReservation
    from ..range_credentials import RangeCredential

    output = build_output(rng, record)
    held = (db.query(NetworkReservation)
            .filter(NetworkReservation.range_id == rng.id, NetworkReservation.tenant_id == rng.tenant_id).all())
    domains = {r.domain for r in held if r.kind == "vlan"}
    # Values used outside TrueNorth (S1): platform-wide rows with no range and no tenant.
    external = (db.query(NetworkReservation)
                .filter(NetworkReservation.range_id.is_(None), NetworkReservation.kind == "vlan",
                        NetworkReservation.domain.in_(domains)).all()) if domains else []
    accounts = (db.query(RangeCredential)
                .filter(RangeCredential.range_id == rng.id, RangeCredential.tenant_id == rng.tenant_id).all())
    return {"guest_readiness": evidence.guest_readiness(output, build_error),
            "network_allocation": evidence.network_allocation(output, held, external, build_error),
            "template_provenance": evidence.template_provenance(output, golden_builds(db, rng, output)),
            "credentials": evidence.credentials(output, accounts)}


def golden_builds(db: Session, rng: Range, output: dict) -> dict[str, dict[str, dict]]:
    """The registered builds (S2, golden_image_builds) of every image the build's VMs name:
    catalogue_id -> build_id -> {checksum, promoted}. Golden images are platform-wide, one
    per (catalogue id, hypervisor), as the worker resolves them (db_ops.golden_image_pin_rows)."""
    from ..golden_image_builds import GoldenImageBuild
    from ..models import GoldenImage

    wanted = {str(v["template_build"].get("catalogue_id")) for v in output.get("vms") or []
              if isinstance(v, dict) and isinstance(v.get("template_build"), dict)
              and v["template_build"].get("catalogue_id")}
    if not wanted:
        return {}
    hypervisor = "proxmox" if "proxmox" in str(output.get("provider") or rng.provisioner_backend or "") else "vsphere"
    rows = (db.query(GoldenImage.catalogue_id, GoldenImageBuild.build_id, GoldenImageBuild.checksum,
                     GoldenImageBuild.promoted)
            .join(GoldenImageBuild, GoldenImageBuild.golden_image_id == GoldenImage.id)
            .filter(GoldenImage.catalogue_id.in_(wanted), GoldenImage.hypervisor == hypervisor,
                    GoldenImage.deleted_at.is_(None))
            .all())
    out: dict[str, dict[str, dict]] = {}
    for catalogue_id, build_id, checksum, promoted in rows:
        out.setdefault(catalogue_id, {})[build_id] = {"checksum": checksum, "promoted": bool(promoted)}
    return out


def _unchanged(entries: list, found: dict) -> bool:
    for name, (status, summary, detail) in found.items():
        top = sec.latest(entries, name)
        if top is None or (top.status, top.summary, top.detail) != (status, summary[:500], dict(detail)):
            return False
    return True


def capture_build(db: Session, rng: Range, record: RangeCertificationRecord, build_error: str | None = None) -> int:
    """Append the build's evidence where it changed. Returns how many entries were written.
    Does not commit."""
    found = build_evidence(db, rng, record, build_error)
    written = [append(db, record, name, *found[name], source="api.capture", skip_if_unchanged=True) for name in found]
    return sum(1 for e in written if e is not None)


def capture_teardown(db: Session, rng: Range, record: RangeCertificationRecord, destroyed: bool) -> None:
    from ..network_inventory import NetworkReservation

    held = db.query(NetworkReservation).filter(NetworkReservation.range_id == rng.id).all() if destroyed else []
    append(db, record, "teardown", *evidence.teardown(destroyed, rng.error_message, held), source="api.capture",
           skip_if_unchanged=True)


def on_operation_settled(db: Session, rng: Range, op, outcome: str) -> None:
    """range_ops.reconcile: an operation reached ``outcome``. A provision's evidence goes into
    its own record; a destroy's into the range's newest. In a savepoint, and never raises:
    a record problem must not stop the range's state from settling."""
    try:
        with db.begin_nested():
            if op.action == "provision":
                record = record_for_deployment(db, rng.tenant_id, op.id)
                if record is not None:
                    capture_build(db, rng, record, rng.error_message if outcome == "failed" else None)
            elif op.action == "destroy" and (record := latest_record(db, rng.tenant_id, rng.id)) is not None:
                capture_teardown(db, rng, record, destroyed=(outcome == "succeeded"))
    except Exception as exc:  # noqa: BLE001
        logger.error("range %s: certification evidence for operation %s not recorded (%s)", rng.id, op.id, exc)


def refresh_if_changed(db: Session, rng: Range) -> bool:
    """On read, cheaply: if the newest record's provision has settled, the range is not torn
    down and its build evidence now differs from what the record says (a check the worker
    settled later: setup_reconcile), append it. Reads only (no lock) when nothing changed,
    which is the usual case. In a savepoint; never raises. Returns whether it wrote."""
    from ..models import RangeState

    try:
        record = latest_record(db, rng.tenant_id, rng.id)
        if record is None or rng.state in (RangeState.destroying, RangeState.destroyed):
            return False
        op = _operation(db, record)
        if op is None or op.status not in ("succeeded", "failed") or op.generation != _last_generation(db, rng):
            return False
        error = rng.error_message if op.status == "failed" else None
        if _unchanged(entries_of(db, record), build_evidence(db, rng, record, error)):
            return False
        with db.begin_nested():
            return capture_build(db, rng, record, error) > 0
    except Exception as exc:  # noqa: BLE001
        logger.error("range %s: certification evidence not refreshed (%s)", rng.id, exc)
        return False


def has_in_flight(db: Session, rng: Range) -> bool:
    """Whether the range has a dispatched operation that reconcile could settle now."""
    from ..range_ops import RangeOperation

    return db.query(RangeOperation.id).filter(
        RangeOperation.range_id == rng.id, RangeOperation.tenant_id == rng.tenant_id,
        RangeOperation.status == "dispatched").first() is not None


def _last_generation(db: Session, rng: Range) -> int:
    from ..range_ops import RangeOperation

    return (db.query(func.max(RangeOperation.generation))
            .filter(RangeOperation.range_id == rng.id, RangeOperation.tenant_id == rng.tenant_id).scalar() or 0)


def deployment_status(db: Session, record: RangeCertificationRecord) -> str:
    """The provision's state: pending, dispatched (building), succeeded, failed, superseded,
    or unknown (its operation row is gone)."""

    op = _operation(db, record)
    return op.status if op is not None else "unknown"


def _operation(db: Session, record: RangeCertificationRecord):
    """The provision operation a record certifies, in the record's tenant (or None)."""
    from ..range_ops import RangeOperation

    return (db.query(RangeOperation)
            .filter(RangeOperation.id == record.deployment_id, RangeOperation.tenant_id == record.tenant_id)
            .first())


# ── Reading ───────────────────────────────────────────────────────────────────
def readable_range(db: Session, range_id: uuid.UUID, user: CurrentUser) -> Range:
    """The caller's range (soft-deleted ones too: the record outlives it), or 404: another
    tenant's, or a Student's lab to staff without infrastructure rights."""
    rng = db.query(Range).filter(Range.id == range_id, Range.tenant_id == _uuid(user.tenant_id)).first()
    if rng is None:
        raise HTTPException(404, "Range not found")
    if not user_has_permission(user, Permission.INFRA_READ):
        from ..lab_sessions.service import lab_range_ids

        if rng.id in lab_range_ids(db, [rng.id]):
            raise HTTPException(404, "Range not found")
    return rng


def chosen_record(db: Session, rng: Range, deployment_id: uuid.UUID | None) -> RangeCertificationRecord | None:
    if deployment_id is None:
        return latest_record(db, rng.tenant_id, rng.id)
    record = record_for_deployment(db, rng.tenant_id, deployment_id)
    if record is None or record.range_id != rng.id:
        raise HTTPException(404, "No certification record for that deployment of this range")
    return record


def view(db: Session, record: RangeCertificationRecord, *, history: bool = False, rows: list | None = None) -> dict:
    """The record as the API shows it: header, verdict, sections. Each section carries its
    latest entry only, and how many it has (``history_count``); ``history`` gives every
    entry, superseded ones included (exports always do)."""
    rows = entries_of(db, record) if rows is None else rows
    verdict, reasons = sec.verdict(rows)
    sections = []
    for s in sec.SECTIONS:
        top = sec.latest(rows, s.name)
        mine = [e for e in rows if e.section == s.name]
        sections.append({
            "name": s.name, "title": s.title, "required": s.required, "provided_by": s.provided_by,
            "status": sec.section_status(rows, s.name), "summary": top.summary if top else "",
            "updated_at": top.recorded_at if top else None, "history_count": len(mine),
            "entries": [_entry(e) for e in (mine if history else ([top] if top else []))],
        })
    return {
        "id": record.id, "deployment_id": record.deployment_id, "generation": record.generation,
        "range_id": record.range_id, "range_name": record.range_name, "template_id": record.template_id,
        "template_name": record.template_name, "template_version": record.template_version,
        "plan_hash": record.plan_hash, "plan_hash_basis": record.plan_hash_basis,
        "opened_by": record.opened_by, "opened_by_id": record.opened_by_id, "opened_at": record.opened_at,
        "tenant_id": record.tenant_id, "deployment_status": deployment_status(db, record),
        "verdict": verdict, "verdict_label": sec.VERDICT_LABELS[verdict], "reasons": reasons, "history": history,
        "sections": sections,
    }


def _entry(e: RangeCertificationEntry) -> dict:
    return {"seq": e.seq, "section": e.section, "status": e.status, "summary": e.summary, "detail": e.detail or {},
            "source": e.source, "actor": e.actor, "actor_id": e.actor_id, "reason": e.reason,
            "corrects_seq": e.corrects_seq, "recorded_at": e.recorded_at}


def archived(db: Session, rng: Range) -> list[dict]:
    """Every record of the range but its newest (current) one, newest first, with its
    verdict and when retention may purge it (retention.py; None: kept forever). The
    entries of all of them are read in one query."""
    from .retention import purge_after

    older = records_of(db, rng.tenant_id, rng.id)[1:]
    by_record: dict = {r.id: [] for r in older}
    if older:
        for e in (db.query(RangeCertificationEntry)
                  .filter(RangeCertificationEntry.record_id.in_(list(by_record)),
                          RangeCertificationEntry.tenant_id == rng.tenant_id)):
            by_record[e.record_id].append(e)
    out = []
    for r in older:
        verdict, _ = sec.verdict(by_record[r.id])
        out.append({"deployment_id": r.deployment_id, "generation": r.generation, "opened_at": r.opened_at,
                    "verdict": verdict, "verdict_label": sec.VERDICT_LABELS[verdict],
                    "purge_after": purge_after(r.opened_at)})
    return out


def record_document(db: Session, record: RangeCertificationRecord) -> tuple[dict, str]:
    """The record as every export carries it (full history, JSON-ready) and its
    ``record_sha256``: the same for every export of the record in this state, whoever made
    it and when (the purge audit row records it too)."""
    from .schemas import CertificationRecordOut

    doc = CertificationRecordOut.model_validate(view(db, record, history=True)).model_dump(mode="json")
    return doc, hashlib.sha256(signing.canonical(doc)).hexdigest()


# ── Signing keys (signing.py): published, and kept after rotation ─────────────
def sign_export(db: Session, payload: dict) -> dict:
    """``payload`` signed with the platform's current key, whose public key is recorded
    (once) so the export still verifies after TN_SECRETS_KEY rotates it out. Does not commit."""
    signed = signing.sign(payload)
    sig = signed["signature"]
    if db.get(SigningKey, sig["key_id"]) is None:  # tenant-safe: platform keys, not tenant data
        try:
            with db.begin_nested():
                db.add(SigningKey(key_id=sig["key_id"], alg=sig["alg"], public_key=sig["public_key"]))
        except IntegrityError:  # another request recorded it first
            pass
    return signed


def verify_export(db: Session, doc: dict) -> bool:
    """True when this platform signed exactly ``doc``: with a configured key, or one it
    recorded before a rotation."""
    if signing.verify(doc):
        return True
    known = {k.key_id: k.public_key for k in db.query(SigningKey).all()}
    key_id = (doc.get("signature") or {}).get("key_id") if isinstance(doc.get("signature"), dict) else None
    return key_id in known and signing.verify_with_public_key(doc, signing.public_key_from_b64(known[key_id]))


def signing_keys(db: Session) -> list[dict]:
    """Every key that has signed exports, newest first, the configured current one marked
    (and listed even before it has signed anything)."""
    current = signing.current_public()
    rows = {k.key_id: k for k in db.query(SigningKey).order_by(SigningKey.first_used_at.desc()).all()}
    out = [{"key_id": k.key_id, "alg": k.alg, "public_key": k.public_key, "first_used_at": k.first_used_at,
            "current": k.key_id == current["key_id"]} for k in rows.values()]
    if current["key_id"] not in rows:
        out.insert(0, {**current, "first_used_at": None, "current": True})
    return out


def audit(db: Session, user: CurrentUser, rng: Range, action: str, detail: str) -> None:
    db.add(AuditLog(user_id=_uuid(user.id), tenant_id=rng.tenant_id, action=action, resource_type="range",
                    resource_id=str(rng.id), detail=detail[:1000]))


def booking_gate(db: Session, tenant_id, range_id) -> tuple[bool, list[str]]:
    """Whether a booking that requires a certified range may use ``range_id``: its newest
    record is ``certified``. Not called yet (enforcement waits for S4; module docstring)."""
    record = latest_record(db, tenant_id, range_id)
    if record is None:
        return False, ["the range has no certification record (not built since certification records began)"]
    verdict, reasons = sec.verdict(entries_of(db, record))
    return verdict == sec.CERTIFIED, reasons
