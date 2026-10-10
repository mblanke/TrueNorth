"""The batch driver's release build (scripts/arc2_batch_inprocess.py build_release): the
engine's own builder over a no-symlink snapshot of the run, read where the api container
reads it (ARC2_RUNS_DIR), and the API's verifier accepts what it produces."""

from __future__ import annotations

import importlib.util
import sys
import tarfile
from pathlib import Path

import pytest
from test_arc2_release import released_run

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("arc2_batch_inprocess", ROOT / "scripts" / "arc2_batch_inprocess.py")
batch = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("arc2_batch_inprocess", batch)
_spec.loader.exec_module(batch)


def test_a_packaged_run_builds_a_release_the_api_accepts(tmp_path, repo):
    from app.course_releases import bundle

    run = released_run(tmp_path, repo)
    data, meta = batch.build_release(run.parent, run.name)
    parsed = bundle.parse(data)
    assert parsed.meta["release_digest"] == meta["release_digest"] and meta["catalogue_code"] == "C101"
    # Nothing is written beside the run: the api container may only read it.
    assert sorted(p.name for p in run.parent.iterdir()) == [run.name]


def test_the_snapshot_follows_no_symlink(tmp_path, repo):
    run = released_run(tmp_path, repo)
    secret = tmp_path / "secret.yaml"
    secret.write_text("JWT_SECRET: hunter2\n")
    (run / "05-sensor" / "leak.yaml").symlink_to(secret)
    (run / "05-sensor" / "linked").symlink_to(tmp_path, target_is_directory=True)
    data, meta = batch.build_release(run.parent, run.name)
    names = [f["path"] for part in meta["parts"].values() for f in part["files"]]
    assert not any("leak" in n or "linked" in n for n in names)
    assert b"hunter2" not in data
    with tarfile.open(fileobj=__import__("io").BytesIO(data), mode="r:gz") as tar:
        assert not any("leak" in n for n in tar.getnames())


def test_a_linked_run_directory_is_refused(tmp_path, repo):
    run = released_run(tmp_path, repo)
    (run.parent / "arc2-linked").symlink_to(run, target_is_directory=True)
    with pytest.raises(batch.ReleaseBuildError, match="not readable"):
        batch.build_release(run.parent, "arc2-linked")


def test_a_run_that_is_not_ready_is_refused(tmp_path, repo):
    from arc2 import check

    run = released_run(tmp_path, repo)
    m = check.load_manifest(run)
    m["qa"]["result"] = "fail"
    check.save_manifest(run, m)
    with pytest.raises(batch.ReleaseBuildError, match="QA is fail"):
        batch.build_release(run.parent, run.name)


def test_a_bad_slug_is_refused(tmp_path):
    with pytest.raises(batch.ReleaseBuildError, match="not a run name"):
        batch.build_release(tmp_path, "../etc")
