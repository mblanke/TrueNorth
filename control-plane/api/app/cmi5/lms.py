"""TrueNorth as a cmi5 LMS for its own releases: launch, fetch, the AU's LRS, moveOn.

The sequence (cmi5 §8-10, KB §9.3), and who does what:

1. **launch** (a signed-in, enrolled Student): find or create the registration (the
   enrolment), abandon any session still open in it (``abandoned``), mint a session, write
   ``LMS.LaunchData`` to the LRS, record ``launched``, and hand back the AU URL with
   ``endpoint``, ``fetch``, ``actor``, ``registration`` and ``activityId``.
2. **fetch** (the AU, once): exchange the one-time fetch secret for the session's auth
   token. A second call answers error-code 1; an unknown secret error-code 2.
3. **the AU's LRS** (``/cmi5/lrs/...``, ``Authorization: Basic <token>``): every request is
   checked against the session (its actor, registration, activity and launch mode) and every
   statement against the cmi5 rules (app/cmi5/rules.py) before it is forwarded with the
   server's own LRS credential. The token never works for anything outside its session and
   cannot read statements, void, or write ``LMS.LaunchData``.
4. **moveOn**: after an accepted completed/passed (or a waiver) the AU's moveOn is
   evaluated, and ``satisfied`` is recorded for every block and the course that has become
   satisfied, under the triggering session id. NotApplicable AUs count at registration.

Delivery: the LMS's own statements (launched, abandoned, waived, satisfied) are written with
``PUT /statements?statementId=`` and ids derived from what they describe (UUID v5), so a
retry never duplicates. A launch whose LaunchData or launched statement the LRS refuses
fails (503/502) and leaves no session. A satisfied statement the LRS refuses is not marked
sent, and is retried at the next evaluation (the next statement or launch); the AU's own,
already accepted statement is not failed for it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from .. import xapi
from ..course_releases.models import CourseRelease
from ..course_releases.service import pinned_release
from ..lms import LRSResponse, LRSUnavailableError, get_lms_backend
from ..models import Course, Enrollment, EnrollmentStatus, User
from ..rbac import ROLE_PERMISSIONS, Permission
from . import ags, rules
from . import structure as structure_mod
from .content import package
from .models import (
    ABANDONED,
    INITIALIZED,
    LAUNCHED,
    OPEN_STATES,
    TERMINATED,
    Cmi5Grade,
    Cmi5Registration,
    Cmi5Session,
)

logger = logging.getLogger("truenorth.cmi5")

LAUNCH_MODES = ("Normal", "Browse", "Review")
WAIVE_REASONS = ("Tested Out", "Equivalent AU", "Equivalent Outside Activity", "Administrative")


class Cmi5Error(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(t: datetime | None) -> datetime | None:
    return t.replace(tzinfo=UTC) if t is not None and t.tzinfo is None else t


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _hours(name: str, default: float) -> float:
    try:
        return max(float(os.getenv(name, default)), 0.01)
    except ValueError:
        return default


def _stamp(t: datetime | None = None) -> str:
    return (t or _now()).astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_PROCESS_KEY = secrets.token_bytes(32)


def _id_key() -> bytes:
    """The secret LMS statement ids are derived with: TN_SECRETS_KEY (required in
    production). Without it (development) a per-process random key: ids are then only
    stable within one process, which costs idempotence across restarts, never secrecy."""
    key = os.getenv("TN_SECRETS_KEY", "").strip()
    return key.encode() if key else _PROCESS_KEY


def lms_statement_id(*parts: object) -> str:
    """An LMS statement id: HMAC-SHA256 of what it records, under a server secret, shaped as
    a version-8 UUID. Stable for a retry (so the LRS dedupes it), unguessable by an AU (so
    it cannot be squatted), and recognisable: AUs may not send version-8 ids (TN-ID)."""
    digest = bytearray(hmac.new(_id_key(), ":".join(map(str, parts)).encode(), hashlib.sha256).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x80  # version 8
    digest[8] = (digest[8] & 0x3F) | 0x80  # RFC 4122 variant
    return str(uuid.UUID(bytes=bytes(digest)))


def au_credential() -> str:
    """The LRS credential AU traffic is forwarded with (``CMI5_LRS_AUTH``, base64 key:secret).
    It must not be the server's ``LRS_AUTH``: the LRS records the writing credential as each
    statement's authority, which is how an AU's statement is told from TrueNorth's own. With
    none configured, cmi5 launching is off (fail closed). Mint one: python -m app.lms.lrsql_admin."""
    au = os.getenv("CMI5_LRS_AUTH", "").strip()
    if not au or au == os.getenv("LRS_AUTH", "").strip():
        raise Cmi5Error(503, "cmi5 needs CMI5_LRS_AUTH, an LRS credential of its own (not LRS_AUTH): docs/cmi5.md")
    return au


# -- the LRS, through the adapter ---------------------------------------------------------
def _backend():
    backend = get_lms_backend()
    if not backend.supports_resources:
        raise Cmi5Error(503, "cmi5 needs an LRS: set LMS_BACKEND=xapi_lrs (docs/cmi5.md)")
    return backend


def _lrs(method: str, resource: str, **kw) -> LRSResponse:
    try:
        return _backend().xapi_request(method, resource, **kw)
    except LRSUnavailableError as exc:
        raise Cmi5Error(503, f"the LRS is unavailable: {exc}") from exc


def put_statement(stmt: dict) -> None:
    resp = _lrs(
        "PUT",
        "statements",
        params={"statementId": stmt["id"]},
        body=json.dumps(stmt).encode(),
        headers={"Content-Type": "application/json"},
    )
    if resp.status == 409:
        # The id is derived from what the statement records, so a conflict normally means an
        # earlier attempt stored it and then failed after. Only if the stored statement is
        # the same verb, actor and object; anything else under our id is refused, loudly.
        stored = _lrs("GET", "statements", params={"statementId": stmt["id"]})
        try:
            have = stored.json() if stored.status == 200 else None
        except ValueError:
            have = None
        same = (
            isinstance(have, dict)
            and (have.get("verb") or {}).get("id") == stmt["verb"]["id"]
            and (have.get("actor") or {}).get("account") == stmt["actor"]["account"]
            and (have.get("object") or {}).get("id") == stmt["object"]["id"]
        )
        if same:
            logger.info("cmi5: %s %s was already recorded", stmt["verb"]["id"].rsplit("/", 1)[-1], stmt["id"])
            return
        raise Cmi5Error(502, f"statement id {stmt['id']} is taken by a different statement in the LRS")
    if resp.status not in (200, 204):
        raise Cmi5Error(502, f"the LRS refused a {stmt['verb']['id'].rsplit('/', 1)[-1]} statement: {resp.status}")


def put_document(resource: str, params: dict[str, str], doc: dict) -> None:
    """Create or replace a document. lrsql refuses a PUT over an existing document without
    a concurrency header (409), so read its ETag first."""
    current = _lrs("GET", resource, params=params)
    headers = {"Content-Type": "application/json"}
    if current.status == 200 and current.headers.get("etag"):
        headers["If-Match"] = current.headers["etag"]
    elif current.status == 404:
        headers["If-None-Match"] = "*"
    elif current.status != 200:
        raise Cmi5Error(502, f"the LRS answered {current.status} reading {resource}")
    resp = _lrs("PUT", resource, params=params, body=json.dumps(doc).encode(), headers=headers)
    if resp.status not in (200, 204):
        raise Cmi5Error(502, f"the LRS refused {resource}: {resp.status}")


# -- registrations ------------------------------------------------------------------------
def progress(reg: Cmi5Registration) -> dict:
    try:
        data = json.loads(reg.progress or "{}")
    except ValueError:
        data = {}
    data.setdefault("aus", {})
    data.setdefault("satisfied", [])
    return data


def _save(reg: Cmi5Registration, data: dict) -> None:
    reg.progress = json.dumps(data, sort_keys=True)


def enrolment_for(db: Session, user_id: uuid.UUID, release: CourseRelease) -> Enrollment:
    enrolled = (
        db.query(Enrollment).filter(Enrollment.user_id == user_id, Enrollment.course_id == release.course_id).first()
    )
    if enrolled is None:
        raise Cmi5Error(403, "you are not enrolled in this course")
    pinned = pinned_release(db, enrolled.id)
    if pinned is None or pinned.id != release.id:
        raise Cmi5Error(409, "your enrolment is not on this release of the course")
    return enrolled


def registration(db: Session, enrolled: Enrollment, release: CourseRelease, *, lock: bool = True) -> Cmi5Registration:
    """The enrolment's registration, created on first launch (and NotApplicable evaluated)."""
    q = db.query(Cmi5Registration).filter(
        Cmi5Registration.id == enrolled.id, Cmi5Registration.user_id == enrolled.user_id
    )
    reg = (q.with_for_update() if lock else q).one_or_none()
    if reg is None:
        reg = Cmi5Registration(
            id=enrolled.id,
            enrollment_id=enrolled.id,
            tenant_id=enrolled.tenant_id or release.tenant_id,
            user_id=enrolled.user_id,
            release_id=release.id,
            progress="{}",
        )
        db.add(reg)
        db.flush()
        evaluate(db, reg, release, str(uuid.uuid4()))
    elif reg.release_id != release.id:
        raise Cmi5Error(409, "this enrolment's cmi5 registration is on another release of the course")
    return reg


