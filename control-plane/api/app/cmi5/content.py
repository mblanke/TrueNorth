"""What an AU shows and how its quiz is marked, read from the release's learner part.

The ARC² package puts each AU in its own folder (``07-bundle/cmi5/mod_NNN/``) with a
``course-config.json`` (schema ``arc2/course-config/0.1``): pages, and a formative quiz with
its answers. TrueNorth's AU runtime gets the pages and the questions from the API, never
the answers: the quiz is marked here and only the score goes back. (The static package
handed to other LMSs marks its quiz in the browser; that is ARC²'s documented trade-off,
not this one.)

Bundles are immutable and content-addressed, so a parsed bundle is cached per blob.
"""

from __future__ import annotations

import json
import posixpath
import threading
from collections import OrderedDict
from typing import Any

from sqlalchemy.orm import Session

from ..course_releases import bundle as bundle_mod
from ..course_releases.models import CourseRelease
from ..course_releases.service import load_bundle
from . import structure as structure_mod

_CACHE: OrderedDict[str, tuple[bundle_mod.Bundle, structure_mod.CourseStructure]] = OrderedDict()
_CACHE_SIZE = 4
_LOCK = threading.Lock()


class ContentError(ValueError):
    """The release has no usable cmi5 package (or not this AU); the message says why."""


def package(db: Session, release: CourseRelease) -> tuple[bundle_mod.Bundle, structure_mod.CourseStructure]:
    """The release's verified bundle and parsed cmi5 structure (cached by blob sha256)."""
    with _LOCK:
        hit = _CACHE.get(release.blob_sha256)
        if hit is not None:
            _CACHE.move_to_end(release.blob_sha256)
            return hit
    bundle = load_bundle(db, release)
    learner = bundle.files.get("learner", {})
    if structure_mod.STRUCTURE_PATH not in learner:
        raise ContentError(f"release {release.id} carries no {structure_mod.STRUCTURE_PATH}")
    try:
        parsed = structure_mod.parse(learner[structure_mod.STRUCTURE_PATH])
    except structure_mod.StructureError as exc:
        raise ContentError(str(exc)) from exc
    with _LOCK:
        _CACHE[release.blob_sha256] = (bundle, parsed)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return bundle, parsed


def _au_dir(au: structure_mod.AU) -> str:
    url = au.url.split("?", 1)[0]
    if "://" in url or url.startswith("/"):
        raise ContentError(f"AU {au.index} is hosted elsewhere ({au.url}); TrueNorth serves only packaged AUs")
    folder = posixpath.dirname(posixpath.normpath(url))
    if folder.startswith(".."):
        raise ContentError(f"AU {au.index} url leaves the package: {au.url}")
    return structure_mod.BUNDLE_ROOT + (folder + "/" if folder else "")


def _config(bundle: bundle_mod.Bundle, au: structure_mod.AU) -> tuple[str, dict[str, Any]]:
    folder = _au_dir(au)
    raw = bundle.files["learner"].get(folder + "course-config.json")
    if raw is None:
        raise ContentError(f"AU {au.index} has no course-config.json in {folder}")
    try:
        cfg = json.loads(raw)
    except ValueError as exc:
        raise ContentError(f"AU {au.index}: course-config.json is not JSON") from exc
    if not isinstance(cfg, dict):
        raise ContentError(f"AU {au.index}: course-config.json is not an object")
    return folder, cfg


def au(parsed: structure_mod.CourseStructure, index: int) -> structure_mod.AU:
    if not 0 <= index < len(parsed.aus):
        raise ContentError(f"no AU {index}: the course has {len(parsed.aus)}")
    return parsed.aus[index]


def au_content(bundle: bundle_mod.Bundle, parsed: structure_mod.CourseStructure, index: int) -> dict[str, Any]:
    """Pages and quiz questions for the AU runtime. No answers."""
    unit = au(parsed, index)
    folder, cfg = _config(bundle, unit)
    pages = []
    for rel in (cfg.get("content") or {}).get("pages") or []:
        path = posixpath.normpath(folder + rel)
        if not path.startswith(folder):
            raise ContentError(f"AU {index}: page {rel!r} leaves its folder")
        raw = bundle.files["learner"].get(path)
        if raw is None:
            raise ContentError(f"AU {index}: page {rel!r} is missing from the release")
        pages.append({"path": rel, "html": raw.decode("utf-8", errors="replace")})
    quiz = cfg.get("quiz") or None
    questions = []
    if quiz:
        for q in quiz.get("questions") or []:
            questions.append(
                {"id": str(q.get("id")), "stem": str(q.get("stem", "")), "options": list(q.get("options") or [])}
            )
    return {
        "index": index,
        "title": cfg.get("title") or next(iter(unit.title.values()), ""),
        "lang": cfg.get("lang") or next(iter(unit.title), None),
        "move_on": unit.move_on,
        "mastery_score": unit.mastery_score,
        "pages": pages,
        "quiz": {"title": quiz.get("title") or "Quiz", "questions": questions} if questions else None,
    }


LETTERS = "ABCDEFGH"


def grade(
    bundle: bundle_mod.Bundle, parsed: structure_mod.CourseStructure, index: int, answers: dict[str, str]
) -> dict:
    """Mark the AU's quiz. Answers are option letters (A, B, ...) keyed by question id."""
    _, cfg = _config(bundle, au(parsed, index))
    questions = ((cfg.get("quiz") or {}).get("questions")) or []
    if not questions:
        raise ContentError(f"AU {index} has no quiz")
    correct = sum(
        1
        for q in questions
        if str(answers.get(str(q.get("id")), "")).strip().upper() == str(q.get("answer", "")).upper()
    )
    total = len(questions)
    return {"correct": correct, "total": total, "scaled": round(correct / total, 4)}
