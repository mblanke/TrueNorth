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
KEY = "2fc8d04d1111"
OS_MAP = org.load_os_map()


# -- one definition, kit and worker -----------------------------------------------------

def test_kit_and_worker_define_the_same_tags():
    from worker.provisioners import vsphere_api
    from worker.provisioners import vsphere_tags as worker_tags

    assert org.CATEGORIES == worker_tags.CATEGORIES
    assert org.TYPES == worker_tags.TYPES
    assert org.KEY_LEN == worker_tags.KEY_LEN == 12
    assert org.RANGES == "TrueNorth/Ranges" and f"{org.LEGACY}/ranges" in worker_tags.LEGACY_RANGE_ROOTS
    assert org.RETIRED.rsplit("/", 1)[1] == worker_tags.RETIRED_FOLDER
    assert org.RANGE_TAG.pattern.startswith(vsphere_api.RANGE_TAG)
    for name in ("Blue Team 3", "", None, f"lab-{KEY}", "x" * 90, "ARC² Week/1"):
        assert org.range_label(name, RID) == worker_tags.range_label(name, RID)
    for folder in (RID[:8], f"blue-{RID[:8]}", f"x{RID[:8]}", f"{RID[:8]}-x", "nothex99", KEY, f"b-{KEY}",
                   "x-2fc8d04d9999"):
        assert org.belongs_to(folder, RID) == worker_tags.belongs_to(folder, RID), folder
        assert org.is_legacy_folder(folder) == worker_tags.is_legacy_folder(folder), folder
    for source in ("ubuntu-lts@esx02-20261020", "library:srv2022", "kali"):
        assert org.template_id(source) == worker_tags.template_id(source)
    assert org.annotated_range(vsphere_api.range_annotation(RID)) == RID


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

def _inv(vms: list[tuple], folders: set[str], tags: dict | None = None) -> org.Inventory:
    """VMs as (moid, relative path, template[, annotation[, powered on]])."""
    out = []
    for moid, rel, template, *more in vms:
        annotation, on = (list(more) + ["", False])[:2]
        out.append(org.Vm(moid, f"/DC-Lab/vm/{rel}", template, dict((tags or {}).get(moid, {})), annotation, on))
    return org.Inventory("DC-Lab", out, folders)


ANN = f"TrueNorth range VM. Deleted with its range. truenorth-range:{RID}"  # vsphere_api.range_annotation
# The work vCenter's VM folders on 2026-10-20 (read by the coordinator), plus what must stay put.
TREE_VMS = [
    ("vm-1", f"truenorth/ranges/{RID[:8]}/{RID[:8]}-ubu01", False, ANN, True),
    ("vm-2", f"truenorth/ranges/{RID[:8]}/{RID[:8]}-ubu02", False, ANN, True),
    ("vm-3", f"truenorth/ranges/{RID[:8]}/{RID[:8]}-ubu03", False, ANN, True),
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
    ("vm-25", "TN-MGMT01", False),
]
TREE_FOLDERS = {"truenorth", "truenorth/ranges", f"truenorth/ranges/{RID[:8]}", "truenorth/templates",
                "TrueNorth", "TrueNorth/Templates", "Discovered virtual machine", "vCLS"}
TREE = _inv(TREE_VMS, TREE_FOLDERS)
RANGES = [{"id": RID, "name": "Ubuntu Lab", "state": "ready",
           "vms": [{"name": f"{RID[:8]}-ubu01", "template": "ubuntu-lts@esx02-20261020"}]},
          {"id": "0badc0de-0000-4000-8000-000000000000", "name": "gone", "state": "destroyed"}]
LABEL = f"ubuntu-lab-{KEY}"
REGISTERED = ["kali-linux", "ubuntu-lts"]  # golden-image template names (tn_ranges_inprocess.py)


def _steps(plan, kind: str) -> list[tuple]:
    return [a.args for a in plan.actions if a.kind == kind]


def test_plan_for_the_work_vcenter_tree():
    plan = org.make_plan(TREE, RANGES, OS_MAP, known_templates=REGISTERED)
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
    assert tags["VirtualMachine:vm-4"] == {"tn-type": "retired"}
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
    plan = org.make_plan(TREE, RANGES, OS_MAP, known_templates=REGISTERED)
    text = "\n".join(str(a) for a in plan.actions)
    for other in ("WIN11-TMPL", "vcsa01", "vCLS", "Discovered virtual machine", "vm-23", "/DC-Lab/vm/kali-linux"):
        assert other not in text, other
    assert all(a.args[0].startswith(("/DC-Lab/vm/truenorth", "/DC-Lab/vm/TrueNorth")) for a in plan.actions
               if a.kind in ("move", "rename", "rmdir"))


