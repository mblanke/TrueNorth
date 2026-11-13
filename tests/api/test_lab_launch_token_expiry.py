"""The token a Moodle "Start lab" hands the lab page (audit #443, integrations.py:948).

Only a lab that is already ending or ended gets a short grace token so the page can load
and show the ended state. A live lab keeps its real expiry, so a token never outlives it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from app.lab_sessions import service, tokens

# Fixtures are imported so pytest registers them here; the tests fetch them by name.
from test_lab_sessions import act, lab, launch, student, until, worker  # noqa: F401


def _exp(target: str) -> int:
    token = target.rpartition("#token=")[2]
    return jwt.decode(token, options={"verify_signature": False})["exp"]


def test_a_live_lab_keeps_its_real_expiry_in_the_launch_token(request, db_session):
    from app.routers.integrations import _launch_lab

    _ = request.getfixturevalue("worker")
    _, rid, course_id = request.getfixturevalue("lab")
    s = student(db_session)
    a, _ = launch(db_session, s, rid)
    until(db_session, a, "ready")
    a.max_expires_at = datetime.now(UTC) + timedelta(seconds=10)
    db_session.commit()

    target = _launch_lab(db_session, s, f"{course_id}:mod_006")
    assert _exp(target) <= int(datetime.now(UTC).timestamp()) + 10 + 1  # not stretched to the grace


def test_a_live_lab_already_past_its_maximum_gets_no_usable_token(request, db_session):
    """Not yet swept to ending: the token must not outlive the lab either."""
    from app.routers.integrations import _launch_lab

    _ = request.getfixturevalue("worker")
    _, rid, course_id = request.getfixturevalue("lab")
    s = student(db_session)
    a, _ = launch(db_session, s, rid)
    until(db_session, a, "ready")
    a.max_expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()
    assert a.state not in service.ENDING

    target = _launch_lab(db_session, s, f"{course_id}:mod_006")
    assert _exp(target) <= int(datetime.now(UTC).timestamp())


def test_the_token_reads_a_naive_database_datetime_as_utc(db_session):
    """The database hands back naive UTC; it must not be read as the host's local time."""
    import uuid

    naive = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)
    token = tokens.mint(db_session, uuid.uuid4(), uuid.uuid4(), naive)
    exp = jwt.decode(token, options={"verify_signature": False})["exp"]
    assert abs(exp - int((datetime.now(UTC) + timedelta(hours=1)).timestamp())) <= 2
