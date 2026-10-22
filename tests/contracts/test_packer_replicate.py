"""``infra/vsphere/packer/build.sh replicate`` (ADR 0012 decision 6), against a stub govc.

The script was also run against vcsim (hosts esx02-04 with <host>-local datastores); this
keeps its contract in CI, where neither vCenter nor govc exists: one copy per range host,
named ``<id>@<host>-<yyyymmdd>``, cloned powered off onto that host's local datastore next
to the template, then marked a template; idempotent; only against vCenter; a missing host
or template is an error. Also: the vSphere Packer tree is checked by CI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "infra/vsphere/packer/build.sh"

# A govc that knows three hosts, one template, and whatever it has cloned (state dir).
STUB = r"""#!/usr/bin/env bash
set -u
state="$STUB_STATE"
echo "$*" >> "$state/calls"
case "$1" in
  about) echo "API type:     ${STUB_API_TYPE:-VirtualCenter}" ;;
  find)
    name="${@: -1}"; type="$4"
    if [ "$type" = h ]; then
      for h in esx02.truenorth.lab esx03.truenorth.lab esx04.truenorth.lab; do
        case "$h" in $name) echo "/DC/host/CL/$h" ;; esac
      done
    elif [ "$name" = srv2022 ]; then echo "/DC/vm/TrueNorth/Templates/srv2022"
    elif [ -e "$state/vm/$name" ]; then echo "/DC/vm/TrueNorth/Templates/$name"
    fi ;;
  object.collect)
    if [ "${4:-}" = config.annotation ]; then cat "$state/ann/$(basename "$3")" 2>/dev/null
    else cat "$state/vm/$(basename "$3")"; fi ;;
  vm.clone)
    mkdir -p "$state/vm" "$state/ann"; echo false > "$state/vm/${@: -1}"
    for a in "$@"; do case "$a" in -annotation=*) printf '%s\n' "${a#-annotation=}" > "$state/ann/${@: -1}" ;; esac; done ;;
  vm.markastemplate) echo true > "$state/vm/$(basename "$2")" ;;
