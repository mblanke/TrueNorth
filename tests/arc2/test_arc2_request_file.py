"""Where a new run's request lives, and why a confined job never writes the runs root.

The first real Course Studio job on a Linux runner (bubblewrap) stopped at ``/arc2``
step 2: writing ``build/arc2/<slug>.request.txt`` failed with ``EROFS``. That file sat in
the runs root, which the sandbox mounts read-only; it was bound in alone, read-write, so
it could be written in place, but Claude Code's Write tool writes a temporary file beside
its target and renames it over, and the directory refused the temporary file. Seatbelt
on macOS denies that too; the job just never ran confined there.

Now the request lives inside the run (``<run>/request.txt``): the runner writes it before
the job starts (``runner.stage_request``) and ``arc2.check init`` reads it from there. The
model below replays bwrap's mount order (``confine.Bubblewrap.args``) so the Linux layout
is checked on every host, including the incident's own layout; the end-to-end probe in
test_arc2_confinement.py runs the real sandbox where the host has one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from arc2 import check, confine, runner

SLUG = "arc2-c101"


class Mounts:
    """bwrap's mount table, replayed from its arguments: a later mount covers an earlier
    one at or below its target; ``--remount-ro`` flips the topmost mount at that path."""

    def __init__(self, args: list[str]):
        self.table: list[dict] = []
        it = iter(args)
        for arg in it:
            if arg in ("--ro-bind", "--bind"):
                next(it)
                self.table.append({"at": Path(next(it)), "rw": arg == "--bind"})
            elif arg in ("--tmpfs", "--dev", "--proc"):
                self.table.append({"at": Path(next(it)), "rw": arg != "--proc"})
            elif arg == "--remount-ro":
                at = Path(next(it))
                next(m for m in reversed(self.table) if m["at"] == at)["rw"] = False
            elif arg == "--":
                break

    def mount(self, path: Path) -> dict:
        return next(m for m in reversed(self.table) if m["at"] == path or m["at"] in path.parents)

    def can_write_in_place(self, path: Path) -> bool:
        return self.mount(path)["rw"]

    def can_replace(self, path: Path) -> bool:
        """Write-temp-then-rename, as Claude Code's Write tool does: the temporary file goes
        in the directory, and the rename needs the target on the directory's own mount
        (a file that is itself a mount point cannot be renamed over: EBUSY)."""
        parent = self.mount(path.parent)
        return parent["rw"] and self.mount(path) is parent


@pytest.fixture
def host(tmp_path):
    """The Linux runner host: the runner's home, a checkout whose runs root is
    build/arc2, the incident's leftovers, and the checkout's .env files."""
    root = tmp_path.resolve()
    home, repo = root / "var" / "lib" / "tn-arc2", root / "srv" / "truenorth" / "app"
    runs = repo / "build" / "arc2"
    for path in (home, runs / "_studio", runs / "_queue", runs / "_jobs", runs / "_history", runs / "arc2-other"):
        path.mkdir(parents=True)
    (repo / "ai-orchestrator").mkdir()
    for name in (".env.example", "ai-orchestrator/.env.example"):
        (repo / name).write_text("API_KEY=change-me\n")
    (repo / ".env").write_text("SECRET=1\n")
    (runs / f"{SLUG}.request.txt").touch()  # what the v1.2.0 runner pre-created
    return home, repo, runs


def jail_for(home: Path, repo: Path, runs: Path) -> confine.Jail:
    return confine.Jail(repo=repo, runs=runs, home=home, writable=(runs / SLUG, home / "jobs" / "job000001"))


def test_the_model_reproduces_the_incident(host):
    """The v1.2.0 layout (the request file bound alone into the read-only runs root): the
    file is writable in place, but the Write tool's replace fails. Proves the model."""
    home, repo, runs = host
    args = confine.Bubblewrap().args(jail_for(home, repo, runs))
    old = runs / f"{SLUG}.request.txt"
    first_remount = args.index("--remount-ro")
    legacy = Mounts([*args[:first_remount], "--bind", str(old), str(old), *args[first_remount:]])
    assert legacy.can_write_in_place(old)
    assert not legacy.can_replace(old), "EROFS: the temporary file next to the target"


