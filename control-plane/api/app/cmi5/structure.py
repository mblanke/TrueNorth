"""cmi5 course structures: read a release's ``cmi5.xml``, serve it with absolute AU URLs,
and name the runtime activities TrueNorth uses when it is the LMS.

The release bundle carries the structure ARC² wrote (``07-bundle/cmi5/cmi5.xml`` in the
learner part), with AU URLs relative to the package root. A bare ``cmi5.xml`` handed to
another LMS must have fully qualified URLs (cmi5 §14), so the served copy points every AU
at TrueNorth's AU runtime in the SPA (``<web>/au/releases/<release>/<index>``) and asks
for ``OwnWindow``: TrueNorth's pages refuse to be framed by another origin
(``frame-ancestors 'self'``) and its sign-in cookie is first-party only. Everything else
(publisher IDs, objectives, moveOn, masteryScore, launchParameters) is left as published.

AUs and blocks are numbered in document order, which is how cmi5 test tooling (CATAPULT)
and LMSs address them ("auIndex").
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET  # building output only; parsing goes through defusedxml
from dataclasses import dataclass, field

from defusedxml import ElementTree as SafeET

from .. import xapi

NS = "https://w3id.org/xapi/profiles/cmi5/v1/CourseStructure.xsd"
Q = f"{{{NS}}}"
BUNDLE_ROOT = "07-bundle/cmi5/"
STRUCTURE_PATH = BUNDLE_ROOT + "cmi5.xml"
MOVE_ON = ("NotApplicable", "Passed", "Completed", "CompletedAndPassed", "CompletedOrPassed")


class StructureError(ValueError):
    """The course structure is not usable; the message says why."""


@dataclass
class AU:
    index: int
    publisher_id: str
    title: dict[str, str]
    description: dict[str, str]
    url: str
    move_on: str = "NotApplicable"
    mastery_score: float | None = None
    launch_method: str = "AnyWindow"
    launch_parameters: str | None = None
    entitlement_key: str | None = None
    blocks: tuple[int, ...] = ()  # the blocks it sits in, outermost first


@dataclass
class Block:
    index: int
    publisher_id: str
    title: dict[str, str]
    children: list[tuple[str, int]] = field(default_factory=list)  # ("au"|"block", index)


@dataclass
class CourseStructure:
    publisher_id: str
    title: dict[str, str]
    description: dict[str, str]
    children: list[tuple[str, int]]
    aus: list[AU]
    blocks: list[Block]


def _langstrings(el: ET.Element | None) -> dict[str, str]:
    if el is None:
        return {}
    out: dict[str, str] = {}
    for ls in el.findall(f"{Q}langstring"):
        out[(ls.get("lang") or "und").strip()] = (ls.text or "").strip()
    return out


def _text(el: ET.Element | None) -> str | None:
    return (el.text or "").strip() if el is not None and el.text is not None else None


def parse(data: bytes) -> CourseStructure:
    """Parse a cmi5 course structure (defusedxml: no entities, no external references)."""
    try:
        root = SafeET.fromstring(data)
    except Exception as exc:  # defusedxml raises its own subclasses as well as ParseError
        raise StructureError(f"cmi5.xml is not well-formed XML: {exc}") from exc
    if root.tag != f"{Q}courseStructure":
        raise StructureError(f"cmi5.xml root is {root.tag!r}, not courseStructure in the cmi5 namespace")
    course = root.find(f"{Q}course")
    if course is None or not (course.get("id") or "").strip():
        raise StructureError("cmi5.xml has no <course id=...>")

    aus: list[AU] = []
    blocks: list[Block] = []

    def walk(parent: ET.Element, path: tuple[int, ...]) -> list[tuple[str, int]]:
        children: list[tuple[str, int]] = []
        for el in parent:
            if el.tag == f"{Q}au":
                move_on = (el.get("moveOn") or "NotApplicable").strip()
                if move_on not in MOVE_ON:
                    raise StructureError(f"AU {el.get('id')!r}: moveOn {move_on!r} is not a cmi5 value")
                mastery = el.get("masteryScore")
                try:
                    mastery_score = float(mastery) if mastery not in (None, "") else None
                except ValueError as exc:
                    raise StructureError(f"AU {el.get('id')!r}: masteryScore {mastery!r} is not a number") from exc
                if mastery_score is not None and not 0.0 <= mastery_score <= 1.0:
                    raise StructureError(f"AU {el.get('id')!r}: masteryScore {mastery_score} is outside 0..1")
                url = _text(el.find(f"{Q}url"))
                if not (el.get("id") or "").strip() or not url:
                    raise StructureError("every AU needs an id and a url")
                au = AU(
                    index=len(aus),
                    publisher_id=el.get("id").strip(),
                    title=_langstrings(el.find(f"{Q}title")),
                    description=_langstrings(el.find(f"{Q}description")),
                    url=url,
                    move_on=move_on,
                    mastery_score=mastery_score,
                    launch_method=(el.get("launchMethod") or "AnyWindow").strip(),
                    launch_parameters=_text(el.find(f"{Q}launchParameters")),
                    entitlement_key=_text(el.find(f"{Q}entitlementKey")),
                    blocks=path,
                )
                aus.append(au)
                children.append(("au", au.index))
            elif el.tag == f"{Q}block":
                if not (el.get("id") or "").strip():
                    raise StructureError("every block needs an id")
                block = Block(
                    index=len(blocks), publisher_id=el.get("id").strip(), title=_langstrings(el.find(f"{Q}title"))
                )
                blocks.append(block)
                block.children = walk(el, (*path, block.index))
                if not block.children:
                    raise StructureError(f"block {block.publisher_id!r} has no AU or block")
                children.append(("block", block.index))
        return children

    top = walk(root, ())
    if not aus:
        raise StructureError("cmi5.xml has no AU")
    ids = [a.publisher_id for a in aus] + [b.publisher_id for b in blocks] + [course.get("id").strip()]
    if len(ids) != len(set(ids)):
        raise StructureError("cmi5.xml repeats a course, block or AU id")
    return CourseStructure(
        publisher_id=course.get("id").strip(),
        title=_langstrings(course.find(f"{Q}title")),
        description=_langstrings(course.find(f"{Q}description")),
        children=top,
        aus=aus,
        blocks=blocks,
    )


# -- where TrueNorth serves things ----------------------------------------------------------
def web_base() -> str:
    """Where Students' browsers reach the SPA (the AU runtime lives there)."""
    return (os.getenv("CMI5_WEB_BASE_URL", "").strip() or xapi.platform_url()).rstrip("/")


