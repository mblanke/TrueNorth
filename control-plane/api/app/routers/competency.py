"""TrueNorth Range - Competency framework & certification tracking router.

Maps NICE/ATT&CK competencies to exercise objectives, tracks user proficiency,
identifies skill gaps, and manages external certifications.
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import (
    Certification,
    Competency,
    CompetencyAssertion,
    CompetencyFramework,
    ProficiencyLevel,
)
from ..schemas import (
    CertificationIn,
    CertificationOut,
    CompetencyAssertionOut,
    CompetencyOut,
    CompetencyProfileOut,
    PaginatedResponse,
    SkillGapOut,
)

logger = logging.getLogger("truenorth.competency")

router = APIRouter(prefix="/competency", tags=["competency"])


# ══════════════════════════════════════════════════════════════════════════
# Competency Framework CRUD
# ══════════════════════════════════════════════════════════════════════════


@router.get("/frameworks", response_model=list[CompetencyOut])
def list_competencies(
    framework: str | None = Query(default=None, description="Filter by framework: nice, mitre_attack, custom"),
    category: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List competency definitions with optional filters."""
    q = db.query(Competency)
    if framework:
        q = q.filter(Competency.framework == framework)
    if category:
        q = q.filter(Competency.category == category)
    return q.order_by(Competency.code).offset(offset).limit(limit).all()


