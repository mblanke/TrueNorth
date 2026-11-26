# Author: Marc-Andre Blanke
# Created: 2026-11-23T20:47:00Z
"""scripts/lab/clear-unstamped-copies.sh and friendly-tags.sh, against a stub govc.

* clear-unstamped-copies.sh removes only host copies that are templates with no
  "tn-build:" stamp (of the given id, and date when given); plan by default; never a VM,
  a stamped copy or anything under _retired. build-phase.sh offers it after a build in
  this run, before replicate, which refuses unstamped same-day copies (2026-11-23: five
  times on site).
* friendly-tags.sh plans/attaches OS and Role tags by catalogue id, only what is missing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "scripts/lab"

# A govc for a vCenter with a few templates/VMs. State: $STUB_STATE/{calls,vms}. A line in
# vms: "<path>|<template true|false>|<annotation, \n escaped>"; tags in $STUB_STATE/tags.
STUB = r"""#!/usr/bin/env bash
set -u
state="$STUB_STATE"
printf '%s\n' "$*" >> "$state/calls"
line_of() { grep -F "$1|" "$state/vms" | head -n1; }
case "$1" in
  about) echo '{"About":{"ApiType":"VirtualCenter"}}' ;;
  find)
    pat="${@: -1}"
    while IFS='|' read -r path tmpl ann; do
      [[ "$path" == "$2"* ]] || continue
      [[ "${path##*/}" == $pat || "$pat" == m ]] && echo "$path"
    done < "$state/vms" ;;
  object.collect)
    # STUB_FAIL_<PROPERTY>=<substring>: that read fails for matching VMs (a vCenter hiccup).
    if [ "$4" = config.annotation ] && [ -n "${STUB_FAIL_ANN:-}" ] && [[ "$3" == *"$STUB_FAIL_ANN"* ]]; then
      echo "ServerFaultCode: session expired" >&2; exit 1; fi
    if [ "$4" = config.template ] && [ -n "${STUB_FAIL_TMPL:-}" ] && [[ "$3" == *"$STUB_FAIL_TMPL"* ]]; then
      echo "ServerFaultCode: session expired" >&2; exit 1; fi
    IFS='|' read -r path tmpl ann <<<"$(line_of "$3")"
    if [ "$4" = config.template ]; then echo "$tmpl"; else printf '%b\n' "$ann"; fi ;;
  vm.destroy) grep -vF "$2|" "$state/vms" > "$state/vms.new"; mv "$state/vms.new" "$state/vms" ;;
  folder.info) [[ "$2" == /DC-Lab/vm/TrueNorth/Templates* ]] ;;
  tags.category.info|tags.info) exit 1 ;;
  tags.attached.ls) grep -F "$3|" "$state/tags" 2>/dev/null | cut -d'|' -f2 ;;
  tags.attach) echo "${@: -1}|$4" >> "$state/tags" ;;
