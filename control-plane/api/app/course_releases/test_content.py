"""Test content: releases whose ARC² gates a test host's runner accepted, not a person.

On a test host (staging) the ARC² runner accepts the outline and preview gates itself
(``ARC2_AUTO_ACCEPT_GATES``, tools/arc2/runner.py). What it produces was never reviewed, so
it carries ``auto_accepted`` in its release.json (under the release digest) and:

* is accepted or published only where ``TN_TEST_HOST`` is on; the installer sets it only
  on hosts with ``tn_arc2_auto_accept: true``. Anywhere else the API refuses it.
* where allowed, is labelled wherever Students see it: ``course_meta.test_content`` on the
  course (the course page shows it as a banner) and the Moodle course name and summary.

The authority for ``auto_accepted`` is the runner's own job record (``<runs>/_jobs``),
which a job cannot write, not the run's manifest, which it can: release.py reads the
runner record, and :func:`runner_says_auto_accepted` checks an upload against it.
"""

from __future__ import annotations

import os
from typing import Any

LABEL = "TEST CONTENT — not reviewed"
SUMMARY = "TEST CONTENT — not reviewed by a person. Generated on a test host; not for Students."
TEST_HOST_ENV = "TN_TEST_HOST"


def is_test_host() -> bool:
    return os.getenv(TEST_HOST_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def refusal(what: str) -> str:
    return (
        f"this release is test content (its ARC² gates were accepted by a test host's runner, not a "
        f"person); it can be {what} only on a test host ({TEST_HOST_ENV})"
    )


def runner_says_auto_accepted(slug: str) -> bool:
    """True when the ARC² runner on this host accepted a gate of run ``slug`` by itself.
    False when the Studio is off here or the run is not on this host."""
    from ..routers import arc2_studio

    if not arc2_studio.enabled() or not arc2_studio.SLUG_RE.match(slug or ""):
        return False
    return any(isinstance(j.get("auto_accept"), dict) for j in arc2_studio._jobs(slug))


def course_label(release_id: Any, version: int, meta: dict[str, Any]) -> dict[str, Any]:
    """What ``course_meta.test_content`` holds while the course delivers test content."""
    auto = meta.get("auto_accept") or {}
    return {
        "label": LABEL,
        "release_id": str(release_id),
        "version": version,
        "gates": list(auto.get("gates") or []),
        "accepted_by": auto.get("accepted_by") or "auto (test host)",
    }