def test_an_unknown_template_under_truenorth_is_left_and_reported():
    plan = org.make_plan(TREE, RANGES, OS_MAP)  # kali-linux neither registered nor --retire
    assert not [m for m in _steps(plan, "move") if "kali-linux" in m[0]]
    assert any("kali-linux" in n and "--retire" in n for n in plan.notes)
    assert ("/DC-Lab/vm/truenorth/templates",) not in _steps(plan, "rmdir")  # not emptied: kept
    assert ("/DC-Lab/vm/truenorth",) not in _steps(plan, "rmdir")
    retired = org.make_plan(TREE, RANGES, OS_MAP, retire=["kali-linux"])
    assert ("/DC-Lab/vm/truenorth/templates/kali-linux", "/DC-Lab/vm/TrueNorth/Templates/_retired") \
        in _steps(retired, "move")


def test_a_powered_on_vm_is_never_retired():
    vms = [v if v[0] != "vm-4" else ("vm-4", "truenorth/templates/kali-linux", True, "", True) for v in TREE_VMS]
    plan = org.make_plan(_inv(vms, TREE_FOLDERS), RANGES, OS_MAP, retire=["kali-linux"])
    assert not [m for m in _steps(plan, "move") if "kali-linux" in m[0]]
    assert any("kali-linux" in n and "powered on" in n for n in plan.notes)


def test_a_vm_under_truenorth_that_is_not_truenorths_is_left():
    vms = [*TREE_VMS, ("vm-30", "truenorth/someones-vm", False),
           ("vm-31", f"truenorth/ranges/{RID[:8]}/stray-vm", False)]
    plan = org.make_plan(_inv(vms, TREE_FOLDERS), RANGES, OS_MAP, known_templates=REGISTERED)
    text = "\n".join(str(a) for a in plan.actions)
    assert "someones-vm" not in text and "stray-vm" not in text
    assert any("someones-vm" in n and "not recognised" in n for n in plan.notes)
    assert any("stray-vm" in n and "not a TrueNorth range VM" in n for n in plan.notes)
    # The range folder no longer moves whole (it holds a stranger): its VMs move one by one.
    assert (f"/DC-Lab/vm/truenorth/ranges/{RID[:8]}", "/DC-Lab/vm/TrueNorth/Ranges") not in _steps(plan, "move")
    assert (f"/DC-Lab/vm/truenorth/ranges/{RID[:8]}/{RID[:8]}-ubu01", f"/DC-Lab/vm/TrueNorth/Ranges/{LABEL}") \
        in _steps(plan, "move")
    assert not [r for r in _steps(plan, "rmdir") if r[0].startswith("/DC-Lab/vm/truenorth/ranges")]
    assert ("/DC-Lab/vm/truenorth",) not in _steps(plan, "rmdir")


def test_a_folder_that_was_already_empty_is_not_this_runs_to_remove():
    folders = TREE_FOLDERS | {"truenorth/old-empty"}
    plan = org.make_plan(_inv(TREE_VMS, folders), RANGES, OS_MAP, known_templates=REGISTERED)
    assert ("/DC-Lab/vm/truenorth/old-empty",) not in _steps(plan, "rmdir")
    assert any("old-empty" in n and "empty before this run" in n for n in plan.notes)
    assert ("/DC-Lab/vm/truenorth",) not in _steps(plan, "rmdir")  # it still holds old-empty


def test_two_ranges_sharing_an_id8_keep_the_old_folder_name():
    twin = {"id": f"{RID[:8]}-9999-4999-8999-999999999999", "name": "Twin", "state": "ready"}
    plan = org.make_plan(TREE, [*RANGES, twin], OS_MAP, known_templates=REGISTERED)
    assert not [r for r in _steps(plan, "rename") if r[0].endswith(f"/{RID[:8]}")]
    assert any("share id" in n for n in plan.notes)
    tags = {ref: want for ref, _, want in _steps(plan, "tag")}
    assert tags["VirtualMachine:vm-1"]["tn-range"] == LABEL  # its annotation names the range