# -- moveOn and satisfied -----------------------------------------------------------------
def au_satisfied(au: structure_mod.AU, state: dict) -> bool:
    s = state.get(str(au.index)) or {}
    if s.get("waived"):
        return True
    done, passed = bool(s.get("completed")), bool(s.get("passed"))
    return {
        "NotApplicable": True,
        "Passed": passed,
        "Completed": done,
        "CompletedAndPassed": done and passed,
        "CompletedOrPassed": done or passed,
    }[au.move_on]


def _satisfied_statement(reg: Cmi5Registration, runtime_id: str, publisher_id: str, kind: str, session_id: str) -> dict:
    return {
        "id": lms_statement_id(reg.id, "satisfied", runtime_id),
        "actor": xapi.actor(reg.user_id),
        "verb": {"id": rules.SATISFIED, "display": {"en": "satisfied"}},
        "object": {
            "objectType": "Activity",
            "id": runtime_id,
            "definition": {"type": rules.TYPE_BLOCK if kind == "block" else rules.TYPE_COURSE},
        },
        "context": {
            "registration": str(reg.id),
            "contextActivities": {"category": [{"id": rules.CAT_CMI5}], "grouping": [{"id": publisher_id}]},
            "extensions": {rules.EXT_SESSION: session_id},
        },
        "timestamp": _stamp(),
    }


