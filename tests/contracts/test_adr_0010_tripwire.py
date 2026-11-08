"""ADR 0010 tripwire: red the day the code drifts from docs/adr/0010-delete-policy.md.

One test per DECISION the ADR states (its "Decision" section, not the context or the
rejected options). Each docstring quotes the line it pins. These are contract checks,
not the behavioural suite: the handlers themselves are covered with a live in-process
API in ``tests/api/test_delete_conflicts.py`` and the NULL-on-delete policy in
``tests/api/test_delete_policy.py``; this module pins the same decisions from the
contract side so the suite fails even if those tests are renamed, moved or gutted.

Read-only over source and in-process objects: no live services, no network, nothing
mocked (the sessions below are real SQLite engines; the sources are read with
``inspect.getsource``).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from app import course_content_ingest, delete_guard
from app import models as m
from app.routers import admin as admin_router
from app.routers import courses as courses_router
from app.routers import integrations as integrations_router
from app.scenario_runs import models as sr_models
from fastapi import HTTPException
from sqlalchemy import String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "control-plane/api/app"
ADR = (REPO / "docs/adr/0010-delete-policy.md").read_text()


class _Base(DeclarativeBase):
    pass


class _Widget(_Base):
    """A minimal real table so ``refuse_if``/``commit_delete`` run against a genuine
    engine, session and ``Query`` — no doubles of the unit under test."""

    __tablename__ = "adr0010_tripwire_widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)


def _db():
    eng = create_engine("sqlite://")
    _Widget.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


# -- Decision 1: "A delete that would orphan history is refused with 409, and the
#    message names what blocks it" ---------------------------------------------------


def test_refuse_if_answers_409_and_names_the_blocker():
    """ADR 0010 decision 1: "A delete that would orphan history is refused with 409, and
    the message names what blocks it"."""
    db = _db()
    # Nothing in the way: no refusal.
    assert delete_guard.refuse_if(db.query(_Widget), "{n} widget(s) in the way") is None
    db.add_all(_Widget(name=n) for n in ("a", "b", "c"))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        delete_guard.refuse_if(db.query(_Widget), "{n} widget(s) in the way")
    assert exc.value.status_code == 409
    assert exc.value.detail == "3 widget(s) in the way"  # {n} names the count


def test_course_delete_refuses_with_the_message_the_adr_documents():
    """ADR 0010 decision 1 quotes this handler as its example:
    ``refuse_if(query, "Course has {n} enrollment(s) and is kept as part of the
    Students' records")``. If the code and the ADR drift apart here, one of them is
    stale and both must be revisited together."""
    src = inspect.getsource(courses_router.delete_course)
    assert "Course has {n} enrollment(s) and is kept as part of the Students' records" in src


# -- Decision 2: "The path for a record that is no longer wanted is retire / archive,
#    not delete" ----------------------------------------------------------------------


def test_deleting_a_user_retires_the_row():
    """ADR 0010 decision 2: "users are soft-deleted (``SoftDeleteMixin``)". User subclasses
    the mixin the ADR names; its DELETE handler retires the row today by deactivating it
    (``is_active = False``), so the handler check accepts either documented mechanism —
    a faithful move to ``target.soft_delete()`` must not trip this wire, a hard delete must."""
    assert issubclass(m.User, m.SoftDeleteMixin)
    src = inspect.getsource(admin_router.delete_user)
    retires = ("is_active = False" in src
               or "soft_delete()" in src or "deleted_at" in src)
    assert retires, "the user DELETE handler no longer retires the row"
    assert "db.delete(" not in src, "the user DELETE handler deletes the row"


def test_a_placeholder_in_use_is_retired_not_deleted():
    """ADR 0010 decision 2: "a course is unpublished (authoring state) or, for a spine
    placeholder, marked ``retired`` and hidden from catalogues"."""
    src = inspect.getsource(course_content_ingest._remove_placeholder)
    retired = src.index('meta["retired"] = True')
    assert retired < src.index("db.delete("), "the retire branch no longer precedes the delete"


# -- Decision 3: "Pure dependents go with their parent, explicitly ... deleted by the
#    handler, in code, before the parent, and the handler's docstring says so" --------