def test_after_apply_the_plan_is_empty():
    plan = org.make_plan(TREE, RANGES, OS_MAP, known_templates=REGISTERED)
    tags = {ref.split(":")[1]: want for ref, _, want in _steps(plan, "tag")}
    done = _inv(
        [("vm-1", f"TrueNorth/Ranges/{LABEL}/{RID[:8]}-ubu01", False, ANN, True),
         ("vm-2", f"TrueNorth/Ranges/{LABEL}/{RID[:8]}-ubu02", False, ANN, True),
         ("vm-3", f"TrueNorth/Ranges/{LABEL}/{RID[:8]}-ubu03", False, ANN, True),
         ("vm-4", "TrueNorth/Templates/_retired/kali-linux", True),
         *[v for v in TREE_VMS if v[0] not in ("vm-1", "vm-2", "vm-3", "vm-4")]],
        {"TrueNorth", "TrueNorth/Templates", "TrueNorth/Templates/_retired", "TrueNorth/Ranges",
         f"TrueNorth/Ranges/{LABEL}", "Discovered virtual machine", "vCLS"},
        tags,
    )
    again = org.make_plan(done, RANGES, OS_MAP, known_templates=REGISTERED)
    assert again.actions == [] and not again.refuse


def test_a_range_vm_keeps_the_os_and_template_its_build_set():
    inv = _inv([("vm-1", f"TrueNorth/Ranges/{LABEL}/{RID[:8]}-ubu01", False, ANN)], {"TrueNorth", "TrueNorth/Templates",
               "TrueNorth/Ranges", f"TrueNorth/Ranges/{LABEL}"},
               {"vm-1": {"tn-type": "range-vm", "tn-range": LABEL, "tn-os": "ubuntu-2204", "tn-template": "ubuntu-lts"}})
    assert org.make_plan(inv, RANGES, OS_MAP).actions == []


@pytest.mark.parametrize("state", ["created", "provisioning", "destroying", "starting", "stopping", "queued", None])
def test_refuses_unless_every_range_is_settled(state):
    plan = org.make_plan(TREE, [*RANGES, {"id": "0123abcd-0", "name": "b", "state": state}], OS_MAP)
    assert plan.refuse and str(state) in plan.refuse[0]
    for settled in org.SETTLED_STATES:
        assert not org.make_plan(TREE, [*RANGES, {"id": "0123abcd-0", "name": "b", "state": settled}], OS_MAP).refuse


def test_without_the_database_folders_keep_their_names():
    plan = org.make_plan(TREE, None, OS_MAP)
    assert _steps(plan, "rename") == []
    assert any("not read" in n for n in plan.notes)
    tags = {ref: want for ref, _, want in _steps(plan, "tag")}
    assert tags["VirtualMachine:vm-1"] == {"tn-type": "range-vm", "tn-range": KEY}  # from its annotation


def test_a_name_already_in_the_target_is_left_and_its_folder_kept():
    inv = _inv([*TREE_VMS, ("vm-30", "truenorth/templates/srv2022", True)], TREE_FOLDERS)
    plan = org.make_plan(inv, RANGES, OS_MAP, known_templates=REGISTERED)
    assert ("/DC-Lab/vm/truenorth/templates/srv2022", "/DC-Lab/vm/TrueNorth/Templates") not in _steps(plan, "move")
    assert any("srv2022" in n and "left where it is" in n for n in plan.notes)
    assert ("/DC-Lab/vm/truenorth/templates",) not in _steps(plan, "rmdir")
    assert ("/DC-Lab/vm/truenorth",) not in _steps(plan, "rmdir")


def test_a_range_that_already_has_a_new_folder_gets_its_vms_moved_in():
    inv = _inv(TREE_VMS, TREE_FOLDERS | {"TrueNorth/Ranges", f"TrueNorth/Ranges/old-{KEY}"})
    plan = org.make_plan(inv, RANGES, OS_MAP, known_templates=REGISTERED)
    moves = _steps(plan, "move")
    assert (f"/DC-Lab/vm/truenorth/ranges/{RID[:8]}/{RID[:8]}-ubu01", f"/DC-Lab/vm/TrueNorth/Ranges/old-{KEY}") in moves
    assert (f"/DC-Lab/vm/truenorth/ranges/{RID[:8]}",) in _steps(plan, "rmdir")


