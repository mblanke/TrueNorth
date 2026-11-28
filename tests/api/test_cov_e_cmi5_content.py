# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Round-2 coverage for app/cmi5/content.py: what an AU serves and how its quiz is marked.

``test_cmi5_lms.py`` drives the happy path over HTTP; this file works one level down, on
``au_content``/``grade`` directly, so every refusal names its own branch: AUs hosted
elsewhere or escaping the package, missing/unparseable course-config.json, pages that
leave their folder, and quizless AUs. The real C105 release marks the quizzes; the
refusals run on a minimal synthetic structure. Nothing is stubbed.
"""

from __future__ import annotations

import json
import posixpath

import pytest
from _cmi5_kit import BUNDLE
from app.cmi5 import content as content_mod
from app.cmi5 import structure as structure_mod
from app.course_releases import bundle as bundle_mod

# C105: six AUs, one per block (tests/api/_cmi5_kit.py docstring)


@pytest.fixture(scope="module")
def bundle() -> bundle_mod.Bundle:
    return bundle_mod.parse(BUNDLE.read_bytes())


@pytest.fixture(scope="module")
def parsed(bundle) -> structure_mod.CourseStructure:
    return structure_mod.parse(bundle.files["learner"][structure_mod.STRUCTURE_PATH])


def _structure(*au_urls: str) -> bytes:
    """A minimal cmi5.xml in the cmi5 namespace: one <au> per URL, document order = index."""
    aus = "".join(
        f'<au id="au-{i}">'
        f'<title><langstring lang="en">AU {i}</langstring></title>'
        f'<description><langstring lang="en">synthetic AU {i}</langstring></description>'
        f"<url>{url}</url>"
        "</au>"
        for i, url in enumerate(au_urls)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<courseStructure xmlns="{structure_mod.NS}">'
        '<course id="course-1"><title><langstring lang="en">Synthetic course</langstring></title></course>'
        f"{aus}"
        "</courseStructure>"
    ).encode()


def _synthetic(*au_urls: str, learner: dict[str, bytes] | None = None):
    files = {structure_mod.STRUCTURE_PATH: _structure(*au_urls), **(learner or {})}
    bundle = bundle_mod.Bundle(meta={}, files={"learner": files}, sha256="0" * 64)
    return bundle, structure_mod.parse(files[structure_mod.STRUCTURE_PATH])


CONFIG_PATH = "07-bundle/cmi5/mod_001/course-config.json"
PAGE_PATH = "07-bundle/cmi5/mod_001/intro.html"


def _configured(cfg: dict) -> tuple[bundle_mod.Bundle, structure_mod.CourseStructure]:
    """One packaged AU whose course-config.json is exactly ``cfg``."""
    return _synthetic(
        "mod_001/index.html",
        learner={CONFIG_PATH: json.dumps(cfg).encode(), PAGE_PATH: b"<p>hello</p>"},
    )


def _c105_folder(parsed, index: int) -> str:
    """The 07-bundle folder of a C105 AU, derived from the AU's own url."""
    url = parsed.aus[index].url.split("?", 1)[0]
    return posixpath.dirname(posixpath.normpath(url))


# -- the AU runtime view ----------------------------------------------------------------------


def test_the_c105_au_runtime_sees_pages_and_questions_never_answers(bundle, parsed):
    out = content_mod.au_content(bundle, parsed, 0)

    assert out["index"] == 0
    assert out["move_on"] == "Passed" and out["mastery_score"] == 0.7
    assert out["pages"], "the release ships pages"
    for page in out["pages"]:
        assert set(page) == {"path", "html"}
    assert out["quiz"] is not None and out["quiz"]["title"]
    questions = out["quiz"]["questions"]
    assert questions and all(set(q) == {"id", "stem", "options"} for q in questions)


def test_a_quizless_config_serves_no_quiz_and_falls_back_to_the_cmi5_title():
    bundle, parsed = _synthetic("mod_001/index.html", learner={CONFIG_PATH: b"{}"})
    out = content_mod.au_content(bundle, parsed, 0)

    assert out["title"] == "AU 0"  # from cmi5.xml, the config has none
    assert out["lang"] == "en"
    assert out["pages"] == []
    assert out["quiz"] is None


def test_a_page_outside_the_au_folder_is_refused():
    bundle, parsed = _configured({"content": {"pages": ["../mod_002/leak.html"]}})
    with pytest.raises(content_mod.ContentError) as refused:
        content_mod.au_content(bundle, parsed, 0)
    assert str(refused.value) == "AU 0: page '../mod_002/leak.html' leaves its folder"


def test_a_page_missing_from_the_release_is_refused():
    bundle, parsed = _configured({"content": {"pages": ["ghost.html"]}})
    with pytest.raises(content_mod.ContentError) as refused:
        content_mod.au_content(bundle, parsed, 0)
    assert str(refused.value) == "AU 0: page 'ghost.html' is missing from the release"


