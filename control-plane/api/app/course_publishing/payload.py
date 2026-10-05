"""The Moodle course a release becomes (pure: release bundle in, payload out).

    release module mod_NNN       -> section "N. <title>", summary = its objectives
      learner page page-NN.html  -> page     tn:mod_NNN:page:NN   (HTML as authored)
      quiz                       -> quiz     tn:mod_NNN:quiz:<hash of the questions>
      range activity             -> LTI link tn:mod_NNN:lab, resource lab:<course>:mod_NNN

Activity idnumbers depend on the module, not the release, so publishing a new release
converges the same Moodle activities; a quiz is named by its questions, so changed
questions are a new quiz and the old one (with its attempts) is kept, hidden. A lab link
names the course, not the release: the launch resolves the student's pinned release
(app/lab_sessions), so publishing a new release never re-points a lab a student is in.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from html import unescape
from typing import Any

from ..course_releases.bundle import Bundle

STAGE_PREFIX = "tn-stage:"
_H2 = re.compile(r"<h[12][^>]*>(.*?)</h[12]>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")


def stage_idnumber(release_id: uuid.UUID) -> str:
    return f"{STAGE_PREFIX}{release_id}"


def _modules(bundle: Bundle) -> list[dict[str, Any]]:
    manifest = json.loads(bundle.files["platform"]["manifest.json"])
    ids = {m["ordinal"]: m["id"] for m in (manifest.get("content") or {}).get("modules") or []}
    out = []
    for m in bundle.course["modules"]:
        mid = ids.get(m["ordinal"], f"mod_{m['ordinal']:03d}")
        out.append({**m, "id": mid})
    return out


def _page_title(html: str, fallback: str) -> str:
    match = _H2.search(html)
    title = unescape(_TAGS.sub("", match.group(1))).strip() if match else ""
    return (title or fallback)[:250]


def quiz_hash(questions: list[dict[str, Any]], pass_pct: int) -> str:
    """Names a quiz by everything a student's attempt was graded against."""
    return hashlib.sha256(json.dumps([questions, pass_pct], sort_keys=True).encode()).hexdigest()[:12]


def build(
    bundle: Bundle,
    *,
    release_id: uuid.UUID,
    course_id: uuid.UUID,
    idnumber: str,
    visible: bool,
    category: dict[str, str],
    version: int,
) -> dict[str, Any]:
    code = bundle.meta["catalogue_code"]
    stage = idnumber.startswith(STAGE_PREFIX)
    learner = bundle.files["learner"]
    sections = []
    for m in _modules(bundle):
        activities: list[dict[str, Any]] = []
        prefix = f"02-content/{m['id']}/content/"
        pages = sorted(p for p in learner if p.startswith(prefix) and p.endswith(".html"))
        for n, path in enumerate(pages, start=1):
            html = learner[path].decode("utf-8")
            activities.append(
                {
                    "idnumber": f"tn:{m['id']}:page:{n:02d}",
                    "type": "page",
                    "name": _page_title(html, f"{m['title']} ({n})"),
                    "content": html,
                    "format": "html",
                }
            )
        if m["questions"]:
            questions = [{"text": q["stem"], "answers": q["options"], "correct": q["correct"]} for q in m["questions"]]
            activities.append(
                {
                    "idnumber": f"tn:{m['id']}:quiz:{quiz_hash(questions, m['quiz_pass'])}",
                    "type": "quiz",
                    "name": m["quiz_title"] or f"{m['title']} quiz",
                    "intro": f"Pass mark {m['quiz_pass']}%.",
                    "grade": 100,
                    "pass_pct": m["quiz_pass"],
                    "questions": questions,
                }
            )
        if m["activity"] == "range":
            activities.append(
                {
                    "idnumber": f"tn:{m['id']}:lab",
                    "type": "lti",
                    "name": f"Start lab: {m['title']}",
                    "intro": m["lab"],
                    "resource": f"lab:{course_id}:{m['id']}",
                    "grade": 100,
                }
            )
        summary = "\n".join(f"- {o}" for o in m["objectives"])
        sections.append({"name": f"{m['ordinal']}. {m['title']}"[:255], "summary": summary, "activities": activities})
    title = bundle.meta["title"]
    return {
        "idnumber": idnumber,
        "fullname": f"{code} {title}" + (f" (staging v{version})" if stage else ""),
        "shortname": f"{code}" + (f" stage v{version}" if stage else ""),
        "summary": f"{title}. Release v{version}.",
        "visible": visible,
        "category": category,
        "sections": sections,
    }


def expected(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """What a describe of the converged course must show, activity by activity."""
    out = {}
    for num, section in enumerate(payload["sections"], start=1):
        for a in section["activities"]:
            want: dict[str, Any] = {"type": a["type"], "section": num}
            if a["type"] == "quiz":
                want["questions"] = len(a["questions"])
            if a["type"] == "page" and a.get("content", "").strip():
                want["content"] = True  # Moodle must hold a non-empty page body
            out[a["idnumber"]] = want
    return out


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
