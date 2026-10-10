"""The Moodle course-publishing seam (ADR 0001).

A backend converges one Moodle course on a TrueNorth payload and reports what Moodle
holds, keyed only by idnumbers TrueNorth chose, so every operation can be repeated and an
interrupted publication can be reconciled by asking rather than remembering.

Payload (``upsert_course``)::

    {"idnumber": "<course uuid> | tn-stage:<release uuid>", "fullname", "shortname",
     "summary", "visible": bool, "category": {"idnumber", "name"},
     "sections": [{"name", "summary", "activities": [
         {"idnumber": "tn:<module>:page:NN", "type": "page", "name", "content", "format": "html"},
         {"idnumber": "tn:<module>:quiz:<hash>", "type": "quiz", "name", "intro", "grade",
          "pass_pct", "questions": [{"text", "answers": [...], "correct": [index, ...]}]},
         {"idnumber": "tn:<module>:file:<hash>", "type": "resource", "name", "intro",
          "filename", "content_b64", "sha1"},
         {"idnumber": "tn:<module>:lab", "type": "lti", "name", "resource", "grade"}]}]}

Results flow back the other way (``pull_results``): TrueNorth asks, Moodle answers with
completions and quiz grades signed by its own LTI key, which TrueNorth already trusts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class MoodleError(Exception):
    """Moodle refused or could not be reached; the message is safe to show staff."""


class BaseMoodleBackend(ABC):
    kind: ClassVar[str]

    @abstractmethod
    def upsert_course(self, platform: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Create or converge the course; returns courseid, activities {idnumber: cmid},
        removed and retired idnumbers."""

    @abstractmethod
    def describe_course(self, platform: Any, idnumber: str) -> dict[str, Any]:
        """{"exists": False} or exists, courseid, visible, sections, ltitool and
        activities {idnumber: {cmid, type, visible, section, questions?}}."""

    @abstractmethod
    def set_visible(self, platform: Any, idnumber: str, visible: bool) -> dict[str, Any]:
        """Show or hide a course; returns courseid (0 when absent)."""

    @abstractmethod
    def delete_stage(self, platform: Any, idnumber: str) -> dict[str, Any]:
        """Delete a ``tn-stage:`` course; any other idnumber is refused."""

    @abstractmethod
    def pull_results(self, platform: Any, cursor: str, limit: int = 500) -> dict[str, Any]:
        """What students did in TrueNorth's courses since ``cursor`` ('' = the beginning).

        Returns ``{"rows": [...], "cursor": str, "more": bool}``, already verified as this
        Moodle's own answer to this request (MoodleError otherwise). A row is one of::

            {"kind": "completion", "user", "course", "activity", "modname", "state", "time"}
            {"kind": "quiz_grade", "user", "course", "activity", "grade", "grademax",
             "gradepass", "attempts", "time"}
            {"kind": "course_completion", "user", "course", "time"}

        ``user`` is the TrueNorth user id (the Moodle account's idnumber, set by TrueNorth
        sign-in), ``course`` the TrueNorth course id, ``activity`` the ``tn:`` idnumber;
        ``state`` is Moodle's completion state (0 incomplete, 1 complete, 2 passed, 3 failed).
        Rows are in change order; pulling again from an earlier cursor repeats rows, so the
        caller records them idempotently."""
