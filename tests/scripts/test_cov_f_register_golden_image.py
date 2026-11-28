# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/register_golden_image_inprocess.py: marking a golden image built — the catalogue
lookup on the hypervisor, the drift-only PATCH of template_name/build_status/datastore, the
unchanged path, the disabled note, a custom variant created from metadata when missing (and
the refusal when it is missing without one), and the build record patched onto the image
without promoting it (ADR 0013). The API is a recording fake; main() runs against a fake
_inprocess_api harness with the build record read from a tmp file."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "cov_f_register_golden_image", ROOT / "scripts" / "register_golden_image_inprocess.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class FakeApi:
    def __init__(self, images=(), create=None, patch_status=200):
        self.images = list(images)
        self.create_body = create
        self.patch_status = patch_status
        self.patches: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        assert path == "/golden-images" and params == {"hypervisor": "vsphere"}
        return 200, self.images

    def post(self, path, json=None, params=None):
        self.posts.append((path, json or {}))
        return 200, self.create_body

    def patch(self, path, json=None):
        self.patches.append((path, json))
        if self.patch_status >= 400:
            return self.patch_status, {"detail": "conflict"}
        image = dict(next((i for i in self.images if str(i.get("id")) == path.rsplit("/", 1)[-1]), {}))
        image.update(json or {})
        return 200, image


def test_register_marks_a_catalogue_image_built_with_only_its_drift(capsys):
    img = {
        "id": "gi-1",
        "catalogue_id": "srv2022",
        "hypervisor": "vsphere",
        "template_name": "old-template",
        "build_status": "built",
        "enabled": False,
    }
    api = FakeApi(images=[img])
    assert mod.register(api, catalogue_id="srv2022", template="TN-srv2022-golden", datastore="datastore1") == 0
    assert api.patches == [("/golden-images/gi-1", {"template_name": "TN-srv2022-golden", "datastore": "datastore1"})]
    out = capsys.readouterr().out
    assert ("golden image: srv2022 -> built (template TN-srv2022-golden; changed datastore, template_name)") in out
    assert "NOTE srv2022 is disabled in the catalogue; ranges will not resolve to it" in out


def test_register_reports_an_image_already_built_unchanged(capsys):
    img = {
        "id": "gi-1",
        "catalogue_id": "srv2022",
        "hypervisor": "vsphere",
        "template_name": "TN-golden",
        "build_status": "built",
    }
    api = FakeApi(images=[img])
    assert mod.register(api, catalogue_id="srv2022", template="TN-golden") == 0
    assert api.patches == [] and api.posts == []
    assert "golden image: unchanged srv2022 (built, template TN-golden)" in capsys.readouterr().out


def test_register_fails_without_the_image_and_without_a_variant(capsys):
    api = FakeApi(images=[{"id": "gi-2", "catalogue_id": "other", "hypervisor": "vsphere"}])
    assert mod.register(api, catalogue_id="srv2022", template="TN-golden") == 1
    out = capsys.readouterr().out
    assert "FAIL no image 'srv2022' on vsphere. Import the catalogue first" in out
    assert api.posts == []


def test_register_creates_a_missing_custom_variant_from_its_metadata(capsys):
    api = FakeApi(create={"id": "gi-new", "catalogue_id": "custom", "build_status": "built"})
    assert (
        mod.register(
            api,
            catalogue_id="custom",
            template="TN-custom",
            status="built",
            datastore="ds-1",
            variant={"os_family": "linux", "version": "24.04", "role": None, "os_aliases": ["ubuntu"]},
        )
        == 0
    )
    ((path, payload),) = api.posts
    assert path == "/golden-images"
    # None values are dropped; enabled defaults to True; the datastore rides along.
    assert payload == {
        "catalogue_id": "custom",
        "hypervisor": "vsphere",
        "template_name": "TN-custom",
        "build_status": "built",
        "enabled": True,
        "os_family": "linux",
        "version": "24.04",
        "os_aliases": ["ubuntu"],
        "datastore": "ds-1",
    }
    assert "golden image: created variant custom -> built (template TN-custom)" in capsys.readouterr().out


def test_register_fails_when_creating_a_variant_is_refused(capsys):
    api = FakeApi()
    api.post = lambda path, json=None, params=None: (422, {"detail": "bad variant"})
    assert mod.register(api, catalogue_id="custom", template="TN-custom", variant={"os_family": "linux"}) == 1
    assert "FAIL create variant 'custom': 422 bad variant" in capsys.readouterr().out


