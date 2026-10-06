"""The lab-profile rules exist twice: tools/arc2 (QA of a run) and the API (verification of a
release and, later, of each provisioned lab). They must be the same bytes, or a profile that
passed QA could be refused at upload, or worse, accepted at upload and refused at launch."""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/arc2"
API = ROOT / "control-plane/api/app/course_releases"


@pytest.mark.parametrize("name", ["lab_profile.py", "lab_profile.schema.json"])
def test_the_api_copy_matches_the_tool(name):
    assert (API / name).read_bytes() == (TOOL / name).read_bytes(), (
        f"{API / name} drifted from {TOOL / name}; copy the tool's file over (cp {TOOL / name} {API / name})"
    )