def evaluate(db: Session, reg: Cmi5Registration, release: CourseRelease, session_id: str) -> list[str]:
    """Record ``satisfied`` for every block and the course newly satisfied. Returns their keys."""
    _, parsed = package(db, release)
    data = progress(reg)
    done = set(data["satisfied"])
    aus = data["aus"]
    newly: list[str] = []

    def node_ok(kind: str, index: int) -> bool:
        if kind == "au":
            return au_satisfied(parsed.aus[index], aus)
        block = parsed.blocks[index]
        ok = all(node_ok(k, i) for k, i in block.children)
        key = f"block:{index}"
        if ok and key not in done:
            runtime = structure_mod.block_runtime_id(release.id, index)
            if _send_satisfied(_satisfied_statement(reg, runtime, block.publisher_id, "block", session_id)):
                done.add(key)
                newly.append(key)
        return ok

    course_ok = all([node_ok(k, i) for k, i in parsed.children])  # every child, even after a False
    if course_ok and "course" not in done:
        runtime = structure_mod.course_runtime_id(release.id)
        if _send_satisfied(_satisfied_statement(reg, runtime, parsed.publisher_id, "course", session_id)):
            done.add("course")
            newly.append("course")
    data["satisfied"] = sorted(done)
    _save(reg, data)
    return newly


def _send_satisfied(stmt: dict) -> bool:
    try:
        put_statement(stmt)
        return True
    except Cmi5Error as exc:
        logger.error(
            "cmi5: satisfied for %s not recorded (retried at the next evaluation): %s", stmt["object"]["id"], exc
        )
        return False