esac
exit 0
"""

T = "/DC-Lab/vm/TrueNorth/Templates"
VMS = [
    f"{T}/srv2022|true|",
    f"{T}/Host copies/esx02/srv2022@esx02-20261123|true|TrueNorth golden copy of srv2022\\ntn-build: srv2022 b1 sha256:aa",
    f"{T}/Host copies/esx03/srv2022@esx03-20261123|true|",
    f"{T}/Host copies/esx04/srv2022@esx04-20261122|true|",
    f"{T}/Host copies/esx04/srv2022@esx04-20261123|false|",
    "/DC-Lab/vm/_retired/srv2022@esx02-20261123|true|",
    f"{T}/srv2022-mecm@esx02-20261123|true|",
    f"{T}/kali|true|",
    f"{T}/Host copies/esx02/kali@esx02-20261123|true|",
    f"{T}/tn-testclone-kali-esx02|false|",
    f"{T}/mystery|true|",
    # Hand-made templates that only look like copies: never host copies (one was destroyed by a
    # manual run of the first version, re-review of #530).
    f"{T}/srv2022@pre-sysprep-backup|true|",
    f"{T}/srv2022@esx02-2026112|true|",
    f"{T}/srv2022@esx02-20261123-old|true|",
]


@pytest.fixture
def lab(tmp_path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "govc").write_text(STUB)
    (bindir / "govc").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    (state / "vms").write_text("\n".join(VMS) + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GOVC_", "TN_", "APPLY", "ASSUME_YES"))}
    env.update(
        PATH=f"{bindir}:{env['PATH']}",
        STUB_STATE=str(state),
        GOVC_USERNAME="u",
        GOVC_PASSWORD="p",
        TN_TEMPLATE_ALLOW_UNPROMOTED="false",
        TN_ENV_FILE=str(tmp_path / "no.env"),
    )

    def run(script, *args, **extra):
        return subprocess.run(
            ["bash", str(LAB / script), *args],
            env={k: v for k, v in {**env, **extra}.items() if v is not None},  # None: unset
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )

    run.state = state
    return run


def _calls(state: Path, verb: str) -> list[str]:
    return [c for c in (state / "calls").read_text().splitlines() if c.startswith(verb)]


def test_clear_unstamped_plans_by_default_and_changes_nothing(lab):
    r = lab("clear-unstamped-copies.sh", "srv2022")
    assert r.returncode == 0, r.stderr
    assert "[plan] govc vm.destroy /DC-Lab/vm/TrueNorth/Templates/Host copies/esx03/srv2022@esx03-20261123" in r.stdout
    assert "plan only" in r.stdout and _calls(lab.state, "vm.destroy") == []


def test_clear_unstamped_removes_only_unstamped_templates_of_that_id_and_date(lab):
    r = lab("clear-unstamped-copies.sh", "srv2022", "--date", "20261123", "--apply", "--yes")
    assert r.returncode == 0, r.stderr
    assert _calls(lab.state, "vm.destroy") == [f"vm.destroy {T}/Host copies/esx03/srv2022@esx03-20261123"]
    kept = (lab.state / "vms").read_text()
    for name in (
        "srv2022@esx02-20261123|true|TrueNorth",  # stamped: a recorded build
        "srv2022@esx04-20261122",  # another date
        "srv2022@esx04-20261123|false",  # a VM, not a template
        "_retired/srv2022@esx02-20261123",  # retired
        "srv2022-mecm@esx02-20261123",
    ):  # another id
        assert name in kept, name
    assert "keep  srv2022@esx02-20261123: stamped" in r.stdout


def test_clear_unstamped_without_a_date_takes_every_date(lab):
    assert lab("clear-unstamped-copies.sh", "srv2022", "kali", "--apply", "--yes").returncode == 0
    gone = {c.rsplit("/", 1)[1] for c in _calls(lab.state, "vm.destroy")}
    assert gone == {"srv2022@esx03-20261123", "srv2022@esx04-20261122", "kali@esx02-20261123"}


def test_clear_unstamped_apply_without_a_yes_removes_nothing(lab):
    r = lab("clear-unstamped-copies.sh", "srv2022", "--apply")  # no terminal to answer y/N on
    assert r.returncode != 0 and _calls(lab.state, "vm.destroy") == []


def test_clear_unstamped_fails_closed_when_the_annotation_cannot_be_read(lab):
    """A failed config.annotation read must not look like "no stamp": the stamped (maybe
    promoted) copy would be destroyed. Kept, warned about, exit non-zero."""
    r = lab(
        "clear-unstamped-copies.sh",
        "srv2022",
        "--date",
        "20261123",
        "--apply",
        "--yes",
        STUB_FAIL_ANN="srv2022@esx02-20261123",
    )
    assert r.returncode != 0
    destroyed = _calls(lab.state, "vm.destroy")
    assert not [c for c in destroyed if "srv2022@esx02-20261123" in c]
    assert "cannot read its annotation" in r.stderr and "could not be read and were kept" in r.stderr
    # The copy that was read fine and is unstamped still goes.
    assert destroyed == [f"vm.destroy {T}/Host copies/esx03/srv2022@esx03-20261123"]


def test_clear_unstamped_fails_closed_when_the_template_flag_cannot_be_read(lab):
    r = lab(
        "clear-unstamped-copies.sh",
        "srv2022",
        "--date",
        "20261123",
        "--apply",
        "--yes",
        STUB_FAIL_TMPL="srv2022@esx03-20261123",
    )
    assert r.returncode != 0 and _calls(lab.state, "vm.destroy") == []
    assert "cannot read whether it is a template" in r.stderr


@pytest.mark.parametrize("flag", ["true", "1", "kali,srv2022"])
def test_clear_unstamped_refuses_copies_ranges_may_use_without_force(lab, flag):
    """With TN_TEMPLATE_ALLOW_UNPROMOTED allowing the id, the worker clones the newest dated
    copy per host of an unpromoted image: exactly these copies."""
    plan = lab("clear-unstamped-copies.sh", "srv2022", TN_TEMPLATE_ALLOW_UNPROMOTED=flag)
    assert plan.returncode == 0 and "may be in use: srv2022@esx03-20261123" in plan.stderr
    assert "--apply --force" in plan.stdout
    r = lab("clear-unstamped-copies.sh", "srv2022", "--apply", "--yes", TN_TEMPLATE_ALLOW_UNPROMOTED=flag)
    assert r.returncode != 0 and "--force" in r.stderr and _calls(lab.state, "vm.destroy") == []
    forced = lab(
        "clear-unstamped-copies.sh", "srv2022", "--apply", "--yes", "--force", TN_TEMPLATE_ALLOW_UNPROMOTED=flag
    )
    assert forced.returncode == 0 and len(_calls(lab.state, "vm.destroy")) == 2


def test_clear_unstamped_reads_the_flag_from_the_env_file_and_assumes_in_use_when_unknown(lab, tmp_path):
    env_file = tmp_path / "site.env"
    env_file.write_text('VSPHERE_USERNAME=u\nTN_TEMPLATE_ALLOW_UNPROMOTED="true"\n')
    r = lab(
        "clear-unstamped-copies.sh",
        "srv2022",
        "--apply",
        "--yes",
        "--env-file",
        str(env_file),
        TN_TEMPLATE_ALLOW_UNPROMOTED=None,
    )
    assert r.returncode != 0 and _calls(lab.state, "vm.destroy") == []
    env_file.write_text("TN_TEMPLATE_ALLOW_UNPROMOTED=kali\n")  # another id only
    r = lab(
        "clear-unstamped-copies.sh",
        "srv2022",
        "--date",
        "20261123",
        "--apply",
        "--yes",
        "--env-file",
        str(env_file),
        TN_TEMPLATE_ALLOW_UNPROMOTED=None,
    )
    assert r.returncode == 0 and len(_calls(lab.state, "vm.destroy")) == 1
    r = lab(
        "clear-unstamped-copies.sh",
        "srv2022",
        "--apply",
        "--yes",
        "--env-file",
        str(tmp_path / "gone.env"),
        TN_TEMPLATE_ALLOW_UNPROMOTED=None,
    )  # unreadable: assume ranges may use them
    assert r.returncode != 0 and len(_calls(lab.state, "vm.destroy")) == 1


def test_clear_unstamped_touches_only_names_in_the_host_copy_format(lab):
    """<id>@<host>-<yyyymmdd> exactly (and that date with --date); anything else is kept."""
    r = lab("clear-unstamped-copies.sh", "srv2022", "--apply", "--yes")
    assert r.returncode == 0, r.stderr
    gone = {c.rsplit("/", 1)[1] for c in _calls(lab.state, "vm.destroy")}
    assert gone == {"srv2022@esx03-20261123", "srv2022@esx04-20261122"}
    kept = (lab.state / "vms").read_text()
    for name in ("srv2022@pre-sysprep-backup", "srv2022@esx02-2026112|", "srv2022@esx02-20261123-old"):
        assert name in kept, name
    assert "keep  srv2022@pre-sysprep-backup: not a host copy name" in r.stdout


def _flag_env(tmp_path, line):
    env_file = tmp_path / "worker.env"
    env_file.write_text(f"VSPHERE_USERNAME=u\n{line}\n")
    return str(env_file)


@pytest.mark.parametrize(
    "line",
    [
        "TN_TEMPLATE_ALLOW_UNPROMOTED=true  # work site",
        'TN_TEMPLATE_ALLOW_UNPROMOTED="yes"',
        "TN_TEMPLATE_ALLOW_UNPROMOTED=maybe?",  # unparseable: allowing
        "TN_TEMPLATE_ALLOW_UNPROMOTED=ubuntu-lts,srv2022  # transition",
    ],
)
def test_clear_unstamped_guard_is_open_only_for_an_explicit_off(lab, tmp_path, line):
    r = lab(
        "clear-unstamped-copies.sh",
        "srv2022",
        "--apply",
        "--yes",
        "--env-file",
        _flag_env(tmp_path, line),
        TN_TEMPLATE_ALLOW_UNPROMOTED=None,
    )
    assert r.returncode != 0 and "--force" in r.stderr and _calls(lab.state, "vm.destroy") == []


@pytest.mark.parametrize(
    "line",
    [
        "TN_TEMPLATE_ALLOW_UNPROMOTED=false  # not on this site",
        "TN_TEMPLATE_ALLOW_UNPROMOTED='off'",
        "TN_TEMPLATE_ALLOW_UNPROMOTED=",
        "TN_TEMPLATE_ALLOW_UNPROMOTED=kali",
    ],
)
def test_clear_unstamped_guard_open_for_off_and_for_other_ids(lab, tmp_path, line):
    r = lab(
        "clear-unstamped-copies.sh",
        "srv2022",
        "--date",
        "20261123",
        "--apply",
        "--yes",
        "--env-file",
        _flag_env(tmp_path, line),
        TN_TEMPLATE_ALLOW_UNPROMOTED=None,
    )
    assert r.returncode == 0, r.stderr
    assert len(_calls(lab.state, "vm.destroy")) == 1


@pytest.mark.parametrize(("file_value", "shell_value"), [("false", "true"), ("true", "false"), ("false", "kali")])
def test_clear_unstamped_prefers_the_worker_env_file_and_refuses_when_the_shell_disagrees(
    lab, tmp_path, file_value, shell_value
):
    env_file = _flag_env(tmp_path, f"TN_TEMPLATE_ALLOW_UNPROMOTED={file_value}")
    r = lab(
        "clear-unstamped-copies.sh",
        "srv2022",
        "--date",
        "20261123",
        "--apply",
        "--yes",
        "--env-file",
        env_file,
        TN_TEMPLATE_ALLOW_UNPROMOTED=shell_value,
    )
    assert r.returncode != 0 and _calls(lab.state, "vm.destroy") == []
    agreed = lab(
        "clear-unstamped-copies.sh",
        "srv2022",
        "--date",
        "20261123",
        "--apply",
        "--yes",
        "--env-file",
        env_file,
        TN_TEMPLATE_ALLOW_UNPROMOTED=file_value,
    )
    assert (agreed.returncode == 0) == (file_value == "false")


def test_clear_unstamped_refuses_a_pattern_for_an_id(lab):
    r = lab("clear-unstamped-copies.sh", "*", "--apply", "--yes")
    assert r.returncode != 0 and "not a template id" in r.stderr


def test_build_phase_clears_only_after_a_build_and_before_replicate():
    text = (LAB / "build-phase.sh").read_text()
    post = text[text.index("post_build() {") :]
    post = post[: post.index("\n}\n")]
    assert 'clear-unstamped-copies.sh" "$id" --date "${REPLICA_DATE:-$(date +%Y%m%d)}"' in post
    assert post.index('"${clear[@]}" --apply') < post.index('DRY_RUN=1 RANGE_HOSTS="$RANGE_HOSTS"')
    assert post.count('[[ "$BUILT_NOW" == "$id"') == 2
    build = text[text.index("build_and_post() {") :]
    build = build[: build.index("\n}\n")]
    skipped = build[build.index("template exists; not rebuilt") : build.index("NOT BUILDABLE")]
    assert "BUILT_NOW" not in skipped  # an existing template that was not rebuilt: nothing cleared
    assert build.count('BUILT_NOW="$id"') == 2  # the build (and its dry run)
    assert build.index('"$PACKER_DIR/build.sh" "$id"') < build.rindex('BUILT_NOW="$id"')


def test_friendly_tags_plans_os_and_role_per_template(lab):
    r = lab("friendly-tags.sh")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert (
        "[plan] govc tags.category.create -t VirtualMachine -d TrueNorth: operating system family of the image OS"
        in out
    )
    assert f"[plan] govc tags.attach -c OS Windows {T}/Host copies/esx02/srv2022@esx02-20261123" in out
    assert f"[plan] govc tags.attach -c Role Server {T}/srv2022" in out
    assert f"[plan] govc tags.attach -c Role Security tool {T}/Host copies/esx02/kali@esx02-20261123" in out
    assert "tn-testclone" not in out and "_retired" not in out
    assert "no OS/Role mapping (add it to os_role_of): mystery" in r.stderr
    assert out.count("tags.create -c Role Server") == 1  # each tag created once
    assert _calls(lab.state, "tags.attach ") == [] and _calls(lab.state, "tags.create ") == []


def test_friendly_tags_attaches_only_what_is_missing(lab):
    (lab.state / "tags").write_text(f"{T}/kali|Linux\n")
    r = lab("friendly-tags.sh", "--apply", "--yes")
    assert r.returncode == 0, r.stderr
    attached = (lab.state / "tags").read_text().splitlines()
    assert attached.count(f"{T}/kali|Linux") == 1 and f"{T}/kali|Security tool" in attached
    again = lab("friendly-tags.sh", "--apply", "--yes")
    assert again.returncode == 0 and "0 tag attachment(s) made" in again.stdout


@pytest.mark.parametrize("name", ["clear-unstamped-copies.sh", "friendly-tags.sh"])
def test_new_lab_scripts_parse_are_executable_and_shellcheck_clean(name):
    assert os.access(LAB / name, os.X_OK)
    assert subprocess.run(["bash", "-n", str(LAB / name)]).returncode == 0
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    r = subprocess.run(["shellcheck", "-x", name], cwd=LAB, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