def api_base() -> str:
    """Where Students' browsers reach the API: the fetch URL and the LRS endpoint."""
    return (os.getenv("CMI5_API_BASE_URL", "").strip() or f"{web_base()}/api").rstrip("/")


def au_url(release_id, index: int) -> str:
    return f"{web_base()}/au/releases/{release_id}/{index}"


# -- runtime activity ids (cmi5 8.1.5: never the publisher id; the same for every launch) ---
def course_runtime_id(release_id) -> str:
    return f"{xapi.iri_base()}/cmi5/releases/{release_id}"


def au_runtime_id(release_id, index: int) -> str:
    return f"{course_runtime_id(release_id)}/au/{index}"


def block_runtime_id(release_id, index: int) -> str:
    return f"{course_runtime_id(release_id)}/block/{index}"


def served_xml(data: bytes, release_id) -> bytes:
    """The release's cmi5.xml with every AU URL absolute (TrueNorth's AU runtime) and
    ``launchMethod="OwnWindow"``. Parsed and checked first, so a broken structure is refused."""
    parse(data)
    root = SafeET.fromstring(data)
    ET.register_namespace("", NS)
    for index, au in enumerate(root.iter(f"{Q}au")):
        url = au.find(f"{Q}url")
        url.text = au_url(release_id, index)
        au.set("launchMethod", "OwnWindow")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8")