def test_ranges_input_old_and_new_shape():
    assert org.load_ranges(RANGES) == (RANGES, [])
    assert org.load_ranges({"ranges": RANGES, "templates": ["kali-linux", ""]}) == (RANGES, ["kali-linux"])


# -- doing it -----------------------------------------------------------------------------

class FakeGovc:
    def __init__(self, outputs: dict[str, str] | None = None):
        self.calls: list[list[str]] = []
        self.outputs = outputs or {}

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        return next((out for key, out in self.outputs.items() if " ".join(argv).startswith(key)), "")


def _children(*morefs: str) -> str:
    """``govc object.collect -json <folder> childEntity`` as govc prints it."""
    return json.dumps([{"name": "childEntity", "op": "assign",
                        "val": {"_value": [{"type": "VirtualMachine", "value": m} for m in morefs] or None}}])


def test_an_old_folder_is_deleted_only_when_the_property_collector_says_it_is_empty():
    plan = org.Plan()
    plan.add("rmdir", "/DC-Lab/vm/truenorth/templates")
    plan.add("rmdir", "/DC-Lab/vm/truenorth/ranges")
    plan.add("rmdir", "/DC-Lab/vm/truenorth")
    run = FakeGovc({"object.collect -json /DC-Lab/vm/truenorth/templates childEntity": _children("vm-99"),
                    "object.collect -json /DC-Lab/vm/truenorth/ranges childEntity": "not json",
                    "object.collect -json /DC-Lab/vm/truenorth childEntity": _children()})
    assert org.execute(plan, run, log=lambda *_: None) == 0
    destroyed = [c for c in run.calls if c[0] == "object.destroy"]
    assert destroyed == [["object.destroy", "/DC-Lab/vm/truenorth"]]  # late arrival kept; unreadable kept


def test_no_folder_is_deleted_after_a_failed_step():
    plan = org.Plan()
    plan.add("move", "/DC-Lab/vm/truenorth/templates/x", "/DC-Lab/vm/TrueNorth/Templates")
    plan.add("rmdir", "/DC-Lab/vm/truenorth/templates")

    def run(argv):
        if argv[0] == "object.mv":
            raise subprocess.CalledProcessError(1, argv, stderr="govc: busy")
        return _children()

    calls: list[list[str]] = []
    assert org.execute(plan, lambda a: calls.append(a) or run(a), log=lambda *_: None) == 1
    assert not [c for c in calls if c[0] == "object.destroy"]


def test_apply_reads_the_ranges_again_before_changing_anything(tmp_path, monkeypatch):
    """The plan is made from one read; a range that starts building before the first change
    is caught by a second read and nothing is changed."""
    reads = iter([{"ranges": RANGES}, {"ranges": [*RANGES, {"id": "0123abcd-0", "name": "b", "state": "provisioning"}]}])
    script = tmp_path / "ranges.sh"
    state = tmp_path / "n"
    state.write_text("0")
    payloads = [json.dumps(next(reads)), json.dumps(next(reads))]
    for i, p in enumerate(payloads):
        (tmp_path / f"r{i}.json").write_text(p)
    script.write_text(f'n=$(cat {state}); echo $((n+1)) > {state}; cat {tmp_path}/r$n.json\n')
    monkeypatch.setattr(org, "read_inventory", lambda dc: TREE)
    executed = []
    monkeypatch.setattr(org, "execute", lambda plan, *a, **k: executed.append(plan) or 0)
    assert org.main(["apply", "--dc", "DC-Lab", "--ranges-cmd", f"bash {script}"]) == 3
    assert executed == [] and state.read_text().strip() == "2"


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

# docker compose: `run ... api ...tn_ranges_inprocess.py` prints $STUB_RANGES (fails when unset);
# `exec -T worker-provision printenv VSPHERE_RANGE_FOLDER` prints $STUB_WORKER_FOLDER (fails when unset).
DOCKER_STUB = r"""#!/usr/bin/env bash
echo "docker $*" >> "$STUB_STATE/calls"
case " $* " in
  *" exec "*) [ -n "${STUB_WORKER_FOLDER:-}" ] || exit 1; echo "$STUB_WORKER_FOLDER" ;;
  *" run "*) [ -n "${STUB_RANGES:-}" ] || exit 1; cat "$STUB_RANGES" ;;
  *) exit 1 ;;
esac
"""