def test_register_fails_when_the_listing_or_the_patch_fails(capsys):
    api = FakeApi()
    api.get = lambda path, params=None: (503, "boom")
    assert mod.register(api, catalogue_id="srv2022", template="t") == 1
    assert "FAIL listing images: 503 boom" in capsys.readouterr().out

    api = FakeApi(images=[{"id": "gi-1", "catalogue_id": "srv2022", "template_name": "old"}])
    api.patch_status = 409
    assert mod.register(api, catalogue_id="srv2022", template="new") == 1
    assert "FAIL update 'srv2022': 409 conflict" in capsys.readouterr().out


def test_build_body_maps_the_build_sh_record():
    record = {
        "build_id": "b-7",
        "checksum": "sha256:aa",
        "build_date": "2026-11-01",
        "iso_checksum": "sha256:bb",
        "template": "TN-golden",
        "copies": {"tn-esx-01": "datastore1"},
    }
    assert mod.build_body(record) == {
        "build_id": "b-7",
        "checksum": "sha256:aa",
        "build_date": "2026-11-01",
        "iso_checksum": "sha256:bb",
        "template_name": "TN-golden",
        "copies": {"tn-esx-01": "datastore1"},
    }
    # Missing optional fields fall back to empty, never KeyError.
    assert mod.build_body({"build_id": "b-8", "checksum": "x"}) == {
        "build_id": "b-8",
        "checksum": "x",
        "build_date": None,
        "iso_checksum": "",
        "template_name": "",
        "copies": {},
    }


def test_record_build_patches_the_build_and_never_promotes(capsys):
    img = {"id": "gi-1", "catalogue_id": "srv2022", "template_name": "TN-golden", "build_status": "built"}
    api = FakeApi(images=[img])
    build = {"build_id": "b-7", "checksum": "x", "copies": {"tn-esx-02": "ds", "tn-esx-01": "ds"}}
    assert mod.register(api, catalogue_id="srv2022", template="TN-golden", build=build) == 0
    assert api.patches == [("/golden-images/gi-1", {"build": build})]
    out = capsys.readouterr().out
    assert (
        "golden image: srv2022 build b-7 recorded (tn-esx-01, tn-esx-02); "
        "an admin promotes it on the Golden Images page before ranges use it" in out
    )

    # No copies yet reads plainly; a failed recording patch is exit 1.
    api = FakeApi(images=[img])
    assert (
        mod.register(api, catalogue_id="srv2022", template="TN-golden", build={"build_id": "b-9", "checksum": "x"}) == 0
    )
    assert "recorded (no host copies yet)" in capsys.readouterr().out

    api = FakeApi(images=[img])
    api.patch_status = 500
    assert (
        mod.register(api, catalogue_id="srv2022", template="TN-golden", build={"build_id": "b-9", "checksum": "x"}) == 1
    )
    assert "FAIL recording build b-9 of 'srv2022': 500" in capsys.readouterr().out


def test_main_reads_the_build_record_and_hands_over_to_register(tmp_path, monkeypatch, capsys):
    record = tmp_path / "b-7.build.json"
    record.write_text(json.dumps({"build_id": "b-7", "checksum": "x"}), encoding="utf-8")
    seen = {}

    class Api(FakeApi):
        def get(self, path, params=None):
            seen["params"] = params
            return 200, []

    harness = types.ModuleType("_inprocess_api")
    harness.connect = lambda admin: (None, Api())
    monkeypatch.setitem(sys.modules, "_inprocess_api", harness)

    argv = ["--admin", "ops@example.test", "--id", "srv2022", "--build-record", str(record)]
    # The listing is empty and no --variant was asked for: not registered is exit 1.
    assert mod.main(argv) == 1
    assert seen["params"] == {"hypervisor": "vsphere"}
    # --id without --template defaults the template name to the catalogue id.
    assert "FAIL no image 'srv2022' on vsphere" in capsys.readouterr().out

    # A broken build record is exit 1 before any API call.
    broken = tmp_path / "broken.build.json"
    broken.write_text("{}", encoding="utf-8")  # no build_id / checksum -> KeyError
    assert mod.main([*argv[:-2], "--build-record", str(broken)]) == 1
    assert f"FAIL reading build record {broken}" in capsys.readouterr().out

    # --variant without --os-family is an argparse error; a missing admin is exit 2.
    with pytest.raises(SystemExit) as exc_info:
        mod.main(["--admin", "ops@example.test", "--id", "srv2022", "--variant"])
    assert exc_info.value.code == 2

    harness.connect = lambda admin: None
    assert mod.main(argv) == 2
