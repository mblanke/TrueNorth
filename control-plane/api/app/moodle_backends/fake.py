"""An in-memory Moodle with the plugin's semantics, for tests and offline development.

It keeps one store per Moodle site (``lti_issuer``), so separate platforms never share
courses. ``fail_next`` makes the next call of an operation raise, which is how tests
interrupt a publication at a chosen step; ``record_attempt`` marks an activity as used by a
student, after which removing it hides it instead of deleting it, as the plugin does.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, ClassVar

from .base import BaseMoodleBackend, MoodleError

STAGE_PREFIX = "tn-stage:"
_SITES: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
_FAIL_NEXT: dict[str, int] = defaultdict(int)
_CALLS: list[tuple[str, str]] = []
_RESULTS: dict[str, list[dict[str, Any]]] = defaultdict(list)  # per site, in change order


def reset() -> None:
    _SITES.clear()
    _FAIL_NEXT.clear()
    _CALLS.clear()
    _RESULTS.clear()


def record_result(platform: Any, **row: Any) -> None:
    """What a student did in this Moodle, as ``pull_results`` will report it (see base)."""
    row.setdefault("time", 1_760_000_000 + len(_RESULTS[_key(platform)]))
    _RESULTS[_key(platform)].append(row)


def _key(platform: Any) -> str:
    return (platform.lti_issuer or platform.base_url or "").rstrip("/")


def fail_next(op: str, times: int = 1) -> None:
    _FAIL_NEXT[op] += times


def calls() -> list[tuple[str, str]]:
    return list(_CALLS)


def site(platform: Any) -> dict[str, dict[str, Any]]:
    return _SITES[_key(platform)]


def record_attempt(platform: Any, course_idnumber: str, activity_idnumber: str) -> None:
    site(platform)[course_idnumber]["activities"][activity_idnumber]["used"] = True


class FakeMoodle(BaseMoodleBackend):
    kind: ClassVar[str] = "fake"
    _next_id = 100

    def __init__(self, key_provider: Any = None, transport: Any = None):
        pass

    def _enter(self, op: str, idnumber: str) -> None:
        _CALLS.append((op, idnumber))
        if _FAIL_NEXT[op] > 0:
            _FAIL_NEXT[op] -= 1
            raise MoodleError(f"Moodle could not be reached: injected failure in {op}")

    @classmethod
    def _id(cls) -> int:
        cls._next_id += 1
        return cls._next_id

    def upsert_course(self, platform: Any, payload: dict[str, Any]) -> dict[str, Any]:
        self._enter("upsert_course", payload["idnumber"])
        courses = site(platform)
        course = courses.get(payload["idnumber"])
        created = course is None
        if created:
            course = {"courseid": self._id(), "activities": {}}
            courses[payload["idnumber"]] = course
        course.update(
            fullname=payload["fullname"],
            visible=1 if payload.get("visible") else 0,
            sections=len(payload["sections"]),
            payload=copy.deepcopy(payload),
        )
        wanted: dict[str, dict[str, Any]] = {}
        for num, section in enumerate(payload["sections"], start=1):
            for a in section.get("activities") or []:
                wanted[a["idnumber"]] = dict(a, section=num)
        existing = course["activities"]
        out: dict[str, int] = {}
        for idn, a in wanted.items():
            act = existing.get(idn)
            if act is None or act["type"] != a["type"]:
                act = {"cmid": self._id(), "type": a["type"], "used": False}
                existing[idn] = act
            act.update(visible=1, section=a["section"])
            if a["type"] == "quiz":
                act["questions"] = len(a["questions"])
            if a["type"] == "page":
                act["content_length"] = len(a.get("content") or "")
            if a["type"] == "resource":
                act["sha1"] = a["sha1"]
            out[idn] = act["cmid"]
        removed, retired = [], []
        for idn in [i for i in existing if i not in wanted]:
            if existing[idn].get("used"):
                existing[idn]["visible"] = 0
                retired.append(idn)
            else:
                del existing[idn]
                removed.append(idn)
        return {
            "courseid": course["courseid"],
            "created": created,
            "activities": out,
            "removed": removed,
            "retired": retired,
        }

    def describe_course(self, platform: Any, idnumber: str) -> dict[str, Any]:
        self._enter("describe_course", idnumber)
        course = site(platform).get(idnumber)
        if course is None:
            return {"exists": False}
        acts = {
            idn: {
                k: v for k, v in a.items() if k in ("cmid", "type", "visible", "section", "questions", "content_length", "sha1")
            }
            for idn, a in course["activities"].items()
        }
        return {
            "exists": True,
            "courseid": course["courseid"],
            "visible": course["visible"],
            "sections": course["sections"],
            "activities": acts,
            "ltitool": True,
        }

    def set_visible(self, platform: Any, idnumber: str, visible: bool) -> dict[str, Any]:
        self._enter("set_visible", idnumber)
        course = site(platform).get(idnumber)
        if course is not None:
            course["visible"] = 1 if visible else 0
        return {"courseid": course["courseid"] if course else 0}

    def delete_stage(self, platform: Any, idnumber: str) -> dict[str, Any]:
        self._enter("delete_stage", idnumber)
        if not idnumber.startswith(STAGE_PREFIX):
            raise MoodleError("Moodle refused delete_stage: syncnotstage")
        return {"deleted": site(platform).pop(idnumber, None) is not None}

    def pull_results(self, platform: Any, cursor: str, limit: int = 500) -> dict[str, Any]:
        """The cursor is the count of rows already read ('' = none)."""
        self._enter("pull_results", cursor)
        rows = _RESULTS[_key(platform)]
        start = int(cursor) if cursor.isdigit() else 0
        page = rows[start : start + limit]
        return {
            "rows": copy.deepcopy(page),
            "cursor": str(start + len(page)) if (start or page) else cursor,
            "more": start + len(page) < len(rows),
        }