@pytest.fixture
def wrapper(tmp_path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {"govc": GOVC_STUB, "docker": DOCKER_STUB}.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env_file = tmp_path / ".env.production"
    app = tmp_path / "app"
    (app / "infra/platform/docker").mkdir(parents=True)
    ranges = tmp_path / "ranges.json"
    ranges.write_text(json.dumps({"ranges": RANGES, "templates": []}))
    base = {k: v for k, v in os.environ.items() if not k.startswith(("GOVC_", "TN_", "STUB_"))}
    base.update(PATH=f"{bindir}:{os.environ['PATH']}", STUB_STATE=str(tmp_path), TN_LAB_PYTHON=sys.executable)

    def run(*args, folder="TrueNorth/Ranges", worker="TrueNorth/Ranges", db=True):
        env_file.write_text("VSPHERE_URL=https://10.0.0.5\nVSPHERE_USERNAME=svc-truenorth@vsphere.local\n"
                            "VSPHERE_PASSWORD='S3cret!pw'\n" + (f"VSPHERE_RANGE_FOLDER={folder}\n" if folder else ""))
        env = {**base, **({"STUB_WORKER_FOLDER": worker} if worker else {}),
               **({"STUB_RANGES": str(ranges)} if db else {})}
        return subprocess.run(["bash", str(LAB / "vcenter-organize.sh"), "--env-file", str(env_file),
                               "--app-dir", str(app), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    run.state = tmp_path
    return run


def _calls(wrapper) -> list[str]:
    path = wrapper.state / "calls"
    return path.read_text().splitlines() if path.exists() else []


def test_dry_run_never_prints_the_password_and_changes_nothing(wrapper):
    got = wrapper("--dry-run", db=False, worker=None)
    assert got.returncode == 0, got.stderr
    assert "plan  create folder /DC-Lab/vm/TrueNorth/Ranges" in got.stdout  # the stub's inventory is empty
    assert "Dry run: nothing changed" in got.stdout
    assert "could not read the TrueNorth ranges" in got.stderr
    assert "S3cret!pw" not in got.stdout + got.stderr
    assert not [c for c in _calls(wrapper) if c.startswith("folder.create")]


def test_apply_without_the_range_list_is_refused(wrapper):
    got = wrapper("--yes", db=False)
    assert got.returncode != 0 and "--no-db only if you are sure" in got.stderr


def test_apply_refused_while_the_env_file_has_the_old_folder(wrapper):
    got = wrapper("--yes", folder="truenorth/ranges")
    assert got.returncode != 0 and "VSPHERE_RANGE_FOLDER=truenorth/ranges" in got.stderr
    assert "--force-folder" in got.stderr
    warned = wrapper("--dry-run", folder="truenorth/ranges")
    assert warned.returncode == 0 and "new ranges would still go there" in warned.stderr


def test_apply_refused_while_the_running_worker_has_the_old_folder(wrapper):
    """The file says TrueNorth/Ranges but the worker was not restarted: what it builds with wins."""
    got = wrapper("--yes", worker="truenorth/ranges")
    assert got.returncode != 0 and "running worker-provision has VSPHERE_RANGE_FOLDER=truenorth/ranges" in got.stderr
    assert any(c.startswith("docker ") and "exec -T worker-provision printenv VSPHERE_RANGE_FOLDER" in c
               for c in _calls(wrapper))


def test_apply_refused_when_the_running_worker_cannot_be_asked(wrapper):
    got = wrapper("--yes", worker=None)
    assert got.returncode != 0 and "could not read VSPHERE_RANGE_FOLDER from the running worker-provision" in got.stderr
    forced = wrapper("--yes", "--force-folder", worker=None)
    assert forced.returncode == 0, forced.stderr


def test_apply_reads_the_ranges_itself(wrapper):
    got = wrapper("--yes", "--retire", "kali-linux")
    assert got.returncode == 0, got.stderr
    assert "done: 3 step(s) ok, 0 failed" in got.stdout
    calls = _calls(wrapper)
    assert "folder.create /DC-Lab/vm/TrueNorth/Ranges" in calls
    # Once for the plan shown, then twice inside apply (before planning, before the first change).
    assert sum(1 for c in calls if c.startswith("docker ") and " run " in c) == 3


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
