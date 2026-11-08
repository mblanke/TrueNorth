"""COV-I: scripts/lab/choco-internalize.py — the edges test_choco_internalize.py stops short of.

Whole-slice coverage before this file: 75.5% (372 statements). Missing were: the remaining
definition-schema refusals (not a mapping, bad exit codes, a blank/typed software_name, an
installer that is not a mapping, a duplicate file, uninstall on zip and its malformed
shapes), an unreadable definition file, package_version's own refusal, write-back stopping
at the next installer, the real choco_prefetch load, the Nexus auth header, sha256_file,
fetch_installer's reuse branch, the raw repo's has/upload, hosted_versions' pagination, the
raw "present" shortcut and the advisory-only upstream swallow inside internalize, and
main()'s whole network path (FATAL on bad defs, --only, both --pack-only/--nexus refusals,
the missing credentials, an unreachable depot, and a full run with a failed package).

Network is faked at ``urllib.request.urlopen`` with one URL router (the seam the real calls
make); choco_prefetch.py — an external role file — is a stub where main() needs it, and
loaded for real where the card tests the loader itself.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import runpy
import sys
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


ci = _load("choco_internalize_cov_i", ROOT / "scripts" / "lab" / "choco-internalize.py")

SHA = "a" * 64
BLOB = b"installer bytes"
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()
DEPOT = "http://10.30.32.10:8081"


# --------------------------------------------------------------------------- #
# Definition schema: the refusals the rejects table does not reach
# --------------------------------------------------------------------------- #
def _raw(**over) -> dict:
    doc = {"name": "demo", "version": "1.2.3", "type": "msi", "silent_args": "/qn",
           "installers": [{"url": "https://x/d.msi", "sha256": SHA}]}
    doc.update(over)
    return doc


def test_a_definition_that_is_not_a_mapping_is_refused():
    with pytest.raises(ci.DefinitionError, match="not a mapping"):
        ci.validate("demo", "test")


def test_valid_exit_codes_must_be_integers():
    for codes in ([], ["0"], [0, "3010"]):
        with pytest.raises(ci.DefinitionError, match="non-empty list of integers"):
            ci.validate(_raw(valid_exit_codes=codes), "test")


def test_software_name_when_given_must_be_a_string():
    for bad in ("", "   ", 7):
        with pytest.raises(ci.DefinitionError, match="software_name"):
            ci.validate(_raw(software_name=bad), "test")
    assert ci.validate(_raw(software_name=None), "test")["software_name"] is None


def test_an_installer_that_is_not_a_mapping_is_refused():
    with pytest.raises(ci.DefinitionError, match=r"installers\[0\] is not a mapping"):
        ci.validate(_raw(installers=["https://x/d.msi"]), "test")


def test_two_installers_cannot_share_a_file_name():
    dup = [{"url": "https://x/one/dir/d.msi", "sha256": SHA}, {"url": "https://x/other/d.msi", "sha256": SHA}]
    with pytest.raises(ci.DefinitionError, match="duplicate file d.msi"):
        ci.validate(_raw(installers=dup), "test")


def test_uninstall_is_zip_free_and_strictly_shaped():
    with pytest.raises(ci.DefinitionError, match="not used for zip"):
        ci.validate(_raw(type="zip", silent_args=None, uninstall={"silent_args": "/qn"}), "test")
    for bad in ("/qn", {"silent_args": "/qn", "timeout": 9}):
        with pytest.raises(ci.DefinitionError, match="uninstall must be a mapping"):
            ci.validate(_raw(uninstall=bad), "test")
    with pytest.raises(ci.DefinitionError, match="uninstall.silent_args is required"):
        ci.validate(_raw(software_name="Demo App", uninstall={"silent_args": "  "}), "test")
    d = ci.validate(_raw(software_name="Demo App", uninstall={"silent_args": " /S "}), "test")
    assert d["uninstall"] == {"silent_args": "/S"}  # stripped on the way in


def test_an_unreadable_definition_raises_a_definition_error(tmp_path):
    broken = tmp_path / "demo.yaml"
    broken.write_text("name: [unclosed")
    with pytest.raises(ci.DefinitionError, match="demo.yaml"):
        ci.load_definition(broken)
    with pytest.raises(ci.DefinitionError, match="No such file"):
        ci.load_definition(tmp_path / "absent.yaml")


def test_package_version_refuses_its_own_bad_input():
    with pytest.raises(ci.DefinitionError, match="bad version"):
        ci.package_version("1.2.3.4.5")
    with pytest.raises(ci.DefinitionError, match="bad version"):
        ci.package_version("1.2.3", 100)


def test_write_back_stops_at_the_next_installer(tmp_path):
    p = tmp_path / "demo.yaml"
    p.write_text(
        "name: demo\nversion: '1'\ntype: zip\ninstallers:\n"
        "  - url: https://x/a.zip\n    sha256: pinned\n"
        "  - url: https://x/b.zip\n    sha256: \"\"\n")
    assert ci.write_back_sha256(p, "https://x/a.zip", "c" * 64) is False  # pinned: keep looking? no: next item
    assert yaml.safe_load(p.read_text())["installers"][1]["sha256"] == ""  # b was never touched


# --------------------------------------------------------------------------- #
# The Nexus boundary and the installer cache
# --------------------------------------------------------------------------- #
def test_the_prefetch_role_module_loads_with_the_reused_helpers():
    cp = ci._prefetch()
    assert callable(cp.http) and callable(cp.upload) and callable(cp.latest_version)


def test_the_auth_header_is_basic_and_never_printed():
    assert ci._auth_header(("u", "p")) == {
        "Authorization": "Basic " + base64.b64encode(b"u:p").decode()}


def test_sha256_file_hashes_in_chunks(tmp_path):
    f = tmp_path / "blob"
    f.write_bytes(BLOB)
    assert ci.sha256_file(f) == BLOB_SHA


def test_fetch_installer_reuses_a_cached_copy_that_still_matches(tmp_path):
    dest = tmp_path / "cached" / "d.msi"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(BLOB)
    assert ci.fetch_installer("https://vendor.example/d.msi", dest, BLOB_SHA) == (BLOB_SHA, False)
    assert list(dest.parent.glob("*.part")) == []  # nothing was re-downloaded


def test_raw_has_asks_the_search_api_for_the_sha_and_matches_the_path():
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req)
        return io.BytesIO(json.dumps({"items": [
            {"path": "demo/1.2.3/d.msi"}, {"path": "/prefix/demo/1.2.3/d.msi"}]}).encode())

    ci.urllib.request.urlopen, real = urlopen, ci.urllib.request.urlopen
    try:
        assert ci.raw_has(DEPOT, ("u", "p"), "demo/1.2.3/d.msi", BLOB_SHA) is True
        assert ci.raw_has(DEPOT, ("u", "p"), "demo/1.2.3/other.msi", BLOB_SHA) is False
    finally:
        ci.urllib.request.urlopen = real
    q = parse_qs(urlparse(seen[0].full_url).query)
    assert q["repository"] == ["installers"] and q["sha256"] == [BLOB_SHA]
    assert seen[0].get_header("Authorization") == ci._auth_header(("u", "p"))["Authorization"]


def test_raw_upload_puts_the_file_with_its_length():
    src = Path(__file__)
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req)
        return io.BytesIO(b"")

    ci.urllib.request.urlopen, real = urlopen, ci.urllib.request.urlopen
    try:
        ci.raw_upload(DEPOT, ("u", "p"), "demo/1.2.3/d.msi", src)
    finally:
        ci.urllib.request.urlopen = real
    req, = seen
    assert req.get_method() == "PUT"
    assert req.full_url == f"{DEPOT}/repository/installers/demo/1.2.3/d.msi"
    assert req.get_header("Content-length") == str(src.stat().st_size)
    assert req.get_header("Content-type") == "application/octet-stream"


def test_hosted_versions_follows_the_continuation_tokens():
    pages = [
        json.dumps({"items": [{"name": "demo", "version": "1.0"},
                              {"name": "other", "version": "9.9"}],
                    "continuationToken": "tok-1"}),
        json.dumps({"items": [{"name": "DEMO", "version": "2.0"}]}),  # name match is case-free
    ]
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req.full_url)
        return io.BytesIO(pages.pop(0).encode())

    ci.urllib.request.urlopen, real = urlopen, ci.urllib.request.urlopen
    try:
        assert ci.hosted_versions(DEPOT, ("u", "p"), "demo") == ["1.0", "2.0"]
    finally:
        ci.urllib.request.urlopen = real
    assert len(seen) == 2 and "continuationToken=tok-1" in seen[1]


def test_internalize_takes_the_raw_present_shortcut_and_keeps_the_advisory_advisory(tmp_path):
    """raw already has the installer: no upload; a failing upstream check is swallowed."""
    defn = ci.validate({"name": "demo", "version": "1.2.3", "type": "zip",
                        "installers": [{"url": "https://vendor.example/d.zip", "sha256": BLOB_SHA}]})

    def urlopen(req, timeout=None):
        if "vendor.example" in req.full_url:
            return io.BytesIO(BLOB)
        if "search/assets" in req.full_url:  # the raw repo already holds it
            return io.BytesIO(json.dumps({"items": [{"path": "demo/1.2.3/d.zip"}]}).encode())
        return io.BytesIO(json.dumps({"items": [{"name": "demo", "version": "1.2.3.1"}]}).encode())

    uploads = []

    class Prefetch:
        def upload(self, *a):
            uploads.append(a)
            raise AssertionError("a present package is never pushed")

        def latest_version(self, upstream, pkg):
            raise RuntimeError("the community feed is down")

    ci.urllib.request.urlopen, real = urlopen, ci.urllib.request.urlopen
    try:
        rec = ci.internalize(defn, DEPOT, ("u", "p"), tmp_path, "https://up/", False, Prefetch())
    finally:
        ci.urllib.request.urlopen = real
    assert rec == {"id": "demo", "vendor_version": "1.2.3", "version": "1.2.3.1", "hosted": "present",
                   "installers": [{"file": "d.zip", "sha256": BLOB_SHA, "downloaded": True, "raw": "present"}]}
    assert uploads == []


# --------------------------------------------------------------------------- #
# main(): the argument guardrails and one full depot run
# --------------------------------------------------------------------------- #
def _defs(tmp_path, names=("demo1", "demo2"), blank=()):
    d = tmp_path / "defs"
    d.mkdir()
    for n in names:
        sha = "''" if n in blank else f"'{BLOB_SHA}'"
        (d / f"{n}.yaml").write_text(
            f"name: {n}\nversion: '1.2.3'\ntype: zip\ninstallers:\n"
            f"  - url: https://vendor.example/{n}.zip\n    sha256: {sha}\n")
    return d


def test_a_bad_definition_is_fatal_before_anything_else_runs(tmp_path, capsys):
    d = tmp_path / "defs"
    d.mkdir()
    (d / "demo.yaml").write_text("name: [unclosed")
    assert ci.main(["--defs", str(d), "--list"]) == 2
    assert "FATAL" in capsys.readouterr().out


def test_only_limits_the_run_to_the_named_packages(tmp_path):
    d, work = _defs(tmp_path), tmp_path / "work"
    assert ci.main(["--defs", str(d), "--only", "demo2", "--pack-only",
                    "--baked-url", DEPOT, "--workdir", str(work)]) == 0
    assert [p.name for p in (work / "nupkgs").iterdir()] == ["demo2.1.2.3.1.nupkg"]


def test_pack_only_and_network_mode_each_demand_their_depot(tmp_path):
    d = _defs(tmp_path, names=("demo1",))
    with pytest.raises(SystemExit, match="--pack-only needs --baked-url"):
        ci.main(["--defs", str(d), "--pack-only"])
    with pytest.raises(SystemExit, match="--nexus is required"):
        ci.main(["--defs", str(d)])


def test_depot_credentials_are_required(tmp_path, monkeypatch):
    d = _defs(tmp_path, names=("demo1",))
    monkeypatch.delenv("NEXUS_USER", raising=False)
    monkeypatch.delenv("NEXUS_PASSWORD", raising=False)
    with pytest.raises(SystemExit, match="NEXUS_USER / NEXUS_PASSWORD are not set"):
        ci.main(["--defs", str(d), "--nexus", DEPOT])


def test_an_unreachable_depot_is_fatal_not_a_crash(tmp_path, monkeypatch, capsys):
    d = _defs(tmp_path, names=("demo1",))
    monkeypatch.setenv("NEXUS_USER", "u")
    monkeypatch.setenv("NEXUS_PASSWORD", "p")

    class Unreachable:
        def http(self, url):
            raise urllib.error.URLError("no route to depot")

    monkeypatch.setattr(ci, "_prefetch", Unreachable)
    assert ci.main(["--defs", str(d), "--nexus", DEPOT]) == 2
    assert "FATAL depot unreachable" in capsys.readouterr().out


def test_a_full_run_uploads_one_package_and_reports_the_failed_one(tmp_path, monkeypatch, capsys):
    d, work = _defs(tmp_path, names=("demo1", "demo2", "demo3"), blank=("demo3",)), tmp_path / "work"
    monkeypatch.setenv("NEXUS_USER", "u")
    monkeypatch.setenv("NEXUS_PASSWORD", "p")

    def urlopen(req, timeout=None):
        url = req.full_url
        if "vendor.example" in url:
            return io.BytesIO(BLOB)
        if "search/assets" in url:
            return io.BytesIO(json.dumps({"items": []}).encode())  # raw repo: upload everything
        if "/service/rest/v1/search?" in url:  # hosted: demo2 is dominated by 9.9
            name = parse_qs(urlparse(url).query)["name"][0]
            items = [{"name": name, "version": "9.9"}] if name == "demo2" else []
            return io.BytesIO(json.dumps({"items": items}).encode())
        return io.BytesIO(b"{}")  # the status ping, the PUT acks

    pushed = []

    class Prefetch:
        def http(self, url):
            pass

        def upload(self, nexus, auth, fname, data):
            pushed.append(fname)

        def latest_version(self, upstream, pkg):
            return "2.0"  # newer than ours: the advisory is printed

    monkeypatch.setattr(ci, "_prefetch", Prefetch)
    monkeypatch.setattr(ci.urllib.request, "urlopen", urlopen)
    report = tmp_path / "report.json"
    argv = ["--defs", str(d), "--nexus", DEPOT, "--workdir", str(work),
            "--report", str(report), "--write-back"]
    assert ci.main(argv) == 0
    out = capsys.readouterr().out
    assert "uploaded  demo1 1.2.3.1  NEWER-UPSTREAM 2.0" in out
    assert "failed    demo2 1.2.3.1" in out
    assert "RuntimeError: chocolatey-hosted already has 9.9" in out
    assert "uploaded  demo3 1.2.3.1  RECORDED sha256 demo3.zip=" in out
    assert "TNINTERNALIZE uploaded=2 present=0 failed=1 ids=demo1,demo2,demo3" in out
    recs = json.loads(report.read_text())
    assert [r["hosted"] for r in recs] == ["uploaded", "failed", "uploaded"]
    assert recs[0]["installers"][0]["raw"] == "uploaded" and recs[0]["installers"][0]["downloaded"]
    assert recs[0]["newer_upstream"] == "2.0"
    assert recs[1]["error"].startswith("RuntimeError: chocolatey-hosted already has 9.9")
    assert recs[2]["installers"][0]["recorded"] is True
    assert recs[2]["installers"][0]["written_back"] is True
    assert pushed == ["demo1.1.2.3.1.nupkg", "demo3.1.2.3.1.nupkg"]
    assert (work / "nupkgs" / "demo1.1.2.3.1.nupkg").is_file()  # the nupkg is kept next to the report


def test_the_script_still_runs_as_a_script(tmp_path, monkeypatch, capsys):
    d = _defs(tmp_path, names=("demo1",))
    monkeypatch.setattr(sys, "argv", ["choco-internalize.py", "--defs", str(d), "--list"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "scripts" / "lab" / "choco-internalize.py"), run_name="__main__")
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out) == ["demo1"]