def test_pure_dependents_are_deleted_in_code_before_the_parent():
    """ADR 0010 decision 3: "A child with no value of its own ... is deleted by the
    handler, in code, before the parent, and the handler's docstring says so"."""
    for handler, dependent, parent in (
        (integrations_router.deregister_platform, "LTINonce", "db.delete(p)"),
        (courses_router.delete_course, "ModuleContent", "db.delete(course)"),
    ):
        src = inspect.getsource(handler)
        in_code = re.search(rf"db\.query\({dependent}\)[\s\S]*?\.delete\(", src)
        assert in_code, f"{handler.__name__} no longer deletes {dependent} in code"
        assert in_code.start() < src.index(parent), (
            f"{handler.__name__} deletes {dependent} after the parent row"
        )
        assert "go with" in src, f"{handler.__name__} docstring no longer says the dependents go with the parent"


def test_the_documented_cascade_is_still_what_the_adr_names():
    """ADR 0010, "Related, and also to be revisited": "``inject_records`` (inject
    evidence) cascades from ``exercises`` and ``scenario_executions``
    (``ondelete=\"CASCADE\"``)". Those are the two FKs the ADR names; a third CASCADE in
    the module, or one of these removed, and the code has drifted from the ADR."""
    assert 'ForeignKey("exercises.id", ondelete="CASCADE")' in inspect.getsource(sr_models.InjectRecord)
    assert 'ForeignKey("scenario_executions.id", ondelete="CASCADE")' in inspect.getsource(sr_models.InjectRecord)
    src = (APP / "scenario_runs" / "models.py").read_text()
    # The third is the one the ADR does not name: exercise_runs.exercise_id, the run
    # lease of a live run — a pure dependent. A fourth CASCADE must land in the ADR too.
    assert src.count('ondelete="CASCADE"') == 3


# -- Decision 4: "No NULL-on-delete ... tests/api/test_delete_policy.py holds the list
#    of the sites that predate this ADR and fails on any other" -----------------------

SET_NULL = re.compile(r"""ondelete\s*=\s*["']SET NULL["']""", re.I)
BULK_NULL = re.compile(r"\.update\(\s*\{[^{}]*:\s*None\s*\}")

# docs/adr/0010-delete-policy.md, "Exceptions that predate this ADR".
ADR_NULL_EXCEPTIONS = {
    ("scenario_runs/models.py", "set-null"): 2,  # ScenarioExecution.scenario_id, range_id
    ("scheduler/service.py", "bulk-null"): 1,  # detach(): finished events lose the link
    ("routers/courses.py", "bulk-null"): 1,  # delete_course: quizzes.module_id
}


def test_no_new_null_on_delete_outside_the_adr_exceptions():
    """ADR 0010 decision 4: "No NULL-on-delete. No new foreign key uses
    ``ondelete=\"SET NULL\"``, and no handler nulls a reference to let a delete through"."""
    found: dict[tuple[str, str], int] = {}
    for path in APP.rglob("*.py"):
        text = path.read_text()
        rel = path.relative_to(APP).as_posix()
        for kind, pattern in (("set-null", SET_NULL), ("bulk-null", BULK_NULL)):
            if n := len(pattern.findall(text)):
                found[(rel, kind)] = n
    assert found == ADR_NULL_EXCEPTIONS, (
        "A reference is nulled outside ADR 0010's exceptions: refuse the delete with 409 "
        "(delete_guard.refuse_if) and give the record a retire/archive state, or move the "
        "site into the ADR's exception table first."
    )
    for rel, _kind in ADR_NULL_EXCEPTIONS:
        assert rel in ADR, f"{rel} is grandfathered in code but no longer named in the ADR"


# -- Decision 5: "``commit_delete`` is the backstop ... rolls back and answers 409
#    instead of letting the ``IntegrityError`` escape as a 500" ------------------------


def test_commit_delete_is_the_backstop_409_not_500():
    """ADR 0010 decision 5: "``commit_delete`` is the backstop: a reference the handler
    does not know about yet (a table added later) rolls back and answers 409 instead of
    letting the ``IntegrityError`` escape as a 500"."""
    db = _db()
    db.add(_Widget(name="dupe"))
    db.commit()
    # A clean commit is not an error and refuses nothing.
    assert delete_guard.commit_delete(db, "Widget") is None
    # The database refuses the second "dupe": the backstop must answer 409, not raise.
    db.add(_Widget(name="dupe"))
    with pytest.raises(HTTPException) as exc:
        delete_guard.commit_delete(db, "Widget")
    assert exc.value.status_code == 409
    assert "still referenced" in exc.value.detail
    # The refusal rolled back: the session is usable and the duplicate never landed.
    db.add(_Widget(name="kept"))
    db.commit()
    assert db.query(_Widget).filter_by(name="dupe").count() == 1
    assert db.query(_Widget).filter_by(name="kept").count() == 1
