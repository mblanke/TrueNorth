"""scripts/lab/vcenter_organize.py and vcenter-organize.sh, and the kit's tagging, with no vCenter.

tests/integration/test_vcenter_organize_sim.py runs the same tool against vcsim through the
real govc; this file keeps the contract where neither exists (CI's unit job):

* the kit and the worker (provisioners/vsphere_tags.py) define the same categories, types,
  range labels and folder matching;
* the plan for the tree the work vCenter had on 2026-10-20, step by step, and that nothing
  outside TrueNorth's folders and names is in it;
* an old-folder delete only ever runs on a folder vCenter reports empty;
* a value of a single-cardinality category is detached before the new one is attached;
* vcenter-organize.sh never prints the vCenter password, and refuses to apply without the
  range list or with the worker still configured for the old folder;
* build.sh replicate tags the golden template and its copies, best effort.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "scripts/lab"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


org = _load("vcenter_organize", LAB / "vcenter_organize.py")
ranges_inprocess = _load("tn_ranges_inprocess", LAB / "tn_ranges_inprocess.py")

RID = "2fc8d04d-1111-4222-8333-444455556666"
OS_MAP = org.load_os_map()


# -- one definition, kit and worker -----------------------------------------------------

def test_kit_and_worker_define_the_same_tags():
    from worker.provisioners import vsphere_tags as worker_tags

    assert org.CATEGORIES == worker_tags.CATEGORIES
    assert org.TYPES == worker_tags.TYPES
    assert org.RANGES == "TrueNorth/Ranges" and f"{org.LEGACY}/ranges" in worker_tags.LEGACY_RANGE_ROOTS
    for name in ("Blue Team 3", "", None, "lab-2fc8d04d", "x" * 90, "ARC² Week/1"):
        assert org.range_label(name, RID) == worker_tags.range_label(name, RID)
    for folder in ("2fc8d04d", "blue-2fc8d04d", "x2fc8d04d", "2fc8d04d-x", "nothex99"):
        assert (org.folder_id8(folder) == "2fc8d04d") == worker_tags.range_folder_id8(folder, "2fc8d04d")
    for source in ("ubuntu-lts@esx02-20261020", "library:srv2022", "kali"):
        assert org.template_id(source) == worker_tags.template_id(source)


def test_the_worker_default_is_the_kit_target():
    from worker.provisioners import vsphere_api

    src = Path(vsphere_api.__file__).read_text()
    assert 'os.environ.get("VSPHERE_RANGE_FOLDER", "TrueNorth/Ranges")' in src
    assert "VSPHERE_RANGE_FOLDER:-TrueNorth/Ranges}" in (ROOT / "infra/platform/docker/compose.prod.yml").read_text()
    assert "tn_vsphere_range_folder: TrueNorth/Ranges" in (ROOT / "install/inventory/group_vars/all/main.yml").read_text()


def test_os_aliases_come_from_the_catalogue_and_variants():
    assert OS_MAP["ubuntu-lts"] == "ubuntu-2404"
    assert OS_MAP["srv2022"] == "windows-server-2022"
    assert OS_MAP["pfsense"] == "pfsense"
    assert OS_MAP["rocky"] == "rocky"  # no alias: its id
    variants = sorted(p.name.removesuffix(".pkrvars.hcl") for p in (ROOT / "infra/vsphere/packer/variants").glob("*.pkrvars.hcl"))
    assert variants and all(v in OS_MAP for v in variants)


# -- the plan -----------------------------------------------------------------------------

def _inv(vms: list[tuple[str, str, bool]], folders: set[str], tags: dict | None = None) -> org.Inventory:
    out = [org.Vm(moid, f"/DC-Lab/vm/{rel}", template, dict((tags or {}).get(moid, {}))) for moid, rel, template in vms]
    return org.Inventory("DC-Lab", out, folders)


# The work vCenter's VM folders on 2026-10-20 (read by the coordinator), plus what must stay put.
TREE = _inv(
    [("vm-1", f"truenorth/ranges/{RID[:8]}/{RID[:8]}-ubu01", False),
     ("vm-2", f"truenorth/ranges/{RID[:8]}/{RID[:8]}-ubu02", False),
     ("vm-3", f"truenorth/ranges/{RID[:8]}/{RID[:8]}-ubu03", False),
     ("vm-4", "truenorth/templates/kali-linux", True),
     ("vm-5", "TrueNorth/Templates/ubuntu-lts", True),
     ("vm-6", "TrueNorth/Templates/ubuntu-lts@esx02-20261020", True),
     ("vm-7", "TrueNorth/Templates/ubuntu-lts@esx03-20261020", True),
     ("vm-8", "TrueNorth/Templates/ubuntu-lts@esx04-20261020", True),
     ("vm-9", "TrueNorth/Templates/kali", True),
     ("vm-10", "TrueNorth/Templates/srv2022", True),
     ("vm-20", "WIN11-TMPL", True),
     ("vm-21", "Discovered virtual machine/vcsa01", False),
     ("vm-22", "vCLS/vCLS-4c4c4544", False),
     ("vm-23", "kali-linux", False),  # someone's own VM of that name, outside TrueNorth's folders
     ("vm-24", "TN-DEPOT01", False),
     ("vm-25", "TN-MGMT01", False)],
    {"truenorth", "truenorth/ranges", f"truenorth/ranges/{RID[:8]}", "truenorth/templates",
     "TrueNorth", "TrueNorth/Templates", "Discovered virtual machine", "vCLS"},
)
RANGES = [{"id": RID, "name": "Ubuntu Lab", "state": "ready",
           "vms": [{"name": f"{RID[:8]}-ubu01", "template": "ubuntu-lts@esx02-20261020"}]},
          {"id": "0badc0de-0000-4000-8000-000000000000", "name": "gone", "state": "destroyed"}]
LABEL = f"ubuntu-lab-{RID[:8]}"


def _steps(plan, kind: str) -> list[tuple]:
    return [a.args for a in plan.actions if a.kind == kind]


def test_plan_for_the_work_vcenter_tree():
    plan = org.make_plan(TREE, RANGES, OS_MAP)
    assert not plan.refuse
    assert _steps(plan, "mkdir") == [("/DC-Lab/vm/TrueNorth/Ranges",), ("/DC-Lab/vm/TrueNorth/Templates/_retired",)]
    assert _steps(plan, "move") == [
        ("/DC-Lab/vm/truenorth/templates/kali-linux", "/DC-Lab/vm/TrueNorth/Templates/_retired"),
        (f"/DC-Lab/vm/truenorth/ranges/{RID[:8]}", "/DC-Lab/vm/TrueNorth/Ranges"),
    ]
    assert _steps(plan, "rename") == [(f"/DC-Lab/vm/TrueNorth/Ranges/{RID[:8]}", LABEL)]
    assert _steps(plan, "rmdir") == [("/DC-Lab/vm/truenorth/ranges",), ("/DC-Lab/vm/truenorth/templates",),
                                     ("/DC-Lab/vm/truenorth",)]  # deepest first, the root last
    tags = {ref: want for ref, _, want in _steps(plan, "tag")}
    assert tags["VirtualMachine:vm-5"] == {"tn-type": "golden-template", "tn-template": "ubuntu-lts", "tn-os": "ubuntu-2404"}
    assert tags["VirtualMachine:vm-7"] == {"tn-type": "template-copy", "tn-template": "ubuntu-lts", "tn-os": "ubuntu-2404"}
    assert tags["VirtualMachine:vm-10"]["tn-os"] == "windows-server-2022"
    assert tags["VirtualMachine:vm-1"] == {"tn-type": "range-vm", "tn-range": LABEL, "tn-template": "ubuntu-lts",
                                           "tn-os": "ubuntu-2404"}
    assert tags["VirtualMachine:vm-2"] == {"tn-type": "range-vm", "tn-range": LABEL}
    assert tags["VirtualMachine:vm-24"] == {"tn-type": "depot"} and tags["VirtualMachine:vm-25"] == {"tn-type": "mgmt"}
    # Order: folders exist before anything moves into them; deletes only after the moves.
    kinds = [a.kind for a in plan.actions]
    assert kinds.index("mkdir") < kinds.index("move") and max(i for i, k in enumerate(kinds) if k == "move") \
        < kinds.index("rmdir") < kinds.index("tag")


def test_nothing_outside_truenorth_is_in_the_plan():
    plan = org.make_plan(TREE, RANGES, OS_MAP)
    text = "\n".join(str(a) for a in plan.actions)
    for other in ("WIN11-TMPL", "vcsa01", "vCLS", "Discovered virtual machine", "vm-23", "vm-4", "/DC-Lab/vm/kali-linux"):
        assert other not in text, other
    assert all(a.args[0].startswith(("/DC-Lab/vm/truenorth", "/DC-Lab/vm/TrueNorth")) for a in plan.actions
               if a.kind in ("move", "rename", "rmdir"))


def test_after_apply_the_plan_is_empty():
    plan = org.make_plan(TREE, RANGES, OS_MAP)
    tags = {ref.split(":")[1]: want for ref, _, want in _steps(plan, "tag")}
    done = _inv(
        [("vm-1", f"TrueNorth/Ranges/{LABEL}/{RID[:8]}-ubu01", False),
         ("vm-2", f"TrueNorth/Ranges/{LABEL}/{RID[:8]}-ubu02", False),
         ("vm-3", f"TrueNorth/Ranges/{LABEL}/{RID[:8]}-ubu03", False),
         ("vm-4", "TrueNorth/Templates/_retired/kali-linux", True),
         *[(v.moid, TREE.rel(v.path), v.template) for v in TREE.vms if v.moid not in ("vm-1", "vm-2", "vm-3", "vm-4")]],
        {"TrueNorth", "TrueNorth/Templates", "TrueNorth/Templates/_retired", "TrueNorth/Ranges",
         f"TrueNorth/Ranges/{LABEL}", "Discovered virtual machine", "vCLS"},
        tags,
    )
    again = org.make_plan(done, RANGES, OS_MAP)
    assert again.actions == [] and not again.refuse


def test_a_range_vm_keeps_the_os_and_template_its_build_set():
    inv = _inv([("vm-1", f"TrueNorth/Ranges/{LABEL}/{RID[:8]}-ubu01", False)], {"TrueNorth", "TrueNorth/Templates",
               "TrueNorth/Ranges", f"TrueNorth/Ranges/{LABEL}"},
               {"vm-1": {"tn-type": "range-vm", "tn-range": LABEL, "tn-os": "ubuntu-2204", "tn-template": "ubuntu-lts"}})
    assert org.make_plan(inv, RANGES, OS_MAP).actions == []


def test_refuses_while_a_range_is_provisioning_or_destroying():
    for state in ("provisioning", "destroying"):
        plan = org.make_plan(TREE, [*RANGES, {"id": "0123abcd-0", "name": "b", "state": state}], OS_MAP)
        assert plan.refuse and state in plan.refuse[0]


def test_without_the_database_folders_keep_their_id():
    plan = org.make_plan(TREE, None, OS_MAP)
    assert _steps(plan, "rename") == []
    assert any("not read" in n for n in plan.notes)
    tags = {ref: want for ref, _, want in _steps(plan, "tag")}
    assert tags["VirtualMachine:vm-1"] == {"tn-type": "range-vm", "tn-range": RID[:8]}


def test_a_name_already_in_the_target_is_left_and_its_folder_kept():
    inv = _inv([*[(v.moid, TREE.rel(v.path), v.template) for v in TREE.vms],
                ("vm-30", "truenorth/templates/srv2022", True)], TREE.folders)
    plan = org.make_plan(inv, RANGES, OS_MAP)
    assert ("/DC-Lab/vm/truenorth/templates/srv2022", "/DC-Lab/vm/TrueNorth/Templates") not in _steps(plan, "move")
    assert any("srv2022" in n and "left where it is" in n for n in plan.notes)
    assert ("/DC-Lab/vm/truenorth/templates",) not in _steps(plan, "rmdir")
    assert ("/DC-Lab/vm/truenorth",) not in _steps(plan, "rmdir")


def test_a_range_that_already_has_a_new_folder_gets_its_vms_moved_in():
    inv = _inv([(v.moid, TREE.rel(v.path), v.template) for v in TREE.vms],
               TREE.folders | {"TrueNorth/Ranges", f"TrueNorth/Ranges/old-{RID[:8]}"})
    plan = org.make_plan(inv, RANGES, OS_MAP)
    moves = _steps(plan, "move")
    assert (f"/DC-Lab/vm/truenorth/ranges/{RID[:8]}/{RID[:8]}-ubu01", f"/DC-Lab/vm/TrueNorth/Ranges/old-{RID[:8]}") in moves
    assert (f"/DC-Lab/vm/truenorth/ranges/{RID[:8]}",) in _steps(plan, "rmdir")


# -- doing it -----------------------------------------------------------------------------

class FakeGovc:
    def __init__(self, outputs: dict[str, str] | None = None):
        self.calls: list[list[str]] = []
        self.outputs = outputs or {}

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        return next((out for key, out in self.outputs.items() if " ".join(argv).startswith(key)), "")


def test_an_old_folder_is_deleted_only_when_vcenter_says_it_is_empty():
    plan = org.Plan()
    plan.add("rmdir", "/DC-Lab/vm/truenorth/templates")
    plan.add("rmdir", "/DC-Lab/vm/truenorth")
    run = FakeGovc({"ls /DC-Lab/vm/truenorth/templates": "/DC-Lab/vm/truenorth/templates/late-arrival\n"})
    assert org.execute(plan, run, log=lambda *_: None) == 0
    destroyed = [c for c in run.calls if c[0] == "object.destroy"]
    assert destroyed == [["object.destroy", "/DC-Lab/vm/truenorth"]]


def test_a_single_category_value_is_detached_before_the_new_one():
    cats = json.dumps([{"id": "urn:c:1", "name": "tn-type"}, {"id": "urn:c:2", "name": "owner"}])
    attached = json.dumps([{"tags": [{"name": "golden-template", "category_id": "urn:c:1"},
                                     {"name": "alice", "category_id": "urn:c:2"}]}])
    run = FakeGovc({"tags.category.ls": cats, "tags.attached.ls": attached,
                    "tags.ls -c tn-type": json.dumps([{"name": "golden-template"}, {"name": "template-copy"}])})
    org.tag_one("VirtualMachine:vm-9", "template-copy", None, None, os_map=OS_MAP, run=run, log=lambda *_: None)
    changes = [c for c in run.calls if c[0] in ("tags.attach", "tags.detach", "tags.create", "tags.category.create")]
    assert changes == [["tags.detach", "-c", "tn-type", "golden-template", "VirtualMachine:vm-9"],
                       ["tags.attach", "-c", "tn-type", "template-copy", "VirtualMachine:vm-9"]]


def test_missing_categories_are_created_single_cardinality():
    run = FakeGovc()
    org.tag_one("/DC/vm/TrueNorth/Templates/kali", "golden-template", "kali", None, os_map=OS_MAP, run=run,
                log=lambda *_: None)
    created = [c for c in run.calls if c[0] == "tags.category.create"]
    assert {c[-1] for c in created} == {"tn-type", "tn-template", "tn-os"}
    assert all("-m" not in c and c[1:3] == ["-t", "VirtualMachine"] for c in created)  # no -m: SINGLE
    assert ["tags.attach", "-c", "tn-os", "kali-2024", "/DC/vm/TrueNorth/Templates/kali"] in run.calls


def test_one_failed_step_is_reported_and_the_rest_still_run():
    plan = org.Plan()
    plan.add("move", "/a/x", "/b")
    plan.add("move", "/a/y", "/b")

    def run(argv):
        if argv[1] == "/a/x":
            raise subprocess.CalledProcessError(1, argv, stderr="govc: no such object")
        return ""

    lines: list[str] = []
    assert org.execute(plan, run, log=lines.append) == 1
    assert any("no such object" in ln for ln in lines) and any("/a/y" in ln for ln in lines)


def test_the_range_reader_prints_names_and_templates_only():
    rows = [(RID, "Ubuntu Lab", "ready", json.dumps({"vms": [{"name": "a", "template": "t", "ip": "10.1.1.1",
                                                               "password": "s3cret"}], "uplink": {"ip": "x"}})),
            ("0badc0de", "gone", "destroyed", None), ("12345678", "b", "provisioning", "not json")]
    out = ranges_inprocess.summarise(rows)
    assert out == [{"id": RID, "name": "Ubuntu Lab", "state": "ready", "vms": [{"name": "a", "template": "t"}]},
                   {"id": "12345678", "name": "b", "state": "provisioning", "vms": []}]
    assert "s3cret" not in json.dumps(out) and "10.1.1.1" not in json.dumps(out)


# -- the shell wrapper ----------------------------------------------------------------------

GOVC_STUB = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB_STATE/calls"
case "$1" in
  about) echo '{"About": {"ApiType": "VirtualCenter"}}' ;;
esac
"""