@router.post("/frameworks", response_model=CompetencyOut, status_code=status.HTTP_201_CREATED)
def create_competency(
    code: str,
    name: str,
    framework: str,
    description: str = "",
    category: str = "",
    level: str = "",
    parent_code: str | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a competency definition (admin/instructor)."""
    comp = Competency(
        code=code,
        name=name,
        description=description,
        framework=CompetencyFramework(framework),
        category=category,
        level=level,
        parent_code=parent_code,
    )
    db.add(comp)
    db.commit()
    db.refresh(comp)
    return comp


@router.post("/frameworks/import-nice", status_code=status.HTTP_201_CREATED)
def import_nice_framework(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Seed the NICE Workforce Framework (SP 800-181r1) core work roles and KSAs.

    This is idempotent — skips competencies that already exist.
    """
    nice_roles = [
        ("SP-RSK-001", "Authorizing Official", "Analyze", "Senior official with authority to formally assume responsibility for operating a system"),
        ("SP-RSK-002", "Security Control Assessor", "Analyze", "Conducts independent assessments of security controls and privacy controls"),
        ("AN-ASA-001", "All-Source Analyst", "Analyze", "Analyzes data from multiple sources to develop threat intelligence"),
        ("AN-TGT-001", "Target Developer", "Analyze", "Performs target system analysis and develops targeting solutions"),
        ("AN-TWA-001", "Threat/Warning Analyst", "Analyze", "Identifies and assesses cyber threats to inform decision makers"),
        ("CO-CLO-001", "Cloud Operations Specialist", "Collect and Operate", "Manages cloud infrastructure and services"),
        ("CO-OPL-001", "Cyber Operations Planner", "Collect and Operate", "Develops detailed plans for cyber operations"),
        ("IN-FOR-001", "Cyber Defense Forensics Analyst", "Investigate", "Analyzes digital evidence and investigates computer security incidents"),
        ("IN-INV-001", "Cyber Defense Incident Responder", "Investigate", "Investigates, analyzes, and responds to cyber incidents"),
        ("OV-MGT-001", "Information Systems Security Manager", "Oversee and Govern", "Manages information systems security program"),
        ("OV-TEL-001", "Cyber Instructional Curriculum Developer", "Oversee and Govern", "Develops cyber training curricula and instructional materials"),
        ("OV-TEL-002", "Cyber Instructor", "Oversee and Govern", "Delivers technical cyber training to personnel"),
        ("PR-CDA-001", "Cyber Defense Analyst", "Protect and Defend", "Analyzes events and trends to identify cyber defense mitigations"),
        ("PR-CIR-001", "Cyber Defense Incident Responder", "Protect and Defend", "Investigates and responds to cyber defense incidents"),
        ("PR-INF-001", "Cyber Defense Infrastructure Support", "Protect and Defend", "Tests, implements, and maintains infrastructure security"),
        ("PR-VAM-001", "Vulnerability Assessment Analyst", "Protect and Defend", "Performs vulnerability assessments and recommends mitigations"),
        ("SE-DEV-001", "Secure Software Assessor", "Securely Provision", "Analyzes security of software applications"),
        ("SE-ARC-001", "Security Architect", "Securely Provision", "Develops system security architecture and designs"),
    ]

    created = 0
    for code, name, category, description in nice_roles:
        existing = db.query(Competency).filter(
            Competency.framework == CompetencyFramework.nice,
            Competency.code == code,
        ).first()
        if not existing:
            comp = Competency(
                code=code,
                name=name,
                description=description,
                framework=CompetencyFramework.nice,
                category=category,
            )
            db.add(comp)
            created += 1

    db.commit()
    logger.info("Imported %d NICE work roles", created)
    return {"imported": created, "total_nice_roles": len(nice_roles)}


# ══════════════════════════════════════════════════════════════════════════
# User Competency Profile
# ══════════════════════════════════════════════════════════════════════════


@router.get("/users/{user_id}/profile", response_model=CompetencyProfileOut)
def get_competency_profile(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Get aggregated competency profile for a user."""
    assertions = (
        db.query(CompetencyAssertion)
        .filter(CompetencyAssertion.user_id == user_id)
        .all()
    )

    by_framework: dict[str, int] = {}
    by_proficiency: dict[str, int] = {}
    for a in assertions:
        comp = db.query(Competency).filter(Competency.id == a.competency_id).first()
        if comp:
            fw = comp.framework.value if hasattr(comp.framework, 'value') else str(comp.framework)
            by_framework[fw] = by_framework.get(fw, 0) + 1
        prof = a.proficiency.value if hasattr(a.proficiency, 'value') else str(a.proficiency)
        by_proficiency[prof] = by_proficiency.get(prof, 0) + 1

    assertion_outs = [CompetencyAssertionOut.model_validate(a) for a in assertions]
    return CompetencyProfileOut(
        user_id=user_id,
        assertions=assertion_outs,
        total_competencies=len(assertions),
        by_framework=by_framework,
        by_proficiency=by_proficiency,
    )


@router.get("/users/{user_id}/skill-gaps", response_model=list[SkillGapOut])
def get_skill_gaps(
    user_id: uuid.UUID,
    target_role: str = Query(..., description="NICE work role code, e.g. PR-CDA-001"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Identify competencies required by target role but not yet demonstrated."""
    # Get all competencies for the target role category
    target = db.query(Competency).filter(Competency.code == target_role).first()
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target role not found")

    role_competencies = (
        db.query(Competency)
        .filter(Competency.category == target.category)
        .all()
    )

    # Get user's existing assertions
    user_assertions = {
        a.competency_id: a
        for a in db.query(CompetencyAssertion)
        .filter(CompetencyAssertion.user_id == user_id)
        .all()
    }

    gaps = []
    for comp in role_competencies:
        assertion = user_assertions.get(comp.id)
        if not assertion:
            gaps.append(SkillGapOut(
                competency=CompetencyOut.model_validate(comp),
                required_level="intermediate",
                current_level=None,
                gap=True,
            ))
        else:
            prof = assertion.proficiency.value if hasattr(assertion.proficiency, 'value') else str(assertion.proficiency)
            if prof in ("novice", "beginner"):
                gaps.append(SkillGapOut(
                    competency=CompetencyOut.model_validate(comp),
                    required_level="intermediate",
                    current_level=prof,
                    gap=True,
                ))

    return gaps


@router.post("/users/{user_id}/assertions", response_model=CompetencyAssertionOut, status_code=status.HTTP_201_CREATED)
def create_assertion(
    user_id: uuid.UUID,
    competency_id: uuid.UUID,
    proficiency: str = "novice",
    source: str = "truenorth",
    evidence_refs: list[str] | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Record a competency assertion for a user."""
    assertion = CompetencyAssertion(
        user_id=user_id,
        competency_id=competency_id,
        proficiency=ProficiencyLevel(proficiency),
        source=source,
        evidence_refs=json.dumps(evidence_refs or []),
    )
    db.add(assertion)
    db.commit()
    db.refresh(assertion)
    return assertion


# ══════════════════════════════════════════════════════════════════════════
# Certifications
# ══════════════════════════════════════════════════════════════════════════

certs_router = APIRouter(prefix="/certifications", tags=["certifications"])


@certs_router.get("/users/{user_id}", response_model=list[CertificationOut])
def list_user_certifications(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List all certifications for a user."""
    return (
        db.query(Certification)
        .filter(Certification.user_id == user_id)
        .order_by(Certification.issued_at.desc())
        .all()
    )


@certs_router.post("/users/{user_id}", response_model=CertificationOut, status_code=status.HTTP_201_CREATED)
def add_certification(
    user_id: uuid.UUID,
    body: CertificationIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Add a certification record for a user."""
    cert = Certification(
        user_id=user_id,
        cert_name=body.cert_name,
        issuer=body.issuer,
        credential_id=body.credential_id,
        issued_at=body.issued_at,
        expires_at=body.expires_at,
        verification_url=body.verification_url,
        nice_work_roles=json.dumps(body.nice_work_roles),
        dod_8140_category=body.dod_8140_category,
    )
    db.add(cert)
    db.commit()
    db.refresh(cert)
    logger.info("Certification added: %s for user %s", body.cert_name, user_id)
    return cert