esac
"""


@pytest.fixture
def govc(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "govc"
    stub.write_text(STUB)
    stub.chmod(0o755)
    state = tmp_path / "state"
    (state / "vm").mkdir(parents=True)
    records = tmp_path / "records"
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_STATE": str(state),
           "GOVC_URL": "https://vcsa.test", "REPLICA_DATE": "20261019", "TN_BUILD_RECORDS": str(records)}
    for name in ("RANGE_HOSTS", "TEMPLATE_FOLDER", "TN_API_URL", "TN_API_TOKEN", "TN_BUILD_ID", "TN_ISO_CHECKSUM"):
        env.pop(name, None)

    def run(*args, command="replicate", **extra):
        return subprocess.run(["bash", str(BUILD), command, *args], env={**env, **extra},
                              capture_output=True, text=True, timeout=60)

    run.state = state
    run.records = records
    return run


def _calls(state: Path, verb: str) -> list[str]:
    path = state / "calls"
    return [c for c in path.read_text().splitlines() if c.startswith(verb)] if path.exists() else []


def test_one_copy_per_range_host_on_its_local_datastore(govc):
    got = govc("srv2022")
    assert got.returncode == 0, got.stderr
    clones = _calls(govc.state, "vm.clone")
    assert len(clones) == 3
    for short in ("esx02", "esx03", "esx04"):
        (clone,) = [c for c in clones if c.endswith(f"srv2022@{short}-20261019")]
        assert "-vm /DC/vm/TrueNorth/Templates/srv2022" in clone
        assert "-on=false" in clone and "-template=false" in clone
        assert f"-host=/DC/host/CL/{short}.truenorth.lab" in clone and f"-ds={short}-local" in clone
        assert "-folder=/DC/vm/TrueNorth/Templates" in clone
        assert (govc.state / "vm" / f"srv2022@{short}-20261019").read_text().strip() == "true"


def test_a_second_run_the_same_day_changes_nothing(govc):
    assert govc("srv2022").returncode == 0
    before = len(_calls(govc.state, "vm.clone"))
    again = govc("srv2022")
    assert again.returncode == 0 and "already exists; skipped" in again.stdout
    assert len(_calls(govc.state, "vm.clone")) == before


def test_a_copy_left_unmarked_is_marked_not_cloned_again(govc):
    (govc.state / "vm" / "srv2022@esx03-20261019").write_text("false\n")
    got = govc("srv2022", "esx03")
    assert got.returncode == 0 and "marking it" in got.stdout
    assert _calls(govc.state, "vm.clone") == []
    assert (govc.state / "vm" / "srv2022@esx03-20261019").read_text().strip() == "true"


def test_named_hosts_and_range_hosts_override_the_default(govc):
    assert govc("srv2022", "esx04").returncode == 0
    assert [c.rsplit(" ", 1)[1] for c in _calls(govc.state, "vm.clone")] == ["srv2022@esx04-20261019"]
    assert govc("srv2022", RANGE_HOSTS="esx02").returncode == 0
    assert len(_calls(govc.state, "vm.clone")) == 2


def test_an_unknown_host_fails_and_the_others_still_copy(govc):
    got = govc("srv2022", "esx09", "esx02")
    assert got.returncode == 1 and "host 'esx09' is not in vCenter inventory" in got.stderr
    assert [c.rsplit(" ", 1)[1] for c in _calls(govc.state, "vm.clone")] == ["srv2022@esx02-20261019"]


def test_a_missing_template_fails(govc):
    got = govc("kali")
    assert got.returncode == 1 and "not in vCenter inventory; build it first" in got.stderr


def test_refuses_an_esxi_host(govc):
    got = govc("srv2022", STUB_API_TYPE="HostAgent")
    assert got.returncode == 1 and "must point at vCenter" in got.stderr
    assert _calls(govc.state, "vm.clone") == []


def test_dry_run_changes_nothing(govc):
    got = govc("srv2022", DRY_RUN="1")
    assert got.returncode == 0 and got.stdout.count("DRY_RUN: govc vm.clone") == 3
    assert _calls(govc.state, "vm.clone") == []


# -- build records and stamps (ADR 0013 §2) ------------------------------------------------
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required for build records")


@needs_jq
def test_manifest_records_a_build_the_api_accepts(govc):
    got = govc("srv2022", command="manifest", TN_BUILD_ID="srv2022-20261019T120000Z",
               TN_ISO_CHECKSUM="sha256:" + "c" * 64,
               # Never registered before replicate (an incomplete, unpromotable record): an
               # unreachable API would fail the command if manifest tried to.
               TN_API_URL="http://127.0.0.1:9/api", TN_API_TOKEN="t")
    assert got.returncode == 0, got.stderr
    assert "register-back" not in got.stdout and "replicate" in got.stdout
    rec = json.loads((govc.records / "srv2022.build.json").read_text())
    core = {"build_id": rec["build_id"], "template": "srv2022", "iso_checksum": rec["iso_checksum"],
            "inputs_sha256": rec["inputs_sha256"]}
    # The checksum is the sha256 of the build's identity, as jq -c writes it.
    assert rec["checksum"] == hashlib.sha256(json.dumps(core, separators=(",", ":")).encode()).hexdigest()
    assert (rec["build_id"], rec["copies"]) == ("srv2022-20261019T120000Z", {})
    from app.golden_image_builds.schemas import GoldenImageBuildIn

    GoldenImageBuildIn(build_id=rec["build_id"], checksum=rec["checksum"], build_date=rec["build_date"],
                       iso_checksum=rec["iso_checksum"], template_name=rec["template"], copies=rec["copies"])


@needs_jq
def test_replicate_stamps_each_copy_and_records_it(govc):
    assert govc("srv2022", command="manifest", TN_BUILD_ID="b1").returncode == 0
    rec = json.loads((govc.records / "srv2022.build.json").read_text())
    got = govc("srv2022")
    assert got.returncode == 0, got.stderr
    stamp = f"tn-build: srv2022 b1 sha256:{rec['checksum']}"
    for short in ("esx02", "esx03", "esx04"):
        assert (govc.state / "ann" / f"srv2022@{short}-20261019").read_text().splitlines()[-1] == stamp
    rec = json.loads((govc.records / "srv2022.build.json").read_text())
    assert rec["copies"] == {s: f"srv2022@{s}-20261019" for s in ("esx02", "esx03", "esx04")}
    again = govc("srv2022")  # the same build's copies: reused
    assert again.returncode == 0 and "already exists; skipped" in again.stdout


@needs_jq
def test_a_copy_of_that_name_from_another_build_fails_replicate(govc):
    assert govc("srv2022", command="manifest", TN_BUILD_ID="b1").returncode == 0
    assert govc("srv2022", "esx02").returncode == 0
    assert govc("srv2022", command="manifest", TN_BUILD_ID="b2").returncode == 0  # a newer build, same day
    got = govc("srv2022", "esx02")
    assert got.returncode == 1 and "exists as tn-build: srv2022 b1" in got.stderr and "set REPLICA_DATE" in got.stderr
    assert len(_calls(govc.state, "vm.clone")) == 1


def test_without_a_record_copies_are_unstamped_and_it_says_so(govc):
    got = govc("srv2022", "esx02")
    assert got.returncode == 0 and "no build record" in got.stderr
    assert not (govc.state / "ann" / "srv2022@esx02-20261019").exists()


def test_ci_checks_the_vsphere_packer_tree():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = ci["jobs"]["packer-vsphere"]
    assert job["defaults"]["run"]["working-directory"] == "infra/vsphere/packer"
    runs = [s.get("run", "") for s in job["steps"]]
    for cmd in ("packer init .", "packer fmt -check -recursive .", "packer validate -syntax-only ."):
        assert cmd in runs
    setup = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("hashicorp/setup-packer@"))
    assert len(setup["uses"].split("@")[1]) == 40 and setup["with"]["version"] == "1.16.0"  # pinned by SHA
