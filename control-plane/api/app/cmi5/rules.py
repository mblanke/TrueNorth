"""What an AU may send: the cmi5 (Quartz) rules TrueNorth enforces as the LMS.

cmi5 says the LMS SHOULD reject a statement that breaks its rules and MUST void one it
accepted. TrueNorth rejects, before anything reaches the LRS, so there is nothing to void.
Each refusal names the requirement it enforces with the ID from the cmi5 requirements list
(``@cmi5/requirements``, the IDs ADL's CATAPULT reports), so an AU author can look it up.

The rules follow the cmi5 spec sections 9 and 10 and the reference player's reading of
them (adlnet/CATAPULT ``player/service/plugins/routes/lrs.js``), with one deliberate
difference: the AU's credential here cannot read statements back (least privilege).
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field

CAT_CMI5 = "https://w3id.org/xapi/cmi5/context/categories/cmi5"
CAT_MOVEON = "https://w3id.org/xapi/cmi5/context/categories/moveon"
EXT = "https://w3id.org/xapi/cmi5/context/extensions/"
EXT_SESSION = EXT + "sessionid"
EXT_MASTERY = EXT + "masteryscore"
EXT_LAUNCHMODE = EXT + "launchmode"
EXT_LAUNCHURL = EXT + "launchurl"
EXT_MOVEON = EXT + "moveon"
EXT_LAUNCHPARAMS = EXT + "launchparameters"
EXT_REASON = "https://w3id.org/xapi/cmi5/result/extensions/reason"

ADL = "http://adlnet.gov/expapi/verbs/"
LAUNCHED = ADL + "launched"
INITIALIZED = ADL + "initialized"
COMPLETED = ADL + "completed"
PASSED = ADL + "passed"
FAILED = ADL + "failed"
TERMINATED = ADL + "terminated"
VOIDED = ADL + "voided"
ABANDONED = "https://w3id.org/xapi/adl/verbs/abandoned"
WAIVED = "https://w3id.org/xapi/adl/verbs/waived"
SATISFIED = "https://w3id.org/xapi/adl/verbs/satisfied"
AU_DEFINED = (INITIALIZED, COMPLETED, PASSED, FAILED, TERMINATED)
JUDGED = (COMPLETED, PASSED, FAILED)
TYPE_BLOCK = "https://w3id.org/xapi/cmi5/activitytype/block"
TYPE_COURSE = "https://w3id.org/xapi/cmi5/activitytype/course"

_UTC = re.compile(r"(Z|\+00:?(?:00)?)$")


class RuleViolationError(Exception):
    """A statement (or request) the LMS refuses. ``req`` is the cmi5 requirement id."""

    def __init__(self, req: str, message: str, status: int = 400):
        super().__init__(f"{req}: {message}")
        self.req = req
        self.message = message
        self.status = status


@dataclass
class SessionView:
    """What the rules need to know about the session and its registration."""

    actor: dict
    registration: str
    session_id: str
    au_runtime_id: str
    context_template: dict
    launch_mode: str
    mastery_score: float | None
    initialized: bool
    terminated: bool
    prefs_fetched: bool
    sent: set[str] = field(default_factory=set)  # cmi5-defined verbs sent in this session
    au_completed: bool = False  # in the registration
    au_passed: bool = False
    # IRI roots of TrueNorth's own activities (XAPI_IRI_BASE, the legacy one): a context
    # activity under one of them must be under this AU (TN-SCOPE).
    tn_iri_roots: tuple[str, ...] = ()
    # TrueNorth marks the quiz (POST .../grade): passed/failed must report that score.
    require_graded: bool = True
    graded: float | None = None  # the latest server-marked score since this session's launch


# TrueNorth's own rules on top of cmi5 (ids starting "TN-"), for what an LMS that also issues
# TrueNorth's records must not let an AU do:
#  TN-SCOPE    an AU's statements are about its own activity (the launch activityId, or an
#              activity under it), never another TrueNorth activity such as a quiz or course;
#  TN-DEFINED  the cmi5-defined verbs only as cmi5-defined statements (with the category), so
#              no "passed" escapes the cmi5 rules by leaving the category off;
#  TN-ID       statement ids of UUID version 8 are reserved for the LMS's own statements;
#  TN-SCORE    judged against a masteryScore, passed/failed carry score.scaled;
#  TN-GRADE    in a TrueNorth session, passed/failed report the score TrueNorth marked;
#  TN-VERB     no verb id that is a cmi5/LMS verb in another spelling (case, https, a slash);
#  TN-RESULT   a cmi5-allowed statement carries no success, completion or score;
#  TN-NUMBER   no NaN or infinity anywhere (they would compare false against any mark).


def is_reserved_id(value) -> bool:
    """Version-8 UUIDs: the form TrueNorth's LMS statement ids take (app/cmi5/lms.py)."""
    try:
        return uuid.UUID(str(value)).version == 8
    except ValueError:
        return False


