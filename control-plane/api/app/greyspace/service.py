"""Attach, detach and describe a range's Greyspace block (ADR 0007).

* **Tenant-scoped.** Every lookup goes through the caller's tenant; a foreign range is a
  404, never a 403. A student's lab-session range reads as not found to people without
  infrastructure rights, and is never changed from here (its session owns it).
* **The block is validated against its corpus.** When the control plane can read the
  tier's manifest (T0 always; others with ``GREYSPACE_MANIFEST_DIR``), a block that
  could not render (unknown site pack, a prefix that does not cover the corpus) is
  refused with 422 and the reasons. A tier whose manifest only the Greyspace host can
  read is accepted and its problems are reported once it can be checked.
* **Not while the range is changing.** Attach and detach are refused with 409 while a
  provision, destroy, stop or start is in flight. On a range that is already built the
  change takes effect at its next provision (the worker deploys it then).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .. import safe_yaml
from ..auth import CurrentUser
from ..models import AuditLog, Range, RangeState
from ..rbac import Permission, user_has_permission
from ..tenancy import tenant_uuid
from . import config, fixture
from .manifest import TIERS, Manifest, ManifestError, parse_manifest
from .models import RangeGreyspace
from .schemas import CorpusSummary, GreyspaceBlock, GreyspaceConfigOut, GreyspaceStatusOut

logger = logging.getLogger("truenorth.greyspace")

BUSY_STATES = (RangeState.provisioning, RangeState.destroying, RangeState.stopping, RangeState.starting)
BUILT_STATES = (RangeState.ready, RangeState.running, RangeState.stopped)


# ── Corpus ──────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _t0_manifest() -> Manifest:
    return parse_manifest(fixture.t0_manifest_doc())


def load_manifest(tier: str) -> Manifest | None:
    """The tier's manifest if the control plane can read it: ``$GREYSPACE_MANIFEST_DIR/<tier>/
    manifest.json`` when present, else the built-in T0 fixture for t0, else None."""
    root = os.getenv("GREYSPACE_MANIFEST_DIR", "").strip()
    if root:
        path = Path(root) / tier / "manifest.json"
        if path.is_file():
            try:
                return parse_manifest(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, ManifestError) as exc:
                logger.warning("greyspace: %s is not a usable manifest: %s", path, exc)
                return None
    return _t0_manifest() if tier == "t0" else None


def corpus_summary(tier: str) -> CorpusSummary:
    t = TIERS[tier]
    m = load_manifest(tier)
    return CorpusSummary(
        tier=t.name,
        title=t.title,
        cap_bytes=t.cap_bytes,
        location=t.location,
        builder=t.builder,
        description=t.description,
        available=m is not None,
        version=m.version if m else None,
        sites=len(m.sites) if m else None,
        bytes=m.total_bytes if m else None,
        categories=m.categories if m else None,
        threat_domains=len(m.threat_domains) if m else None,
    )


def list_corpora() -> list[CorpusSummary]:
    return [corpus_summary(tier) for tier in TIERS]


def params_of(block: GreyspaceBlock) -> config.BlockParams:
    return config.BlockParams(
        corpus_tier=block.corpus_tier,
        site_packs=tuple(block.site_packs) if block.site_packs is not None else None,
        public_prefix=block.public_prefix,
        npc_profile=block.npc_profile,
        threat_infra=block.threat_infra,
        trust_ca=block.trust_ca,
    )


def problems(block: GreyspaceBlock) -> list[str]:
    """Why the block cannot render on its corpus; empty when it can, or cannot be checked here."""
    manifest = load_manifest(block.corpus_tier)
    return config.validate(manifest, params_of(block)) if manifest else []


# ── Ranges ──────────────────────────────────────────────────────────────────
def _tenant_range(db: Session, range_id: uuid.UUID, user: CurrentUser) -> Range:
    rng = (
        db.query(Range)
        .filter(Range.id == range_id, Range.tenant_id == tenant_uuid(user), Range.deleted_at.is_(None))
        .first()
    )
    if not rng:
        raise HTTPException(404, "Range not found")
    return rng


def _lab_range(db: Session, rng: Range) -> bool:
    from ..lab_sessions.service import lab_range_ids

    return rng.id in lab_range_ids(db, [rng.id])


def readable_range(db: Session, range_id: uuid.UUID, user: CurrentUser) -> Range:
    rng = _tenant_range(db, range_id, user)
    if not user_has_permission(user, Permission.INFRA_READ) and _lab_range(db, rng):
        raise HTTPException(404, "Range not found")
    return rng


def changeable_range(db: Session, range_id: uuid.UUID, user: CurrentUser) -> Range:
    rng = readable_range(db, range_id, user)
    if _lab_range(db, rng):
        raise HTTPException(409, "This range belongs to a student's lab session; manage it from the lab session")
    if RangeState(rng.state) in BUSY_STATES:
        raise HTTPException(409, f"The range is {RangeState(rng.state).value}; try again when it settles")
    return rng


def template_block(rng: Range) -> GreyspaceBlock | None:
    """The ``greyspace:`` block the range's template declares, if it declares a valid one."""
    raw = getattr(rng.template, "yaml", None) if rng.template is not None else None
    if not raw:
        return None
    try:
        doc = safe_yaml.load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("greyspace"), dict):
        return None
    try:
        return GreyspaceBlock.model_validate(doc["greyspace"])
    except ValidationError:
        return None


