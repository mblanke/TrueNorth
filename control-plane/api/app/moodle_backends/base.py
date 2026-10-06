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