# -- launch ---------------------------------------------------------------------------------
@dataclass
class Launch:
    url: str
    session_id: uuid.UUID
    registration: uuid.UUID
    launch_mode: str
    launch_method: str


def _context_template(au: structure_mod.AU, session_id: str) -> dict:
    return {
        "contextActivities": {"grouping": [{"objectType": "Activity", "id": au.publisher_id}]},
        "extensions": {rules.EXT_SESSION: session_id},
    }


def launch(
    db: Session, user_id: uuid.UUID, release: CourseRelease, au_index: int, launch_mode: str | None = None
) -> Launch:
    au_credential()  # fail closed before anything is written: no AU credential, no launch
    enrolled = enrolment_for(db, user_id, release)
    _, parsed = package(db, release)
    if not 0 <= au_index < len(parsed.aus):
        raise Cmi5Error(404, f"no AU {au_index}: the course has {len(parsed.aus)}")
    au = parsed.aus[au_index]
    reg = registration(db, enrolled, release)
    evaluate(db, reg, release, str(uuid.uuid4()))  # retries any satisfied the LRS refused earlier
    data = progress(reg)
    if launch_mode is not None and launch_mode not in LAUNCH_MODES:
        raise Cmi5Error(422, f"launchMode must be one of {', '.join(LAUNCH_MODES)}")
    mode = launch_mode or ("Review" if au_satisfied(au, data["aus"]) and au.move_on != "NotApplicable" else "Normal")

    for stale in (
        db.query(Cmi5Session)
        .filter(Cmi5Session.registration_id == reg.id, Cmi5Session.state.in_(OPEN_STATES))
        .with_for_update()
        .all()
    ):
        abandon(db, stale, release)

    session_id = uuid.uuid4()
    sid = str(session_id)
    actor = xapi.actor(user_id)
    runtime = structure_mod.au_runtime_id(release.id, au_index)
    template = _context_template(au, sid)
    au_url = structure_mod.au_url(release.id, au_index)
    launch_data: dict = {
        "contextTemplate": template,
        "launchMode": mode,
        "moveOn": au.move_on,
        "returnURL": f"{structure_mod.web_base()}/au/releases/{release.id}",
    }
    if au.mastery_score is not None:
        launch_data["masteryScore"] = au.mastery_score
    if au.launch_parameters:
        launch_data["launchParameters"] = au.launch_parameters
    if au.entitlement_key:
        launch_data["entitlementKey"] = {"courseStructure": au.entitlement_key}
    put_document(
        "activities/state",
        {"stateId": "LMS.LaunchData", "activityId": runtime, "agent": json.dumps(actor), "registration": str(reg.id)},
        launch_data,
    )

    extensions = {
        rules.EXT_SESSION: sid,
        rules.EXT_LAUNCHMODE: mode,
        rules.EXT_LAUNCHURL: au_url,
        rules.EXT_MOVEON: au.move_on,
    }
    if au.launch_parameters:
        extensions[rules.EXT_LAUNCHPARAMS] = au.launch_parameters
    if au.mastery_score is not None:
        extensions[rules.EXT_MASTERY] = au.mastery_score
    put_statement(
        {
            "id": lms_statement_id(sid, "launched"),
            "actor": actor,
            "verb": {"id": rules.LAUNCHED, "display": {"en": "launched"}},
            "object": {"objectType": "Activity", "id": runtime},
            "context": {
                "registration": str(reg.id),
                "contextActivities": {"category": [{"id": rules.CAT_CMI5}], "grouping": [{"id": au.publisher_id}]},
                "extensions": extensions,
            },
            "timestamp": _stamp(),
        }
    )

    fetch_secret = secrets.token_urlsafe(32)
    now = _now()
    db.add(
        Cmi5Session(
            id=session_id,
            registration_id=reg.id,
            tenant_id=reg.tenant_id,
            user_id=user_id,
            au_index=au_index,
            launch_mode=mode,
            move_on=au.move_on,
            mastery_score=au.mastery_score,
            context_template=json.dumps(template),
            fetch_hash=_hash(fetch_secret),
            state=LAUNCHED,
            sent="[]",
            launched_at=now,
            expires_at=now + timedelta(hours=_hours("CMI5_SESSION_HOURS", 12)),
        )
    )
    db.flush()
    api = structure_mod.api_base()
    query = urlencode(
        {
            "endpoint": f"{api}/cmi5/lrs/",
            "fetch": f"{api}/cmi5/fetch/{fetch_secret}",
            "actor": json.dumps(actor, separators=(",", ":")),
            "registration": str(reg.id),
            "activityId": runtime,
        }
    )
    return Launch(
        url=f"{au_url}?{query}", session_id=session_id, registration=reg.id, launch_mode=mode, launch_method="OwnWindow"
    )


