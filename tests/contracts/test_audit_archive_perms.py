"""backup.sh / restore.sh and an audit archive they cannot read (rc6 staging upgrade).

On staging the archive is the worker's, /srv/truenorth/audit-archive 10001:10001 0750
(roles/tn_base), and backup.sh runs as the installer's user (uid 1000, docker group, not
root): `cp: cannot stat '/srv/truenorth/audit-archive/.': Permission denied`, backup FAILED
at step 'audit'. The scripts now read, write back and quarantine through a throwaway root
container of the platform's postgres image (scripts/backup/lib.sh "The audit export
archive"), as they already did for the ARC² runs; the archive's 0750 is not loosened.

The static checks always run. The docker test runs the real lib.sh helpers against an
archive owned by uid 10001 with 0750, as this (non-root) user; it skips without Docker or
without the platform's postgres image already pulled (no pulls from the unit lane). Where
the host enforces the ownership (Linux), it first shows the old `cp -a` failing. The drill
(scripts/backup/drill.sh) runs the same case end to end, and fails on Linux if it is not
exercised.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts/backup"
IMAGE = re.search(r"image: (postgres:16@sha256:[0-9a-f]+)",
                  (ROOT / "infra/platform/docker/compose.prod.yml").read_text()).group(1)


# ── static: nothing reads or writes the archive's contents on the host ────────────
@pytest.mark.parametrize("script", ["backup.sh", "restore.sh"])
def test_the_scripts_never_touch_the_archive_contents_on_the_host(script):
    text = (BACKUP / script).read_text()
    host_access = re.findall(r'^(?!\s*#).*\b(cp|find|mv|cat|tar|sed|openssl|rm|ls)\b[^\n]*"\$\{AUDIT_ARCHIVE_PATH\}',
                             text, re.M)
    assert not host_access, f"{script} touches AUDIT_ARCHIVE_PATH on the host: {host_access}"


def test_backup_reads_and_restore_writes_through_the_root_container():
    backup = (BACKUP / "backup.sh").read_text()
    restore = (BACKUP / "restore.sh").read_text()
    lib = (BACKUP / "lib.sh").read_text()
    assert "audit_archive_read " in backup and "audit_archive_owner" in backup
    assert "audit_archive_write " in restore
    assert "audit_archive_manifests" in restore and "audit_archive_quarantine " in restore
    tool = lib[lib.index("audit_archive_tool() {"):lib.index("audit_archive_read() {")]
    assert "--network none" in tool and "--user 0:0" in tool and ':ro"' in tool
    assert "umask 027" in lib  # what the container creates is not world-readable


def test_the_drill_runs_the_backup_against_an_archive_it_cannot_read():
    drill = (BACKUP / "drill.sh").read_text()
    assert "archive_as 10001:10001" in drill
    assert "the permission case is not exercised" in drill  # fails on Linux if it is not


def test_the_drill_declares_its_throwaway_enclave_profile():
    drill = (BACKUP / "drill.sh").read_text()
    assert "TN_ENCLAVE_ID=${DRILL_PROJECT}" in drill
    assert "TN_CLASSIFICATION_LEVEL=unclassified" in drill


# ── docker: the real helpers, an archive owned by 10001 with 0750 ──────────────────
def _docker_ok() -> bool:
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


def _root(archive: Path, script: str, *args: str, ro: bool = False) -> str:
    mount = f"{archive}:/archive" + (":ro" if ro else "")
    run = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", "0:0", "-v", mount,
                          "--entrypoint", "sh", IMAGE, "-c", script, "t", *args],
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    return run.stdout


def _lib(archive: Path, body: str) -> subprocess.CompletedProcess:
    script = f'set -euo pipefail\nsource "{BACKUP}/lib.sh"\n{body}\n'
    env = {**os.environ, "AUDIT_ARCHIVE_PATH": str(archive), "AUDIT_TOOL_IMAGE": IMAGE}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=180)


@pytest.mark.skipif(not _docker_ok(), reason=f"needs Docker and {IMAGE} already pulled")
def test_an_archive_owned_by_another_uid_with_0750_is_backed_up_and_restored(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("runs as root: the permission case needs an unprivileged user")
    archive = tmp_path / "audit-archive"
    (archive / "2026/10").mkdir(parents=True)
    (archive / "keys").mkdir()
    (archive / "2026/10/audit-2026-10-11.manifest.json").write_text('{"file":"2026/10/audit-2026-10-11.jsonl.gz"}')
    (archive / "2026/10/audit-2026-10-11.manifest.json.sig").write_bytes(b"sig")
    (archive / "2026/10/audit-2026-10-11.jsonl.gz").write_bytes(b"rows")
    (archive / "keys/abc.pub.pem").write_text("key")
    (archive / "2026/10/half.tmp").write_text("partial")
    _root(archive, 'chown -R 10001:10001 /archive; find /archive -type d -exec chmod 0750 {} +;'
                   ' find /archive -type f -exec chmod 0640 {} +')
    enforced = platform.system() == "Linux"
    if enforced:
        # The staging failure, reproduced: the installer's user cannot even list it.
        with pytest.raises(PermissionError):
            os.listdir(archive)
        old = subprocess.run(["cp", "-a", f"{archive}/.", str(tmp_path / "old-copy")], capture_output=True, text=True)
        assert old.returncode != 0 and "Permission denied" in old.stderr

    # backup.sh's read: everything but the worker's *.tmp, readable by this user.
    dest = tmp_path / "backup-copy"
    dest.mkdir()
    run = _lib(archive, f'audit_archive_read "{dest}"; audit_archive_owner')
    assert run.returncode == 0, run.stderr
    got = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
    assert got == ["2026/10/audit-2026-10-11.jsonl.gz", "2026/10/audit-2026-10-11.manifest.json",
                   "2026/10/audit-2026-10-11.manifest.json.sig", "keys/abc.pub.pem"]
    assert (dest / "2026/10/audit-2026-10-11.jsonl.gz").read_bytes() == b"rows"
    if enforced:
        assert run.stdout.strip() == "10001:10001"

    # restore.sh's write-back: a new day lands as the worker's, nothing world-readable.
    (dest / "2026/10/audit-2026-10-12.manifest.json").write_text('{"file":"2026/10/audit-2026-10-12.jsonl.gz"}')
    run = _lib(archive, f'audit_archive_write "{dest}"')
    assert run.returncode == 0, run.stderr
    owners = _root(archive, 'stat -c "%a" /archive; find /archive -perm -o=r -print;'
                            ' find /archive ! -user 10001 -print', ro=True).split("\n")
    assert owners[0] == "750"
    if enforced:
        assert [line for line in owners[1:] if line] == [], owners

    # restore.sh's reconcile: list, then quarantine one day, kept and recorded.
    run = _lib(archive, "audit_archive_manifests")
    assert run.returncode == 0, run.stderr
    listed = [line.split("\t") for line in run.stdout.splitlines()]
    assert [r[0] for r in listed] == ["2026/10/audit-2026-10-11.manifest.json", "2026/10/audit-2026-10-12.manifest.json"]
    assert listed[0][2] == "2026/10/audit-2026-10-11.jsonl.gz" and len(listed[0][1]) == 64
    run = _lib(archive, 'printf "%s\\n" 2026/10/audit-2026-10-12.manifest.json '
                        '| audit_archive_quarantine 20261013T000000Z \'{"moved":["2026/10/audit-2026-10-12.manifest.json"]}\'')
    assert run.returncode == 0, run.stderr
    after = _root(archive, "cd /archive && find quarantine -type f | sort; cat quarantine/*/RESTORE.json;"
                           " find /archive/quarantine ! -user 10001 -print", ro=True)
    assert "quarantine/20261013T000000Z/2026/10/audit-2026-10-12.manifest.json" in after
    assert '"moved":["2026/10/audit-2026-10-12.manifest.json"]' in after
    if enforced:
        assert "/archive/quarantine" not in after.split("RESTORE.json")[-1]

    # Hand it back so pytest can clean tmp_path up.
    _root(archive, "chown -R $(stat -c %u:%g /archive/..) /archive; chmod -R u+rwX /archive")
