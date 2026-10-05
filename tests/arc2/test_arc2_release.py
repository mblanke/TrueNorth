"""arc2.release: only an accepted, packaged run with a catalogue identity is released, the
three parts never mix, and the API's verifier reads what the tool writes."""

from __future__ import annotations

import tarfile

import pytest
from arc2 import check, release
from test_arc2_cmi5 import packaged_run
from test_arc2_manifest import full_manifest, make_run


def catalogued(code: str = "C101") -> dict:
    m = full_manifest()
    m["course"]["catalogue_code"] = code
    return m


def released_run(tmp_path, repo):
    """A packaged run after the orchestrator's final recording check (step 7)."""
    run = packaged_run(tmp_path, repo, catalogued())
    m, findings = check.check_run(run, repo)
    assert m["qa"]["result"] == "pass", [f for f in findings if f.severity == "fail"]
    return run


def test_a_packaged_catalogued_run_releases(tmp_path, repo):
    run = released_run(tmp_path, repo)
    out, meta = release.build(run, tmp_path / "r.tar.gz")
    assert meta["catalogue_code"] == "C101" and meta["activities"] == {"mod_001": "range"}
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert "release.json" in names
    learner = [n for n in names if n.startswith("learner/")]
    assert learner and not release.learner_leaks(learner)
    assert any(n.startswith("instructor/04-artifacts/") for n in names)
    assert "platform/03-range/lab_profile.yaml" in names
    assert not any(n.endswith("fragment.json") for n in names)


def test_the_same_run_gives_the_same_bytes(tmp_path, repo):
    run = released_run(tmp_path, repo)
    a, _ = release.build(run, tmp_path / "a.tar.gz")
    b, _ = release.build(run, tmp_path / "b.tar.gz")
    assert a.read_bytes() == b.read_bytes()


def test_the_api_verifier_reads_the_tool_output(tmp_path, repo):
    from app.course_releases import bundle

    run = released_run(tmp_path, repo)
    out, meta = release.build(run, tmp_path / "r.tar.gz")
    parsed = bundle.parse(out.read_bytes())
    assert parsed.meta["release_digest"] == meta["release_digest"]
    assert parsed.lab_profile and parsed.lab_profile["module_ids"] == ["mod_001"]


@pytest.mark.parametrize(
    ("breakage", "message"),
    [
        (lambda m: m["course"].update(catalogue_code=None), "catalogue_code is not set"),
        (lambda m: m["gates"]["preview"].update(state="pending"), "preview gate is pending"),
    ],
)
def test_a_run_that_is_not_ready_is_refused(tmp_path, repo, breakage, message):
    run = released_run(tmp_path, repo)
    m = check.load_manifest(run)
    breakage(m)
    check.save_manifest(run, m)
    with pytest.raises(release.ReleaseError, match=message):
        release.build(run)


def test_an_unpackaged_run_is_refused(tmp_path, repo):
    run = make_run(tmp_path, catalogued())
    with pytest.raises(release.ReleaseError, match="package-builder has not run"):
        release.build(run)


def test_an_edit_after_acceptance_is_refused(tmp_path, repo):
    run = released_run(tmp_path, repo)
    page = next((run / "02-content").rglob("page-01.html"))
    page.write_text(page.read_text() + "<p>edited</p>")
    with pytest.raises(release.ReleaseError, match="previewed files changed"):
        release.build(run)


def test_the_catalogue_identity_must_exist(tmp_path, repo):
    _, findings = check.check_run(make_run(tmp_path, catalogued("C999")), repo)
    assert "qa.catalogue_identity" in {f.check for f in findings if f.severity == "fail"}
    _, findings = check.check_run(make_run(tmp_path / "ok", catalogued("C101")), repo)
    assert "qa.catalogue_identity" not in {f.check for f in findings}


def test_cli(tmp_path, repo, capsys):
    run = released_run(tmp_path, repo)
    assert release.main(["build", str(run), "--out", str(tmp_path / "r.tar.gz")]) == 0
    assert "C101" in capsys.readouterr().out
    assert release.main(["build", str(make_run(tmp_path / "x", catalogued()))]) == 1
