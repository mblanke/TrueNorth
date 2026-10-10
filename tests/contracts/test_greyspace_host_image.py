"""The greyspace-host golden image pre-loads exactly the images the stack runs (ADR 0007).

A gs-core VM has no internet: an image the generator names but the Packer role did not
pull (or a digest bumped in one place only) is a stack that never starts on vSphere.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.greyspace import config, npc

ROOT = Path(__file__).resolve().parents[2]
ROLE = (ROOT / "infra/vsphere/packer/files/linux/roles/greyspace-host.sh").read_text()


def test_every_pinned_image_is_pulled_by_its_digest():
    pinned = set(re.findall(r'^\s+"([^"]+@sha256:[0-9a-f]{64})"$', ROLE, re.M))
    assert pinned == {*config.IMAGES.values(), npc.IMAGE}


def test_every_local_image_is_built_with_the_stack_tag():
    local = re.search(r"^LOCAL=\(([^)]*)\)$", ROLE, re.M).group(1).split()
    assert tuple(local) == config.LOCAL_IMAGES
    assert f"truenorth/greyspace-${{name}}:{config.LOCAL_TAG}" in ROLE
    for name in local:
        assert (ROOT / "greyspace/images" / name / "Dockerfile").is_file()


def test_the_packer_build_has_the_source_and_the_role():
    hcl = (ROOT / "infra/vsphere/packer/derived.pkr.hcl").read_text()
    assert 'source "vsphere-clone" "greyspace-host"' in hcl
    assert "files/linux/roles/greyspace-host.sh" in hcl
    assert "greyspace-host" in (ROOT / "infra/vsphere/packer/build.sh").read_text()