def _row(db: Session, rng: Range) -> RangeGreyspace | None:
    # tenant-safe: rng was resolved through _tenant_range(); the row carries the same tenant.
    return db.query(RangeGreyspace).filter(RangeGreyspace.range_id == rng.id).first()


def status(db: Session, rng: Range) -> GreyspaceStatusOut:
    row = _row(db, rng)
    block = GreyspaceBlock.model_validate(row.block) if row else None
    return GreyspaceStatusOut(
        range_id=rng.id,
        attached=row is not None,
        status=row.status if row else "not_attached",
        range_state=RangeState(rng.state).value,
        block=block,
        template_block=template_block(rng),
        corpus=corpus_summary(block.corpus_tier) if block else None,
        detail=row.detail if row else None,
        problems=problems(block) if block else [],
        deployed_at=row.deployed_at if row else None,
        updated_at=row.updated_at if row else None,
    )


def _audit(db: Session, user: CurrentUser, action: str, rng: Range, detail: str) -> None:
    try:
        user_id = uuid.UUID(str(user.id))
    except ValueError:
        user_id = None
    db.add(
        AuditLog(
            user_id=user_id,
            tenant_id=rng.tenant_id,
            action=action,
            resource_type="range_greyspace",
            resource_id=str(rng.id),
            detail=detail,
        )
    )


def attach(db: Session, rng: Range, block: GreyspaceBlock | None, user: CurrentUser) -> GreyspaceStatusOut:
    """Attach (or replace) the range's block. No block: the template's, else the defaults."""
    block = block or template_block(rng) or GreyspaceBlock()
    found = problems(block)
    if found:
        raise HTTPException(422, {"message": "This Greyspace block cannot run on its corpus", "problems": found})
    row = _row(db, rng)
    note = {"note": "Takes effect at the range's next provision."} if RangeState(rng.state) in BUILT_STATES else None
    if row is None:
        try:
            attached_by = uuid.UUID(str(user.id))
        except ValueError:
            attached_by = None
        row = RangeGreyspace(range_id=rng.id, tenant_id=rng.tenant_id, attached_by=attached_by)
        db.add(row)
    row.block = block.model_dump(mode="json")
    row.corpus_tier = block.corpus_tier
    row.status = "configured"
    row.detail = note
    row.deployed_at = None
    _audit(db, user, "greyspace.attach", rng, json.dumps(row.block, sort_keys=True))
    db.commit()
    db.refresh(rng)
    return status(db, rng)


def detach(db: Session, rng: Range, user: CurrentUser) -> None:
    row = _row(db, rng)
    if row is None:
        raise HTTPException(404, "No Greyspace block is attached to this range")
    db.delete(row)
    _audit(db, user, "greyspace.detach", rng, "")
    db.commit()


def rendered_config(db: Session, rng: Range) -> GreyspaceConfigOut:
    row = _row(db, rng)
    if row is None:
        raise HTTPException(404, "No Greyspace block is attached to this range")
    block = GreyspaceBlock.model_validate(row.block)
    manifest = load_manifest(block.corpus_tier)
    if manifest is None:
        raise HTTPException(
            409, f"The {block.corpus_tier} corpus manifest is not available to the control plane (GREYSPACE_MANIFEST_DIR)"
        )
    try:
        out = config.render(manifest, params_of(block))
    except config.ConfigError as exc:
        raise HTTPException(409, f"This Greyspace block cannot render on its corpus now: {exc}") from exc
    s = out.summary
    return GreyspaceConfigOut(
        range_id=rng.id,
        corpus_tier=block.corpus_tier,
        corpus_version=manifest.version,
        address_plan=s["address_plan"],
        isps=s["isps"],
        services=s["services"],
        sites=s["sites"],
        site_packs=s["site_packs"],
        tlds=s["tlds"],
        zones=s["zones"],
        threat_domains=s["threat_domains"],
        files=s["files"],
    )
