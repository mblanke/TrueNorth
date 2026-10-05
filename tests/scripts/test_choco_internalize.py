"""scripts/lab/choco-internalize.py: offline Chocolatey packages from content/choco."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFS = ROOT / "content" / "choco"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


ci = _load("choco_internalize", ROOT / "scripts" / "lab" / "choco-internalize.py")
cp = _load("choco_prefetch", ROOT / "install" / "lab" / "roles" / "tn_prefetch" / "files" / "choco_prefetch.py")

SHA = "a" * 64
DEPOT = "http://10.30.32.10:8081"


def _defn(**over) -> dict:
    doc = {
        "name": "demo",
        "version": "1.2.3",
        "type": "msi",
        "silent_args": "/qn /norestart",
        "software_name": "Demo App",
        "installers": [{"arch": "x64", "url": "https://vendor.example/demo-1.2.3-x64.msi", "sha256": SHA}],
        "uninstall": {"silent_args": "/qn"},
    }
    doc.update(over)
    return ci.validate(doc, "test")


def _zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


# --------------------------------------------------------------------------- #
# Definition schema
# --------------------------------------------------------------------------- #


def test_every_shipped_definition_is_valid_and_pinned():
    defs = ci.load_definitions(DEFS)
    names = {d["name"] for d in defs}
    assert {"googlechrome", "firefoxesr", "adobereader", "vscode"} <= names
    for d in defs:
        for inst in d["installers"]:
            # A blank sha256 is allowed by the schema (computed at build), but the shipped
            # seed set was verified when it was written.
            assert inst["sha256"], f"{d['name']}: {inst['file']} has no sha256"
            assert inst["url"].startswith("https://")


def test_defaults_and_derived_file_name():
    d = ci.validate({"name": "z", "version": "2026.9.10", "type": "zip",
                     "installers": [{"url": "https://x.example/files/Suite.zip"}]})
    assert d["revision"] == 1 and d["valid_exit_codes"] == [0, 1641, 3010]
    assert d["installers"] == [{"url": "https://x.example/files/Suite.zip", "sha256": None,
                                "file": "Suite.zip", "arch": "any"}]


@pytest.mark.parametrize("over, msg", [
    ({"name": "Bad Name"}, "lower-case"),
    ({"version": "1.2.3esr"}, "numeric"),
    ({"version": "1.2.3.4.5"}, "numeric"),
    ({"revision": 0}, "revision"),
    ({"type": "msix"}, "type"),
    ({"silent_args": ""}, "silent_args"),
    ({"installers": []}, "installers"),
    ({"installers": [{"url": "ftp://x/y.msi"}]}, "http"),
    ({"installers": [{"url": "https://x/y.msi", "sha256": "abc"}]}, "64 hex"),
    ({"installers": [{"url": "https://x/1.0/win32-x64/stable"}]}, "file:"),
    ({"installers": [{"url": "https://x/y.msi", "arch": "arm64"}]}, "arch"),
    ({"installers": [{"url": "https://x/y.msi", "md5": "x"}]}, "unknown"),
    ({"software_name": None, "uninstall": {"silent_args": "/qn"}}, "software_name"),
    ({"silentargs": "/qn"}, "unknown keys"),
])
def test_schema_rejects(over, msg):
    with pytest.raises(ci.DefinitionError, match=msg):
        _defn(**over)


def test_name_must_match_file(tmp_path):
    (tmp_path / "other.yaml").write_text(yaml.safe_dump(
        {"name": "demo", "version": "1", "type": "zip", "installers": [{"url": "https://x/a.zip"}]}))
    with pytest.raises(ci.DefinitionError, match="file name"):
        ci.load_definitions(tmp_path)


# --------------------------------------------------------------------------- #
# Versions and precedence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("vendor, rev, pkg", [
    ("1.140.0", 1, "1.140.0.1"),
    ("7.6", 1, "7.6.0.1"),
    ("3", 2, "3.0.0.2"),
    ("155.0.8059.26", 1, "155.0.8059.2601"),
    ("155.0.8059.0", 3, "155.0.8059.3"),
])
def test_package_version(vendor, rev, pkg):
    assert ci.package_version(vendor, rev) == pkg


def test_ours_sorts_above_the_community_package_of_the_same_release():
    for vendor in ("1.140.0", "155.0.8059.26", "14.51.36247", "2026.2.21931"):
        assert ci.version_key(ci.package_version(vendor)) > ci.version_key(vendor)
    # ...and a newer vendor release still sorts above our older one.
    assert ci.version_key("155.0.8060.1") > ci.version_key(ci.package_version("155.0.8059.26"))
    assert ci.version_key("1.0.0-beta") < ci.version_key("1.0.0")


def test_precedence_problem():
    assert ci.precedence_problem("1.140.0.1", ["1.139.0", "1.140.0"]) is None
    assert ci.precedence_problem("1.140.0.1", ["1.140.0.1"]) is None
    problem = ci.precedence_problem("1.140.0.1", ["1.140.0", "1.141.0"])
    assert problem and "1.141.0" in problem and "Raise version" in problem


# --------------------------------------------------------------------------- #
# Package generation
# --------------------------------------------------------------------------- #


def test_nupkg_has_nuspec_and_install_script_from_the_depot():
    fname, data = ci.build_nupkg(_defn(), DEPOT)
    assert fname == "demo.1.2.3.1.nupkg"
    z = _zip(data)
    names = set(z.namelist())
    assert {"demo.nuspec", "tools/chocolateyInstall.ps1", "tools/chocolateyUninstall.ps1",
            "[Content_Types].xml", "_rels/.rels"} <= names
    assert any(n.startswith("package/services/metadata/core-properties/") for n in names)

    meta = ET.fromstring(z.read("demo.nuspec")).find("{*}metadata")
    assert meta.findtext("{*}id") == "demo"
    assert meta.findtext("{*}version") == "1.2.3.1"
    assert meta.findtext("{*}authors") and meta.findtext("{*}description")
    assert meta.find("{*}dependencies") is None  # no community dependencies

    ps1 = z.read("tools/chocolateyInstall.ps1").decode("utf-8-sig")
    assert "$env:TN_DEPOT_URL" in ps1
    assert f"'{DEPOT}/repository/installers'" in ps1  # baked fallback
    assert '"$installers/demo/1.2.3/demo-1.2.3-x64.msi"' in ps1
    assert "url64bit" in ps1 and f"checksum64     = '{SHA}'" in ps1 and "checksumType64 = 'sha256'" in ps1
    assert "Install-ChocolateyPackage @packageArgs" in ps1
    assert "fileType       = 'msi'" in ps1 and "silentArgs     = '/qn /norestart'" in ps1
    assert "softwareName   = 'Demo App*'" in ps1
    assert "vendor.example" not in ps1  # never the vendor URL

    un = z.read("tools/chocolateyUninstall.ps1").decode("utf-8-sig")
    assert "Get-UninstallRegistryKey -SoftwareName 'Demo App*'" in un and "-FileType 'msi'" in un

    # choco_prefetch.inspect (the prefetch's offline check) sees an offline-safe package.
    pid, ver, deps, urls, _ = cp.inspect(data)
    assert (pid, ver, deps) == ("demo", "1.2.3.1", [])
    assert urls == []


def test_nupkg_is_deterministic():
    assert ci.build_nupkg(_defn(), DEPOT) == ci.build_nupkg(_defn(), DEPOT)


def test_multi_installer_zip_and_quoting():
    d = _defn(type="zip", uninstall=None, software_name=None, silent_args=None, installers=[
        {"url": "https://x/a.zip", "sha256": SHA},
        {"url": "https://x/b.zip", "sha256": "b" * 64, "arch": "x64"},
    ])
    ps1 = ci.render_install_ps1(d, DEPOT + "/")
    assert ps1.count("Install-ChocolateyZipPackage @packageArgs") == 2
    assert "unzipLocation  = $toolsDir" in ps1 and "fileType" not in ps1
    assert ps1.index("/a.zip") < ps1.index("/b.zip")
    assert ci.render_uninstall_ps1(d) is None
    q = _defn(silent_args="/S /D='C:\\it''s'")
    assert "silentArgs     = '/S /D=''C:\\it''''s'''" in ci.render_install_ps1(q, DEPOT)


def test_exe_uninstall_uses_the_registry_uninstaller():
    un = ci.render_uninstall_ps1(_defn(type="exe", silent_args="/S", uninstall={"silent_args": "/S"}))
    assert "-FileType 'exe'" in un and "UninstallString" in un and "$silent = '/S'" in un


def test_a_blank_checksum_cannot_be_packed():
    d = _defn(installers=[{"url": "https://x/a.msi", "sha256": ""}])
    with pytest.raises(ci.DefinitionError, match="no sha256"):
        ci.build_nupkg(d, DEPOT)


def test_every_shipped_definition_packs(tmp_path):
    assert ci.main(["--defs", str(DEFS), "--pack-only", "--baked-url", DEPOT, "--workdir", str(tmp_path)]) == 0
    built = sorted(p.name for p in (tmp_path / "nupkgs").iterdir())
    assert "googlechrome.155.0.8059.2601.nupkg" in built
    assert len(built) == len(list(DEFS.glob("*.yaml")))


def test_list_prints_ids(capsys):
    assert ci.main(["--defs", str(DEFS), "--list"]) == 0
    ids = json.loads(capsys.readouterr().out)
    assert "vscode" in ids and ids == sorted(ids)


def test_write_back_fills_only_the_matching_blank(tmp_path):
    p = tmp_path / "demo.yaml"
    p.write_text(
        "name: demo\nversion: '1'\ntype: zip\ninstallers:\n"
        "  - url: https://x/a.zip\n    sha256: \"\"\n"
        "  - url: https://x/b.zip\n    sha256:  # TODO\n")
    assert ci.write_back_sha256(p, "https://x/b.zip", "c" * 64)
    doc = yaml.safe_load(p.read_text())
    assert doc["installers"][0]["sha256"] == "" and doc["installers"][1]["sha256"] == "c" * 64
    assert not ci.write_back_sha256(p, "https://x/b.zip", "d" * 64)  # no longer blank
    assert not ci.write_back_sha256(p, "https://x/none.zip", "d" * 64)


# --------------------------------------------------------------------------- #
# The build flow, network faked
# --------------------------------------------------------------------------- #


def _flow(tmp_path, monkeypatch, hosted=()):
    blob = b"installer bytes"
    defs = tmp_path / "defs"
    defs.mkdir()
    (defs / "demo.yaml").write_text(
        "name: demo\nversion: '1.2.3'\ntype: msi\nsilent_args: /qn\ninstallers:\n"
        "  - url: https://vendor.example/demo.msi\n    sha256: ''  # TODO: computed at build\n")
    fetched = []

    def urlopen(req, timeout=None):
        fetched.append(req.full_url)
        return io.BytesIO(blob)

    monkeypatch.setattr(ci.urllib.request, "urlopen", urlopen)
    calls = {"raw": [], "pushed": [], "fetched": fetched}
    monkeypatch.setattr(ci, "raw_has", lambda nexus, auth, path, sha: False)
    monkeypatch.setattr(ci, "raw_upload", lambda nexus, auth, path, src: calls["raw"].append((path, src.read_bytes())))
    monkeypatch.setattr(ci, "hosted_versions", lambda nexus, auth, pkg: list(hosted))
    fake = SimpleNamespace(upload=lambda nexus, auth, fname, data: calls["pushed"].append((fname, data)),
                           latest_version=lambda upstream, pkg: "1.3.0")
    defn = ci.load_definitions(defs)[0]
    rec = ci.internalize(defn, DEPOT, ("u", "p"), tmp_path / "work", "https://up/", True, fake)
    return rec, calls, hashlib.sha256(blob).hexdigest(), defs / "demo.yaml"


def test_internalize_downloads_records_uploads_and_pushes(tmp_path, monkeypatch):
    rec, calls, sha, path = _flow(tmp_path, monkeypatch, hosted=["1.2.3"])  # a community build
    assert rec["hosted"] == "uploaded" and rec["version"] == "1.2.3.1"
    assert rec["installers"][0]["recorded"] and rec["installers"][0]["sha256"] == sha
    assert yaml.safe_load(path.read_text())["installers"][0]["sha256"] == sha  # --write-back
    assert calls["fetched"] == ["https://vendor.example/demo.msi"]
    assert calls["raw"] == [("demo/1.2.3/demo.msi", b"installer bytes")]
    (fname, data), = calls["pushed"]
    assert fname == "demo.1.2.3.1.nupkg"
    assert sha in _zip(data).read("tools/chocolateyInstall.ps1").decode("utf-8-sig")
    assert rec["newer_upstream"] == "1.3.0"


def test_internalize_refuses_when_hosted_has_a_higher_version(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="Raise version"):
        _flow(tmp_path, monkeypatch, hosted=["1.3.0"])


def test_internalize_is_a_no_op_when_already_hosted(tmp_path, monkeypatch):
    rec, calls, _, _ = _flow(tmp_path, monkeypatch, hosted=["1.2.3.1"])
    assert rec["hosted"] == "present" and calls["pushed"] == []


def test_checksum_mismatch_fails(tmp_path):
    f = tmp_path / "x.msi"
    f.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        ci.fetch_installer(f.as_uri(), tmp_path / "out" / "x.msi", "0" * 64)
    assert not (tmp_path / "out" / "x.msi").exists()


# --------------------------------------------------------------------------- #
# choco_prefetch.py --skip: internalized ids never come from the community feed
# --------------------------------------------------------------------------- #


def test_prefetch_skips_internalized_ids_and_dependencies(tmp_path, monkeypatch, capsys):
    pkgs = tmp_path / "p.json"
    pkgs.write_text(json.dumps([{"choco": "wireshark"}, {"choco": "googlechrome"}]))
    fetched = []

    def download(nexus, upstream, pkg_id, version):
        fetched.append(pkg_id)
        return pkg_id.encode(), "depot-proxy"

    monkeypatch.setenv("NEXUS_USER", "u")
    monkeypatch.setenv("NEXUS_PASSWORD", "p")
    monkeypatch.setattr(cp, "http", lambda *a, **k: (200, b"{}"))
    monkeypatch.setattr(cp, "latest_version", lambda upstream, pkg_id: "1.0")
    monkeypatch.setattr(cp, "download", download)
    monkeypatch.setattr(cp, "inspect", lambda data: (data.decode(), "1.0", [("vcredist140", None)], [], ["tools/x.exe"]))
    monkeypatch.setattr(cp, "in_hosted", lambda *a: True)
    report = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["choco_prefetch.py", "--nexus", DEPOT, "--packages", str(pkgs),
                                      "--report", str(report), "--skip", "googlechrome,VCREDIST140"])
    assert cp.main() == 0
    assert fetched == ["wireshark"]
    recs = {r["id"]: r["hosted"] for r in json.loads(report.read_text())}
    assert recs == {"wireshark": "present", "googlechrome": "internal", "vcredist140": "internal"}
    assert "failed=0 " in capsys.readouterr().out