@pytest.fixture
def wrapper(tmp_path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {"govc": GOVC_STUB, "docker": "#!/usr/bin/env bash\nexit 1\n"}.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env_file = tmp_path / ".env.production"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GOVC_", "TN_"))}
    env.update(PATH=f"{bindir}:{os.environ['PATH']}", STUB_STATE=str(tmp_path), TN_LAB_PYTHON=sys.executable)

    def run(*args, folder="TrueNorth/Ranges"):
        env_file.write_text("VSPHERE_URL=https://10.0.0.5\nVSPHERE_USERNAME=svc-truenorth@vsphere.local\n"
                            "VSPHERE_PASSWORD='S3cret!pw'\n" + (f"VSPHERE_RANGE_FOLDER={folder}\n" if folder else ""))
        return subprocess.run(["bash", str(LAB / "vcenter-organize.sh"), "--env-file", str(env_file), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    run.state = tmp_path
    return run


def test_dry_run_plans_without_the_database_and_never_prints_the_password(wrapper):
    got = wrapper("--dry-run")
    assert got.returncode == 0, got.stderr
    assert "plan  create folder /DC-Lab/vm/TrueNorth/Ranges" in got.stdout  # the stub's inventory is empty
    assert "Dry run: nothing changed" in got.stdout
    assert not [c for c in (wrapper.state / "calls").read_text().splitlines() if c.startswith("folder.create")]
    assert "could not read the TrueNorth ranges" in got.stderr
    assert "S3cret!pw" not in got.stdout + got.stderr


def test_apply_without_the_range_list_is_refused(wrapper):
    got = wrapper("--yes")
    assert got.returncode != 0 and "--no-db only if you are sure" in got.stderr


def test_apply_refused_while_the_worker_still_uses_the_old_folder(wrapper):
    got = wrapper("--yes", "--no-db", folder="truenorth/ranges")
    assert got.returncode != 0 and "VSPHERE_RANGE_FOLDER=truenorth/ranges" in got.stderr
    assert "--force-folder" in got.stderr
    warned = wrapper("--dry-run", folder="truenorth/ranges")
    assert warned.returncode == 0 and "new ranges would still go there" in warned.stderr


def test_apply_with_a_range_list(wrapper, tmp_path):
    f = tmp_path / "ranges.json"
    f.write_text(json.dumps(RANGES))
    got = wrapper("--yes", "--ranges", str(f))
    assert got.returncode == 0, got.stderr
    assert "done: 3 step(s) ok, 0 failed" in got.stdout
    assert "folder.create /DC-Lab/vm/TrueNorth/Ranges" in (wrapper.state / "calls").read_text()


# -- build.sh replicate tags its templates ------------------------------------------------

REPLICATE_STUB = r"""#!/usr/bin/env bash
set -u
echo "$*" >> "$STUB_STATE/calls"
case "$1" in
  about) echo "API type:     VirtualCenter" ;;
  find)
    name="${@: -1}"; type="$4"
    if [ "$type" = h ]; then echo "/DC/host/CL/esx02.truenorth.lab"
    elif [ "$name" = srv2022 ]; then echo "/DC/vm/TrueNorth/Templates/srv2022"
    elif [ -e "$STUB_STATE/vm/$name" ]; then echo "/DC/vm/TrueNorth/Templates/$name"
    fi ;;
  object.collect) cat "$STUB_STATE/vm/$(basename "$3")" ;;
  vm.clone) mkdir -p "$STUB_STATE/vm"; echo false > "$STUB_STATE/vm/${@: -1}" ;;
  vm.markastemplate) echo true > "$STUB_STATE/vm/$(basename "$2")" ;;
esac
"""


@pytest.fixture
def replicate(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "govc").write_text(REPLICATE_STUB)
    (bindir / "govc").chmod(0o755)
    (bindir / "python3").symlink_to(sys.executable)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_STATE": str(tmp_path),
           "GOVC_URL": "https://vcsa.test", "REPLICA_DATE": "20261019", "RANGE_HOSTS": "esx02"}
    env.pop("TN_TAGS", None)

    def run(**extra):
        got = subprocess.run(["bash", str(ROOT / "infra/vsphere/packer/build.sh"), "replicate", "srv2022"],
                             env={**env, **extra}, capture_output=True, text=True, timeout=120)
        calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
        return got, calls

    return run


def test_replicate_tags_the_template_and_its_copy(replicate):
    got, calls = replicate()
    assert got.returncode == 0, got.stderr
    assert "tags.attach -c tn-type golden-template /DC/vm/TrueNorth/Templates/srv2022" in calls
    assert "tags.attach -c tn-type template-copy /DC/vm/TrueNorth/Templates/srv2022@esx02-20261019" in calls
    assert "tags.attach -c tn-os windows-server-2022 /DC/vm/TrueNorth/Templates/srv2022@esx02-20261019" in calls
    # Tagging comes after the copy is a template, never before it exists.
    assert calls.index("vm.markastemplate /DC/vm/TrueNorth/Templates/srv2022@esx02-20261019") \
        < calls.index("tags.attach -c tn-type template-copy /DC/vm/TrueNorth/Templates/srv2022@esx02-20261019")


def test_replicate_tags_nothing_with_tags_off_or_in_a_dry_run(replicate):
    got, calls = replicate(TN_TAGS="0")
    assert got.returncode == 0 and not [c for c in calls if c.startswith("tags.")]
    got, calls = replicate(DRY_RUN="1")
    assert got.returncode == 0 and "DRY_RUN: tag /DC/vm/TrueNorth/Templates/srv2022 tn-type=golden-template" in got.stdout
    assert not [c for c in calls if c.startswith("tags.")]


def test_a_tagging_failure_never_fails_replicate(replicate, tmp_path):
    broken = tmp_path / "bin" / "python3"
    broken.unlink()
    broken.write_text("#!/usr/bin/env bash\nexit 1\n")
    broken.chmod(0o755)
    got, calls = replicate()
    assert got.returncode == 0, got.stderr
    assert "not tagged (vCenter tagging failed)" in got.stderr and "replicate: OK srv2022@esx02-20261019" in got.stdout
