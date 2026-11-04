"""TrueNorth Range — AI triage suggestions for tickets (issue #381).

==================================================  ==========================
Endpoint                                            Permission(s)
==================================================  ==========================
GET    /tickets/ai-triage/status                    TICKET_WORK
POST   /tickets/{ticket_id}/triage                  TICKET_WORK
==================================================  ==========================

Suggest-only: nothing here changes a ticket. See app/ticket_triage.py for what is sent to the
AI backend (no names, no internal notes) and how the answer is checked. Registered before the
tickets router so ``/tickets/ai-triage/status`` is not read as a ticket id.
"""

from __future__ import annotations

import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import ticket_triage
from ..auth import CurrentUser
from ..db import get_db
from ..rbac import Permission, require_permission
from . import tickets

logger = logging.getLogger("truenorth.api.ticket_triage")

router = APIRouter(prefix="/tickets", tags=["tickets"])


class TriageStatusOut(BaseModel):
    available: bool


class TriageRelatedPage(BaseModel):
    page_id: uuid.UUID
    space_slug: str
    title: str


class TriageOut(BaseModel):
    category: str | None = None
    priority: str | None = None
    labels: list[str] = []
    summary: str = ""
    reply_hint: str = ""
    related_pages: list[TriageRelatedPage] = []
    model: str = ""
    backend: str = ""


@router.get("/ai-triage/status", response_model=TriageStatusOut)
def triage_status(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_WORK)),
) -> TriageStatusOut:
    return TriageStatusOut(available=ticket_triage.available(db))


@router.post("/{ticket_id}/triage", response_model=TriageOut)
def triage_ticket(
    ticket_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TICKET_WORK)),
) -> TriageOut:
    """A suggested category, priority, labels, summary and reply hint. Changes nothing."""
    t = tickets._ticket(db, ticket_id, user)
    try:
        suggestion, model, backend = ticket_triage.suggest(db, t)
    except ticket_triage.UnavailableError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ticket_triage.UnusableError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("AI backend unreachable for ticket triage: %s", exc)
        raise HTTPException(502, "The AI backend could not be reached.") from exc
    # Who asked about which ticket; never the text or the answer.
    tickets._audit(db, user, "ticket_ai_triage", str(t.id))
    db.commit()
    return TriageOut(
        category=suggestion.category,
        priority=suggestion.priority,
        labels=suggestion.labels,
        summary=suggestion.summary,
        reply_hint=suggestion.reply_hint,
        related_pages=[
            TriageRelatedPage(page_id=p.id, space_slug=s.slug, title=p.title)
            for p, s in ticket_triage.related_pages(db, t)
        ],
        model=model,
        backend=backend,
    )