# -- abandon and waive ----------------------------------------------------------------------
def abandon(db: Session, session: Cmi5Session, release: CourseRelease) -> None:
    """End an open session abnormally: the LMS's ``abandoned``, then no more statements."""
    if session.state not in OPEN_STATES:
        return
    _, parsed = package(db, release)
    au = parsed.aus[session.au_index]
    since = _aware(session.initialized_at) or _aware(session.launched_at)
    put_statement(
        {
            "id": lms_statement_id(session.id, "abandoned"),
            "actor": xapi.actor(session.user_id),
            "verb": {"id": rules.ABANDONED, "display": {"en": "abandoned"}},
            "object": {"objectType": "Activity", "id": structure_mod.au_runtime_id(release.id, session.au_index)},
            "result": {"duration": xapi.iso_duration(_now() - since)},
            "context": {
                "registration": str(session.registration_id),
                "contextActivities": {"category": [{"id": rules.CAT_CMI5}], "grouping": [{"id": au.publisher_id}]},
                "extensions": {rules.EXT_SESSION: str(session.id)},
            },
            "timestamp": _stamp(),
        }
    )
    session.state = ABANDONED
    session.ended_at = _now()
    db.flush()


def waive(db: Session, reg: Cmi5Registration, release: CourseRelease, au_index: int, reason: str) -> list[str]:
    """Waive an AU for this registration (``waived``, own session id), then evaluate moveOn."""
    if reason not in WAIVE_REASONS:
        raise Cmi5Error(422, f"reason must be one of: {', '.join(WAIVE_REASONS)}")
    _, parsed = package(db, release)
    if not 0 <= au_index < len(parsed.aus):
        raise Cmi5Error(404, f"no AU {au_index}")
    data = progress(reg)
    state = data["aus"].setdefault(str(au_index), {})
    if state.get("waived"):
        raise Cmi5Error(409, "this AU is already waived in this registration")
    au = parsed.aus[au_index]
    session_id = str(uuid.uuid4())
    put_statement(
        {
            "id": lms_statement_id(reg.id, "waived", au_index),
            "actor": xapi.actor(reg.user_id),
            "verb": {"id": rules.WAIVED, "display": {"en": "waived"}},
            "object": {"objectType": "Activity", "id": structure_mod.au_runtime_id(release.id, au_index)},
            "result": {"success": True, "completion": True, "extensions": {rules.EXT_REASON: reason}},
            "context": {
                "registration": str(reg.id),
                "contextActivities": {
                    "category": [{"id": rules.CAT_CMI5}, {"id": rules.CAT_MOVEON}],
                    "grouping": [{"id": au.publisher_id}],
                },
                "extensions": {rules.EXT_SESSION: session_id},
            },
            "timestamp": _stamp(),
        }
    )
    state["waived"] = reason
    _save(reg, data)
    return evaluate(db, reg, release, session_id)