def test_init_succeeds_inside_the_run_and_the_runs_root_stays_read_only(host, monkeypatch):
    home, repo, runs = host
    mounts = Mounts(confine.Bubblewrap().args(jail_for(home, repo, runs)))
    record = {"id": "job000001", "action": "start", "slug": SLUG, "text": "Computer  Architecture\n& Systems, 60 min"}

    staged = runner.stage_request(runs, record)  # the runner, outside the sandbox
    assert staged == runs / SLUG / "request.txt"
    assert staged.read_text() == "Computer Architecture & Systems, 60 min\n"

    # The job: step 2 may (interactive) rewrite the request with the Write tool, then init.
    assert mounts.can_replace(staged)
    before = {p for p in runs.rglob("*")}
    monkeypatch.chdir(repo)
    assert check.main(["init", str(runs / SLUG), "--slug", SLUG, "--repo-root", str(repo)]) == 0
    written = {p for p in runs.rglob("*")} - before
    assert written, "init wrote the manifest and the stage directories"
    for path in written:
        assert runs / SLUG in path.parents, f"{path} is outside the run"
        assert mounts.can_replace(path), f"{path}: not writable under bwrap"
    manifest = json.loads((runs / SLUG / "manifest.json").read_text())
    assert (runs / SLUG / "request.txt").read_text() == "Computer Architecture & Systems, 60 min\n"
    assert manifest["provenance"]["inputs"]["request.txt"] == check.sha256_bytes(
        b"Computer Architecture & Systems, 60 min"
    )

    for path in (runs / f"{SLUG}.request.txt", runs / "anything", runs / "_studio" / "x.json",
                 runs / "_queue" / "x.json", runs / "_jobs" / "x.json", runs / "_history" / "x",
                 runs / "arc2-other" / "x", repo / "tools" / "x.py"):
        assert not mounts.can_write_in_place(path), path
        assert not mounts.can_replace(path), path


def test_tracked_env_templates_are_not_masked_but_secrets_are(host):
    """Masking ``.env.example`` swapped a tracked file for /dev/null, and every job's
    ``git status`` reported the checkout modified (``provenance.git_dirty``)."""
    home, repo, runs = host
    args = confine.Bubblewrap().args(jail_for(home, repo, runs))
    masked = {args[i + 2] for i, a in enumerate(args[:-2]) if a == "--ro-bind" and args[i + 1] == "/dev/null"}
    assert str(repo / ".env") in masked
    assert str(repo / ".env.example") not in masked
    assert str(repo / "ai-orchestrator" / ".env.example") not in masked


def test_staging_writes_only_a_new_run_and_follows_no_link(host):
    home, repo, runs = host
    start = {"id": "job000001", "action": "start", "slug": SLUG, "text": "a course"}
    assert runner.stage_request(runs, {**start, "action": "resume"}) is None

    # An earlier job of this run left request.txt as a link to the API's ownership file.
    (runs / "_studio" / f"{SLUG}.json").write_text('{"tenant_id": "t1"}')
    (runs / SLUG).mkdir()
    (runs / SLUG / "request.txt").symlink_to(runs / "_studio" / f"{SLUG}.json")
    with pytest.raises(runner.JobError):
        runner.stage_request(runs, start)
    assert (runs / "_studio" / f"{SLUG}.json").read_text() == '{"tenant_id": "t1"}'

    # A FIFO would block a plain open for writing; staging refuses it instead.
    (runs / SLUG / "request.txt").unlink()
    os.mkfifo(runs / SLUG / "request.txt")
    with pytest.raises(runner.JobError):
        runner.stage_request(runs, start)

    # A run that already has a manifest is resumed by /arc2: its request is history.
    (runs / SLUG / "request.txt").unlink()
    (runs / SLUG / "request.txt").write_text("the original request\nan appended line\n")
    (runs / SLUG / "manifest.json").write_text(json.dumps({"slug": SLUG}))
    assert runner.stage_request(runs, start) is None
    assert (runs / SLUG / "request.txt").read_text() == "the original request\nan appended line\n"


def test_a_run_folder_that_is_a_link_fails_the_job_before_the_engine_starts(host, tmp_path):
    home, repo, runs = host
    (runs / SLUG).symlink_to(runs / "arc2-other", target_is_directory=True)
    path = runs / "_jobs" / "job000001.json"
    record = {"id": "job000001", "action": "start", "slug": SLUG, "text": "a course", "state": "running"}
    out = runner.run_job(record, path, str(tmp_path / "never-run"), runs=runs)
    assert out["state"] == "failed" and "request not staged" in out["error"]
    assert not (runs / "arc2-other" / "request.txt").exists()


def test_init_reads_an_explicit_request_file_too(tmp_path):
    """Interactive runs from before this change passed --request-file; it still works."""
    req = tmp_path / "old.request.txt"
    req.write_text("a course\n")
    run = tmp_path / "build" / "arc2" / SLUG
    assert check.main(["init", str(run), "--slug", SLUG, "--request-file", str(req), "--repo-root", str(tmp_path)]) == 0
    assert (run / "request.txt").read_text() == "a course\n"


def test_init_without_a_request_file_says_so(tmp_path, capsys):
    run = tmp_path / "build" / "arc2" / SLUG
    assert check.main(["init", str(run), "--slug", SLUG, "--repo-root", str(tmp_path)]) == 1
    assert "no request file" in capsys.readouterr().err
