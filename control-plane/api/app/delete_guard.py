"""Deleting a parent row that other rows still reference: 409, never 500.

PostgreSQL refuses to delete a row a foreign key still points at, and SQLite (the unit
test engine) does not enforce foreign keys unless asked, so a DELETE handler that just
calls ``db.delete(parent); db.commit()`` passes its tests and returns an unhandled 500
in production the first time the parent is in use.

A handler decides, per child table, between:

* refusing with 409 and a message naming what blocks the delete (the default for data
  with history or independent value: ranges built from a template, Student progress);
* deleting the children itself, explicitly and before the parent, when they are pure
  dependents with no value of their own (an LTI nonce of a deregistered platform).

``refuse_if`` does the first; ``commit_delete`` is the backstop for a reference the
handler does not know about yet (a table added later): it rolls back and answers 409
instead of letting the IntegrityError escape.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

logger = logging.getLogger("truenorth.api.delete_guard")


def refuse_if(query: Query, detail: str) -> None:
    """409 with ``detail`` (``{n}`` is replaced by the count) when ``query`` matches rows."""
    if n := query.count():
        raise HTTPException(409, detail.format(n=n))


def commit_delete(db: Session, what: str) -> None:
    """Commit a delete; a foreign-key refusal becomes 409 naming ``what``."""
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.exception("Delete of %s refused by the database: a reference the handler does not handle", what)
        raise HTTPException(409, f"{what} is still referenced by other records and cannot be deleted") from None