# -- fetch ----------------------------------------------------------------------------------
def fetch(db: Session, secret: str) -> dict:
    """The fetch URL: the auth token once, then error-code 1 (cmi5 8.2). Always a JSON body."""
    session = db.query(Cmi5Session).filter(Cmi5Session.fetch_hash == _hash(secret)).with_for_update().one_or_none()
    if session is None:
        return {"error-code": "2", "error-text": "Unknown or invalid fetch URL."}
    fetch_window = timedelta(hours=_hours("CMI5_FETCH_HOURS", 0.25))
    if session.fetched_at is not None:
        return {"error-code": "1", "error-text": "The authorization token has already been returned."}
    if session.state not in OPEN_STATES or _now() > _aware(session.launched_at) + fetch_window:
        return {"error-code": "1", "error-text": "The session has ended or the fetch URL has expired."}
    token_secret = secrets.token_urlsafe(32)
    session.token_hash = _hash(token_secret)
    session.fetched_at = _now()
    db.flush()
    return {"auth-token": base64.b64encode(f"{session.id}:{token_secret}".encode()).decode()}


# -- the AU's LRS ---------------------------------------------------------------------------
def authenticate(db: Session, authorization: str | None) -> Cmi5Session:
    """The session behind ``Authorization: Basic <auth-token>``, row-locked, or 401/403."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "basic" or not token:
        raise Cmi5Error(401, "send the cmi5 auth token as Basic credentials")
    try:
        sid, _, secret = base64.b64decode(token.strip(), validate=True).decode().partition(":")
        session_uuid = uuid.UUID(sid)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise Cmi5Error(401, "not a cmi5 auth token") from exc
    # tenant-safe: the token names the session, and its secret is checked on the next line.
    session = db.query(Cmi5Session).filter(Cmi5Session.id == session_uuid).one_or_none()
    if session is None or not session.token_hash or not hmac.compare_digest(session.token_hash, _hash(secret)):
        raise Cmi5Error(401, "not a valid cmi5 auth token")
    # Registration first, then the session: the order launch takes them in.
    # tenant-safe: the authenticated session's own registration, locked, nothing read.
    db.query(Cmi5Registration).filter(Cmi5Registration.id == session.registration_id).with_for_update().one()
    db.refresh(session, with_for_update=True)
    if session.state == ABANDONED:
        raise Cmi5Error(403, "8.1.2.0-2: the session was abandoned")
    if session.state == TERMINATED:
        raise Cmi5Error(403, "8.1.2.0-2: the session is terminated")
    if _now() > _aware(session.expires_at):
        raise Cmi5Error(403, "8.1.2.0-2: the session has expired")
    return session


def _release(db: Session, session: Cmi5Session) -> tuple[Cmi5Registration, CourseRelease]:
    # tenant-safe: the authenticated session's own registration, and that registration's release.
    reg = db.get(Cmi5Registration, session.registration_id)
    # tenant-safe: as above.
    release = db.get(CourseRelease, reg.release_id)
    return reg, release


def _agent_param(params: dict[str, str], actor: dict) -> None:
    raw = params.get("agent")
    if raw is None:
        raise rules.RuleViolationError("11.0.0.0-1", "the agent parameter is required", status=400)
    try:
        agent = json.loads(raw)
    except ValueError as exc:
        raise rules.RuleViolationError("8.1.3.0-3", "agent is not JSON", status=400) from exc
    if not rules.same_actor(agent, actor):
        raise rules.RuleViolationError("8.1.3.0-3", "agent is not the launch actor", status=403)


def proxy(
    db: Session,
    session: Cmi5Session,
    method: str,
    resource: str,
    params: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> LRSResponse:
    """Check one AU request against its session and the cmi5 rules, then forward it."""
    reg, release = _release(db, session)
    _still_entitled(db, reg, release)
    actor = xapi.actor(session.user_id)
    runtime = structure_mod.au_runtime_id(release.id, session.au_index)
    method = method.upper()
    resource = resource.strip("/")
    forward = {k: v for k, v in headers.items() if k.lower() in ("content-type", "if-match", "if-none-match")}
    accepted: list[str] = []
    graded: float | None = None
    marks = {"launch_data": False, "prefs": False}

    if resource == "about" and method == "GET":
        pass
    elif resource == "statements":
        if method not in ("POST", "PUT"):
            raise Cmi5Error(403, "the AU credential is write-only for statements")
        graded = _graded(db, session, release.id)
        accepted, body = _check_statements(session, reg, runtime, actor, body, params, method, graded)
    elif resource == "activities/state":
        _agent_param(params, actor)
        if params.get("activityId") != runtime:
            raise rules.RuleViolationError("10.1.0.0-3", "activityId is not the launch activityId", status=403)
        # Every State request names this registration: without one, a DELETE (or a read)
        # would reach the documents of every registration of this AU, LMS.LaunchData included.
        if params.get("registration") != str(reg.id):
            raise rules.RuleViolationError("8.1.4.0-3", "registration must be the launch registration", status=403)
        if not params.get("stateId") and method != "GET":
            raise Cmi5Error(403, "name the stateId: the AU may not change or delete all its State documents at once")
        if params.get("stateId") == "LMS.LaunchData":
            if method != "GET":
                raise rules.RuleViolationError(
                    "10.2.1.0-5", "LMS.LaunchData is the LMS's: read it, never write it", status=403
                )
            marks["launch_data"] = True
    elif resource == "agents/profile":
        _agent_param(params, actor)
        if method != "GET":
            # An Agent Profile follows the Student into every course and LMS: the AU writes
            # none. Only cmi5LearnerPreferences is the AU's business at all, and the LMS may
            # refuse changes to it (cmi5 11.0), as TrueNorth does.
            raise Cmi5Error(403, "Agent Profiles are read-only for an AU")
        if params.get("profileId") == "cmi5LearnerPreferences":
            marks["prefs"] = True
    elif resource == "agents" and method == "GET":
        _agent_param(params, actor)
    elif resource in ("activities", "activities/profile"):
        if params.get("activityId") != runtime:
            raise rules.RuleViolationError("10.1.0.0-3", "activityId is not the launch activityId", status=403)
        if method != "GET":
            # Activity Profiles are shared by every registration of the AU (the LMS's to
            # write); the activity definition is the LMS's too.
            raise Cmi5Error(403, f"{resource} is read-only for an AU")
    else:
        raise Cmi5Error(404, f"{method} {resource} is not part of the AU's LRS")

    # The AU's own credential: the LRS records it as the authority of what the AU wrote.
    resp = _lrs(method, resource, params=params, body=body or None, headers=forward, credential=au_credential())
    if resp.status in (200, 204):
        if marks["launch_data"]:
            session.launch_data_fetched = True
        if accepted:
            _after_statements(db, session, reg, release, accepted, graded)
    if marks["prefs"] and resp.status in (200, 404):
        session.prefs_fetched = True
    db.flush()
    return resp


def _graded(db: Session, session: Cmi5Session, release_id: uuid.UUID) -> float | None:
    """The latest score TrueNorth marked for this Student and AU since the session launched."""
    row = (
        db.query(Cmi5Grade)
        .filter(
            Cmi5Grade.user_id == session.user_id,
            Cmi5Grade.release_id == release_id,
            Cmi5Grade.au_index == session.au_index,
            Cmi5Grade.created_at >= session.launched_at,
        )
        .order_by(Cmi5Grade.created_at.desc())
        .first()
    )
    return row.scaled if row else None


def _view(
    session: Cmi5Session, reg: Cmi5Registration, runtime: str, actor: dict, graded: float | None = None
) -> rules.SessionView:
    state = progress(reg)["aus"].get(str(session.au_index)) or {}
    return rules.SessionView(
        actor=actor,
        registration=str(reg.id),
        session_id=str(session.id),
        au_runtime_id=runtime,
        context_template=json.loads(session.context_template),
        launch_mode=session.launch_mode,
        mastery_score=session.mastery_score,
        initialized=session.state != LAUNCHED,
        terminated=session.state == TERMINATED,
        prefs_fetched=session.prefs_fetched,
        sent=set(json.loads(session.sent or "[]")),
        au_completed=bool(state.get("completed")),
        au_passed=bool(state.get("passed")),
        tn_iri_roots=(xapi.iri_base() + "/", "http://truenorthrange.local/"),
        require_graded=True,
        graded=graded,
    )


def _no_constant(name: str):
    raise rules.RuleViolationError("TN-NUMBER", f"{name} is not a JSON number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise rules.RuleViolationError("TN-NUMBER", f"{text} is not a finite number")
    return value


# What the LRS sets on a statement itself; an AU's values are dropped, never forwarded.
_SERVER_SET = ("authority", "stored")


def _check_statements(
    session, reg, runtime, actor, body: bytes, params: dict[str, str], method: str, graded: float | None = None
) -> tuple[list[str], bytes]:
    """The cmi5-defined verbs accepted, and the body to forward (re-serialised from what was
    checked, without the server-set properties)."""
    try:
        payload = json.loads(body or b"null", parse_constant=_no_constant, parse_float=_finite_float)
    except ValueError as exc:
        raise rules.RuleViolationError("4.1.0.0-1", "the body is not JSON") from exc
    statements = payload if isinstance(payload, list) else [payload]
    if not statements:
        raise rules.RuleViolationError("4.1.0.0-1", "no statements")
    if method == "PUT" and (len(statements) != 1 or params.get("statementId") != (statements[0] or {}).get("id")):
        raise rules.RuleViolationError("4.1.0.0-1", "PUT carries one statement whose id is the statementId parameter")
    view = _view(session, reg, runtime, actor, graded)
    accepted: list[str] = []
    for st in statements:
        verb = rules.check(st, view, accepted)
        if verb:
            accepted.append(verb)
        for key in _SERVER_SET:
            st.pop(key, None)
    cleaned = statements if isinstance(payload, list) else statements[0]
    return accepted, json.dumps(cleaned).encode()


def _still_entitled(db: Session, reg: Cmi5Registration, release: CourseRelease) -> None:
    """Each AU request: the Student is still enrolled on this release and the course is still
    published (unless they may author courses, the rule launching applies: an author previews
    a draft). A token outlives neither (primary-key reads only)."""
    # tenant-safe: the registration's own enrolment.
    enrolled = db.get(Enrollment, reg.enrollment_id)
    if enrolled is None or enrolled.status == EnrollmentStatus.withdrawn:
        raise Cmi5Error(403, "the enrolment has ended")
    pinned = pinned_release(db, enrolled.id)
    if pinned is None or pinned.id != release.id:
        raise Cmi5Error(403, "the enrolment is no longer on this release")
    # tenant-safe: the authenticated session's registration's own release's course.
    course = db.get(Course, release.course_id)
    if course is not None and course.is_published:
        return
    # tenant-safe: the enrolment's own user, for their current role.
    who = db.get(User, enrolled.user_id)
    if course is None or who is None or Permission.COURSE_AUTHOR not in ROLE_PERMISSIONS.get(who.role, set()):
        raise Cmi5Error(403, "the course is no longer published")


def _after_statements(
    db: Session,
    session: Cmi5Session,
    reg: Cmi5Registration,
    release: CourseRelease,
    accepted: list[str],
    graded: float | None = None,
) -> None:
    """Record what the accepted cmi5-defined statements mean. A passed/failed records
    TrueNorth's mark (``graded``: the rules refused any other score) as the AU's score, and
    a result goes on to the gradebook of an LMS that launched the AU over LTI (ags.py)."""
    sent = json.loads(session.sent or "[]")
    data = progress(reg)
    state = data["aus"].setdefault(str(session.au_index), {})
    judged = False
    if graded is not None and (rules.PASSED in accepted or rules.FAILED in accepted):
        state["score"] = graded
    for verb in accepted:
        sent.append(verb)
        if verb == rules.INITIALIZED:
            session.state = INITIALIZED
            session.initialized_at = _now()
        elif verb == rules.TERMINATED:
            session.state = TERMINATED
            session.ended_at = _now()
        elif verb == rules.COMPLETED:
            state["completed"] = True
            judged = True
        elif verb == rules.PASSED:
            state["passed"] = True
            judged = True
        elif verb == rules.FAILED:
            state["failed"] = True
    session.sent = json.dumps(sent)
    _save(reg, data)
    if judged:
        evaluate(db, reg, release, str(session.id))
    if any(verb in rules.JUDGED for verb in accepted):
        ags.enqueue(db, reg, release, session.au_index, state)
