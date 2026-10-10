"""The cmi5 course structure TrueNorth serves (GET /cmi5/releases/{id}/cmi5.xml) and the
parser behind it (app/cmi5/structure.py).

The served file must be importable by another LMS on its own: valid against the cmi5
CourseStructure.xsd (vendored, docs/interfaces/cmi5/), every AU URL fully qualified
(cmi5 §14), and nothing the publisher wrote changed except the URLs and launchMethod.
"""

from __future__ import annotations

import uuid

import pytest
from _cmi5_kit import BUNDLE, CATALOGUE, CROSSWALK, XSD
from app.cmi5 import structure
from app.course_releases import bundle as bundle_mod
from lxml import etree

NS = {"c": structure.NS}


def _schema() -> etree.XMLSchema:
    return etree.XMLSchema(etree.parse(str(XSD), parser=etree.XMLParser(no_network=True, resolve_entities=False)))


def _bundle_xml() -> bytes:
    return bundle_mod.parse(BUNDLE.read_bytes()).files["learner"][structure.STRUCTURE_PATH]


@pytest.fixture
def served(client):
    client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", CROSSWALK.read_bytes(), "text/csv")})
    client.post("/courses/import-programme", files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")})
    rel = client.post("/course-releases", files={"file": (BUNDLE.name, BUNDLE.read_bytes(), "application/gzip")}).json()
    r = client.get(f"/cmi5/releases/{rel['id']}/cmi5.xml")  # the dev admin holds course:author
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/xml")
    return rel, r.content


def test_the_release_structure_is_valid_as_published():
    _schema().assertValid(etree.fromstring(_bundle_xml()))


def test_the_served_structure_is_valid_and_fully_qualified(served):
    rel, xml = served
    doc = etree.fromstring(xml)
    _schema().assertValid(doc)
    aus = doc.findall(".//c:au", NS)
    assert len(aus) == 6
    for i, au in enumerate(aus):
        assert au.findtext("c:url", namespaces=NS) == f"http://localhost:4200/au/releases/{rel['id']}/{i}"
        assert au.get("launchMethod") == "OwnWindow"


def test_serving_changes_only_urls_and_launch_method(served):
    _, xml = served
    original, out = etree.fromstring(_bundle_xml()), etree.fromstring(xml)
    for el in (original, out):
        for au in el.iter(f"{{{structure.NS}}}au"):
            au.find("c:url", NS).text = "URL"
            au.set("launchMethod", "X")
    assert etree.tostring(original, method="c14n") == etree.tostring(out, method="c14n")


def test_parse_numbers_aus_and_blocks_in_document_order():
    parsed = structure.parse(_bundle_xml())
    assert parsed.publisher_id == "https://ccoe.forces.gc.ca/xapi/arc2/arc2-c105"
    assert [a.index for a in parsed.aus] == list(range(6))
    assert [a.blocks for a in parsed.aus] == [(i,) for i in range(6)]
    assert parsed.children == [("block", i) for i in range(6)]
    assert {a.move_on for a in parsed.aus} == {"Passed"} and {a.mastery_score for a in parsed.aus} == {0.7}
    assert parsed.aus[0].url == "mod_001/index.html"


def test_runtime_ids_are_never_publisher_ids():
    parsed = structure.parse(_bundle_xml())
    rid = uuid.uuid4()
    runtime = {structure.course_runtime_id(rid)}
    runtime |= {structure.au_runtime_id(rid, a.index) for a in parsed.aus}
    runtime |= {structure.block_runtime_id(rid, b.index) for b in parsed.blocks}
    published = {parsed.publisher_id} | {a.publisher_id for a in parsed.aus} | {b.publisher_id for b in parsed.blocks}
    assert len(runtime) == 1 + 6 + 6 and not runtime & published


def _doc(body: str) -> bytes:
    return (
        f'<courseStructure xmlns="{structure.NS}"><course id="https://x/c"><title><langstring lang="en">C</langstring>'
        f'</title><description><langstring lang="en">C</langstring></description></course>{body}</courseStructure>'
    ).encode()


AU = '<au id="{id}" moveOn="{m}"><title><langstring lang="en">A</langstring></title><description><langstring lang="en">A</langstring></description><url>a/index.html</url></au>'


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (b"<not-xml", "well-formed"),
        (b'<courseStructure xmlns="urn:other"/>', "namespace"),
        (_doc(""), "no AU"),
        (_doc(AU.format(id="https://x/a", m="Sometimes")), "moveOn"),
        (_doc(AU.format(id="https://x/a", m="Passed") * 2), "repeats"),
        (
            _doc(
                '<block id="https://x/b"><title><langstring lang="en">B</langstring></title><description><langstring lang="en">B</langstring></description></block>'
            ),
            "no AU or block",
        ),
        (
            b'<?xml version="1.0"?><!DOCTYPE c [<!ENTITY a "aaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
            + _doc(AU.format(id="https://x/&b;", m="Passed")),
            "",  # entities: defusedxml refuses the document outright
        ),
    ],
)
def test_parse_refuses_broken_structures(data, why):
    with pytest.raises(structure.StructureError, match=why):
        structure.parse(data)


def test_api_base_and_web_base_are_configurable(monkeypatch):
    monkeypatch.setenv("CMI5_WEB_BASE_URL", "https://range.example.mil/")
    assert structure.api_base() == "https://range.example.mil/api"
    assert structure.au_url("r", 2) == "https://range.example.mil/au/releases/r/2"
    monkeypatch.setenv("CMI5_API_BASE_URL", "https://api.range.example.mil")
    assert structure.api_base() == "https://api.range.example.mil"