def in_scope(object_id, au_runtime_id: str) -> bool:
    """The AU's own activity, or a plain path under it: no empty, '.' or '..' segment and no
    %-encoding (which an LRS or reader could resolve back out of the subtree)."""
    if not isinstance(object_id, str):
        return False
    if object_id == au_runtime_id:
        return True
    if not object_id.startswith(au_runtime_id + "/"):
        return False
    rest = object_id[len(au_runtime_id) + 1 :]
    if any(c in rest for c in "%\\?#"):
        return False
    return all(seg not in ("", ".", "..") for seg in rest.split("/"))


def _norm_verb(verb: str) -> str:
    v = verb.strip().lower().rstrip("/")
    return "http://" + v[len("https://") :] if v.startswith("https://") else v


# Every verb an AU may not use as spelled differently, by its normalised form.
_CANONICAL = {_norm_verb(v): v for v in (*AU_DEFINED, LAUNCHED, ABANDONED, WAIVED, SATISFIED, VOIDED)}


def finite(value) -> bool:
    """No NaN or infinity anywhere in a JSON value, nor an integer too large for a double
    (float() of it would raise rather than compare)."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return math.isfinite(float(value))
        except OverflowError:
            return False
    if isinstance(value, dict):
        return all(finite(v) for v in value.values())
    if isinstance(value, list):
        return all(finite(v) for v in value)
    return True


def _ids(items) -> list[str]:
    return [a.get("id") for a in (items or []) if isinstance(a, dict)]


def same_actor(given, expected: dict) -> bool:
    """Agents are the same if their account IFI is; objectType/name are not identity."""
    if not isinstance(given, dict):
        return False
    acct = given.get("account") or {}
    want = expected["account"]
    return (
        given.get("objectType", "Agent") == "Agent"
        and acct.get("homePage") == want["homePage"]
        and acct.get("name") == want["name"]
        and not any(k in given for k in ("mbox", "mbox_sha1sum", "openid"))
    )


def _matches_template(context: dict, template: dict) -> None:
    for prop, expected in template.items():
        given = context.get(prop)
        if prop == "contextActivities":
            given = given or {}
            for key, wanted in expected.items():
                have = _ids(given.get(key))
                for i, act in enumerate(wanted):
                    if i >= len(have) or have[i] != act.get("id"):
                        raise RuleViolationError(
                            "10.2.1.0-6", f"context.contextActivities.{key} does not follow the contextTemplate"
                        )
        elif isinstance(expected, dict):
            if not isinstance(given, dict):
                raise RuleViolationError("10.2.1.0-6", f"context.{prop} is missing (contextTemplate)")
            for k, v in expected.items():
                if given.get(k) != v:
                    raise RuleViolationError(
                        "10.2.1.0-7", f"context.{prop}[{k!r}] overwrites the contextTemplate value"
                    )
        elif given != expected:
            raise RuleViolationError("10.2.1.0-7", f"context.{prop} overwrites the contextTemplate value")


def check(st: dict, view: SessionView, earlier: list[str]) -> str | None:
    """Refuse ``st`` (raise RuleViolationError) or accept it. Returns the verb id when it is a
    cmi5-defined statement, None for a cmi5-allowed one. ``earlier`` holds the cmi5-defined
    verbs accepted earlier in the same request."""
    if not view.prefs_fetched:
        raise RuleViolationError(
            "11.0.0.0-3", "read the cmi5LearnerPreferences agent profile before sending statements"
        )
    if not isinstance(st, dict):
        raise RuleViolationError("4.1.0.0-1", "a statement is a JSON object")
    for prop in ("actor", "verb", "object"):
        if prop not in st:
            raise RuleViolationError("4.1.0.0-1", f"statement has no {prop}")
    verb = (st.get("verb") or {}).get("id")
    if not verb or not isinstance(verb, str):
        raise RuleViolationError("4.1.0.0-1", "statement has no verb.id")
    if not finite(st):
        raise RuleViolationError("TN-NUMBER", "NaN and infinity are not JSON numbers")
    canonical = _CANONICAL.get(_norm_verb(verb))
    if canonical is not None and canonical != verb:
        raise RuleViolationError("TN-VERB", f"{verb!r} is {canonical} spelled differently", status=403)
    if not st.get("id"):
        raise RuleViolationError("9.1.0.0-1", "the AU must give every statement an id")
    if is_reserved_id(st["id"]):
        raise RuleViolationError("TN-ID", "statement ids of UUID version 8 are reserved for the LMS", status=403)
    if verb == VOIDED:
        raise RuleViolationError("6.3.0.0-1", "an AU may not void statements", status=403)
    if verb in (LAUNCHED, ABANDONED, WAIVED, SATISFIED):
        raise RuleViolationError("9.3.0.0-1", f"{verb} is the LMS's to send, not the AU's", status=403)
    ts = st.get("timestamp")
    if not ts:
        raise RuleViolationError("9.7.0.0-1", "statement has no timestamp")
    if not isinstance(ts, str) or not _UTC.search(ts):
        raise RuleViolationError("9.7.0.0-2", "timestamp must be UTC")
    if not same_actor(st.get("actor"), view.actor):
        raise RuleViolationError("8.1.3.0-3", "actor is not the launch actor")
    context = st.get("context")
    if not isinstance(context, dict):
        raise RuleViolationError("9.6.0.0-1", "statement has no context")
    if not isinstance(context.get("contextActivities"), dict):
        raise RuleViolationError("10.2.1.0-6", "context has no contextActivities (contextTemplate)")
    _matches_template(context, view.context_template)
    for key in ("parent", "grouping", "other", "category"):
        for act_id in _ids(context["contextActivities"].get(key)):
            ours = isinstance(act_id, str) and any(act_id.startswith(root) for root in view.tn_iri_roots)
            if ours and not in_scope(act_id, view.au_runtime_id):
                raise RuleViolationError(
                    "TN-SCOPE",
                    f"context.contextActivities.{key} names a TrueNorth activity outside this AU",
                    status=403,
                )
    categories = _ids(context["contextActivities"].get("category"))
    defined = CAT_CMI5 in categories
    if CAT_MOVEON in categories and (not defined or verb not in JUDGED):
        raise RuleViolationError("9.6.2.2-2", "the moveon category belongs only on completed, passed and failed")
    if context.get("registration") != view.registration:
        raise RuleViolationError("9.6.1.0-1", "context.registration is not the launch registration")
    if (context.get("extensions") or {}).get(EXT_SESSION) != view.session_id:
        raise RuleViolationError("9.6.3.1-4", "the sessionid extension is missing or not this session's")
    if not view.initialized and verb != INITIALIZED and INITIALIZED not in earlier:
        raise RuleViolationError("9.3.0.0-4", "initialized must be the first statement of the session")
    if view.terminated or TERMINATED in earlier:
        raise RuleViolationError("9.3.0.0-5", "the session is terminated", status=403)
    obj = st["object"]
    if not isinstance(obj, dict) or obj.get("objectType", "Activity") != "Activity":
        raise RuleViolationError("TN-SCOPE", "an AU's statements have its own activity as their object", status=403)
    if not in_scope(obj.get("id"), view.au_runtime_id):
        raise RuleViolationError(
            "TN-SCOPE", "object.id must be the launch activityId or an activity under it", status=403
        )
    if not defined:
        if verb in AU_DEFINED:
            raise RuleViolationError(
                "TN-DEFINED", f"{verb.rsplit('/', 1)[1]} is cmi5-defined: send it with the cmi5 category", status=403
            )
        result = st.get("result")
        if isinstance(result, dict) and any(k in result for k in ("success", "completion", "score")):
            raise RuleViolationError(
                "TN-RESULT", "a cmi5-allowed statement carries no success, completion or score", status=403
            )
        return None  # cmi5-allowed: any other verb, about this AU
    return _check_defined(st, verb, view, earlier)


def _check_defined(st: dict, verb: str, view: SessionView, earlier: list[str]) -> str:
    actor = st["actor"]
    if actor.get("objectType", "Agent") != "Agent":
        raise RuleViolationError("9.2.0.0-2", "the actor of a cmi5-defined statement is an Agent")
    if verb not in AU_DEFINED:
        raise RuleViolationError("9.3.0.0-1", f"{verb} is not a cmi5-defined AU verb")
    obj = st["object"]
    if not isinstance(obj, dict) or not obj.get("id"):
        raise RuleViolationError("9.4.0.0-1", "object.id is missing")
    if obj["id"] != view.au_runtime_id:
        raise RuleViolationError("8.1.5.0-6", "object.id must be the launch activityId")
    if view.launch_mode == "Browse" and verb in JUDGED:
        raise RuleViolationError("10.2.2.0-2", "no completed/passed/failed in Browse mode")
    if view.launch_mode == "Review" and verb in JUDGED:
        raise RuleViolationError("10.2.2.0-3", "no completed/passed/failed in Review mode")

    result = st.get("result")
    if not isinstance(result, dict):
        result = None
    missing = {COMPLETED: "9.5.3.0-1", PASSED: "9.5.2.0-1", FAILED: "9.5.2.0-2", TERMINATED: "9.5.4.1-1"}
    if result is None:
        if verb in missing:
            raise RuleViolationError(missing[verb], f"{verb.rsplit('/', 1)[1]} needs a result")
    else:
        if "completion" in result and verb != COMPLETED:
            raise RuleViolationError("9.5.3.0-2", "only completed may carry result.completion")
        if verb == COMPLETED and result.get("completion") is not True:
            raise RuleViolationError("9.5.3.0-1", "completed needs result.completion = true")
        if "success" in result and verb not in (PASSED, FAILED):
            raise RuleViolationError("9.5.2.0-3", "only passed and failed may carry result.success")
        if verb == PASSED and result.get("success") is not True:
            raise RuleViolationError("9.5.2.0-1", "passed needs result.success = true")
        if verb == FAILED and result.get("success") is not False:
            raise RuleViolationError("9.5.2.0-2", "failed needs result.success = false")
        categories = _ids(st["context"]["contextActivities"].get("category"))
        if ("completion" in result or "success" in result) and CAT_MOVEON not in categories:
            raise RuleViolationError("9.6.2.2-1", "completed/passed/failed need the moveon category")
        score = result.get("score")
        if score is not None and (
            not isinstance(score, dict)
            or any(k in score and not isinstance(score[k], int | float) for k in ("scaled", "raw", "min", "max"))
        ):
            raise RuleViolationError("4.1.0.0-1", "result.score values are numbers")
        if score is not None:
            if verb not in (PASSED, FAILED):
                raise RuleViolationError("9.5.1.0-2", "only passed and failed may carry a score")
            if "raw" in score and ("min" not in score or "max" not in score):
                raise RuleViolationError("9.5.1.0-3", "a raw score needs min and max")
            if view.mastery_score is not None:
                ext = (st["context"].get("extensions") or {}).get(EXT_MASTERY)
                if not isinstance(ext, int | float) or float(ext) != float(view.mastery_score):
                    raise RuleViolationError(
                        "9.6.3.2-2", "judged against the masteryScore: include it as the masteryscore extension"
                    )
        durations = {TERMINATED: "9.5.4.1-1", COMPLETED: "9.5.4.1-2", PASSED: "9.5.4.1-3", FAILED: "9.5.4.1-4"}
        if verb in durations and "duration" not in result:
            raise RuleViolationError(durations[verb], f"{verb.rsplit('/', 1)[1]} needs result.duration")

    sent = view.sent | set(earlier)
    scaled = ((result or {}).get("score") or {}).get("scaled")
    if verb in (PASSED, FAILED):
        if view.mastery_score is not None and scaled is None:
            raise RuleViolationError("TN-SCORE", "judged against the masteryScore: report score.scaled")
        if view.require_graded:
            if view.graded is None:
                raise RuleViolationError(
                    "TN-GRADE", "no score marked by TrueNorth in this session: submit the quiz first", status=403
                )
            if scaled is None or not math.isfinite(float(scaled)) or abs(float(scaled) - view.graded) > 1e-4:
                raise RuleViolationError(
                    "TN-GRADE", f"score.scaled must be the score TrueNorth marked ({view.graded})", status=403
                )
    if verb in (INITIALIZED, TERMINATED) and verb in sent:
        raise RuleViolationError("9.3.0.0-2", f"{verb.rsplit('/', 1)[1]} already sent in this session")
    if verb == COMPLETED:
        if COMPLETED in sent:
            raise RuleViolationError("9.3.0.0-2", "completed already sent in this session")
        if view.au_completed:
            raise RuleViolationError("9.3.0.0-6", "completed already recorded in this registration")
    if verb == PASSED:
        if view.mastery_score is not None and scaled is not None and scaled < view.mastery_score:
            raise RuleViolationError(
                "9.3.4.0-2", f"passed with a score {scaled} below the masteryScore {view.mastery_score}"
            )
        if PASSED in sent:
            raise RuleViolationError("9.3.0.0-2", "passed already sent in this session")
        if FAILED in sent:
            raise RuleViolationError("9.3.0.0-3", "failed already sent in this session")
        if view.au_passed:
            raise RuleViolationError("9.3.0.0-7", "passed already recorded in this registration")
    if verb == FAILED:
        if view.mastery_score is not None and scaled is not None and scaled >= view.mastery_score:
            raise RuleViolationError(
                "9.3.6.0-1", f"failed with a score {scaled} at or above the masteryScore {view.mastery_score}"
            )
        if FAILED in sent:
            raise RuleViolationError("9.3.0.0-2", "failed already sent in this session")
        if PASSED in sent:
            raise RuleViolationError("9.3.0.0-3", "passed already sent in this session")
        if view.au_passed:
            raise RuleViolationError("9.3.0.0-8", "failed after passed in this registration")
    return verb