def test_pages_inside_the_folder_are_served_verbatim():
    bundle, parsed = _configured({"content": {"pages": ["intro.html"]}})
    out = content_mod.au_content(bundle, parsed, 0)
    assert out["pages"] == [{"path": "intro.html", "html": "<p>hello</p>"}]


# -- AU addressing -----------------------------------------------------------------------------


def test_an_au_index_beyond_the_course_is_refused_with_the_course_size(parsed):
    for bad in (len(parsed.aus), -1):
        with pytest.raises(content_mod.ContentError) as refused:
            content_mod.au(parsed, bad)
        assert str(refused.value) == f"no AU {bad}: the course has {len(parsed.aus)}"
    assert content_mod.au(parsed, 0) is parsed.aus[0]


def test_an_au_hosted_elsewhere_is_refused():
    bundle, parsed = _synthetic("https://cdn.example.org/au.html", "/abs/au.html")
    with pytest.raises(content_mod.ContentError) as remote:
        content_mod.au_content(bundle, parsed, 0)
    assert str(remote.value) == (
        "AU 0 is hosted elsewhere (https://cdn.example.org/au.html); TrueNorth serves only packaged AUs"
    )
    with pytest.raises(content_mod.ContentError) as absolute:
        content_mod.au_content(bundle, parsed, 1)
    assert str(absolute.value) == "AU 1 is hosted elsewhere (/abs/au.html); TrueNorth serves only packaged AUs"


def test_an_au_url_that_leaves_the_package_is_refused():
    bundle, parsed = _synthetic("../escape/au.html")
    with pytest.raises(content_mod.ContentError) as refused:
        content_mod.au_content(bundle, parsed, 0)
    assert str(refused.value) == "AU 0 url leaves the package: ../escape/au.html"


# -- course-config.json -------------------------------------------------------------------------


def test_an_au_without_a_course_config_names_its_folder():
    bundle, parsed = _synthetic("mod_999/index.html")
    with pytest.raises(content_mod.ContentError) as refused:
        content_mod.au_content(bundle, parsed, 0)
    assert str(refused.value) == f"AU 0 has no course-config.json in {structure_mod.BUNDLE_ROOT}mod_999/"


def test_a_config_that_is_not_json_is_refused():
    bundle, parsed = _synthetic("mod_001/index.html", learner={CONFIG_PATH: b"{oops"})
    with pytest.raises(content_mod.ContentError) as refused:
        content_mod.au_content(bundle, parsed, 0)
    assert str(refused.value) == "AU 0: course-config.json is not JSON"


def test_a_config_that_is_not_an_object_is_refused():
    bundle, parsed = _synthetic("mod_001/index.html", learner={CONFIG_PATH: b"[]"})
    with pytest.raises(content_mod.ContentError) as refused:
        content_mod.au_content(bundle, parsed, 0)
    assert str(refused.value) == "AU 0: course-config.json is not an object"


# -- marking ------------------------------------------------------------------------------------


def test_the_c105_quiz_marks_full_partial_and_unknown_submissions(bundle, parsed):
    config = json.loads(
        bundle.files["learner"][f"{structure_mod.BUNDLE_ROOT}{_c105_folder(parsed, 0)}/course-config.json"]
    )
    key = [(str(q["id"]), str(q["answer"]).strip().upper()) for q in config["quiz"]["questions"]]
    total = len(key)

    perfect = dict(key)
    assert content_mod.grade(bundle, parsed, 0, perfect) == {"correct": total, "total": total, "scaled": 1.0}

    # case and padding are the student's, not the answer key's: still correct
    sloppy = {qid: f" {letter.lower()} " for qid, letter in key}
    assert content_mod.grade(bundle, parsed, 0, sloppy) == {"correct": total, "total": total, "scaled": 1.0}

    one_wrong = {qid: ("A" if letter != "A" else "B") if i == 0 else letter for i, (qid, letter) in enumerate(key)}
    assert content_mod.grade(bundle, parsed, 0, one_wrong)["correct"] == total - 1
    assert content_mod.grade(bundle, parsed, 0, one_wrong)["scaled"] == round((total - 1) / total, 4)

    assert content_mod.grade(bundle, parsed, 0, {"ghost": "A"}) == {"correct": 0, "total": total, "scaled": 0.0}


def test_a_quizless_au_cannot_be_marked():
    bundle, parsed = _configured({"title": "Reading only", "lang": "en"})
    with pytest.raises(content_mod.ContentError) as refused:
        content_mod.grade(bundle, parsed, 0, {})
    assert str(refused.value) == "AU 0 has no quiz"
