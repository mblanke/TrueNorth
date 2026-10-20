"""scripts/lab/vcenter_organize.py against govmomi's vcsim, through the real govc.

Starts its own vcsim on a free local port (never a configured vCenter) and seeds the tree
the work vCenter had on 2026-10-20: range VMs in ``truenorth/ranges/2fc8d04d``, an old
hand-made ``kali-linux`` in ``truenorth/templates``, the kit's templates and host copies in
``TrueNorth/Templates``, and VMs that are not TrueNorth's. Then ``apply`` and read back.

Skipped when ``vcsim`` or ``govc`` is not on PATH (CI's unit job has neither).

What vcsim does not do: it does not enforce single tag cardinality (vCenter does), so the
detach-before-attach that makes a re-tag work on vCenter is checked by reading the tags back,
and by the unit tests' stub.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.vcsim]

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts/lab/vcenter_organize.py"
RID = "2fc8d04d-1111-4222-8333-444455556666"

if not (shutil.which("vcsim") and shutil.which("govc")):
    pytest.skip("vcsim and govc are needed (go install github.com/vmware/govmomi/vcsim@v0.56.0)",
                allow_module_level=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def sim(tmp_path):
    port = _free_port()
    proc = subprocess.Popen(["vcsim", "-l", f"127.0.0.1:{port}"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    env = {**os.environ, "GOVC_URL": f"https://user:pass@127.0.0.1:{port}/sdk", "GOVC_INSECURE": "1",
           "GOVC_DATACENTER": "DC0", "GOVC_PERSIST_SESSION": "false"}
    for _ in range(100):
        if subprocess.run(["govc", "about"], env=env, capture_output=True).returncode == 0:
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("vcsim did not start")

    def govc(*args: str, check: bool = True) -> str:
        r = subprocess.run(["govc", *args], env=env, capture_output=True, text=True)
        if check and r.returncode:
            raise AssertionError(f"govc {' '.join(args)}: {r.stderr}")
        return r.stdout

    def vm(folder: str, name: str, template: bool = False) -> None:
        govc("vm.create", "-on=false", "-pool", "/DC0/host/DC0_C0/Resources", "-ds", "LocalDS_0",
             "-net", "VM Network", "-folder", folder, name)
        if template:
            govc("vm.markastemplate", f"{folder}/{name}")

    def tool(*args: str, ranges: list | None = None) -> subprocess.CompletedProcess:
        extra = []
        if ranges is not None:
            f = tmp_path / "ranges.json"
            f.write_text(json.dumps(ranges))
            extra = ["--ranges", str(f)]
        return subprocess.run([sys.executable, str(TOOL), *args, *extra], env=env, capture_output=True, text=True,
                              timeout=300)

    govc("folder.create", "/DC0/vm/truenorth", "/DC0/vm/truenorth/ranges", f"/DC0/vm/truenorth/ranges/{RID[:8]}",
         "/DC0/vm/truenorth/templates", "/DC0/vm/TrueNorth", "/DC0/vm/TrueNorth/Templates")
    for n in ("ubu01", "ubu02", "ubu03"):
        vm(f"/DC0/vm/truenorth/ranges/{RID[:8]}", f"{RID[:8]}-{n}")
    vm("/DC0/vm/truenorth/templates", "kali-linux", template=True)
    for name in ("ubuntu-lts", "ubuntu-lts@esx02-20261020", "ubuntu-lts@esx03-20261020", "ubuntu-lts@esx04-20261020",
                 "kali", "srv2022"):
        vm("/DC0/vm/TrueNorth/Templates", name, template=True)
    vm("/DC0/vm/TrueNorth/Templates", "tn-testclone-srv2022-esx02")
    vm("/DC0/vm", "WIN11-TMPL", template=True)
    vm("/DC0/vm", "TN-DEPOT01")
    vm("/DC0/vm", "TN-MGMT01")
    govc.tool = tool
    govc.vm = vm
    yield govc
    proc.kill()
    proc.wait()


def _tags(govc, path: str) -> dict[str, str]:
    cats = {c["id"]: c["name"] for c in json.loads(govc("tags.category.ls", "-json") or "[]") or []}
    out = {}
    for row in json.loads(govc("tags.attached.ls", "-json", "-l", "-r", path) or "[]") or []:
        for t in row.get("tags") or []:
            out[cats.get(t["category_id"], t["category_id"])] = t["name"]
    return out


def _paths(govc) -> set[str]:
    return set(govc("find", "/DC0/vm", "-type", "m").split())


RANGES = [{"id": RID, "name": "Ubuntu Lab", "state": "ready",
           "vms": [{"name": f"{RID[:8]}-ubu01", "template": "ubuntu-lts@esx02-20261020"}]}]


def test_apply_consolidates_and_tags_then_has_nothing_left_to_do(sim):
    plan = sim.tool("plan", "--dc", "DC0", ranges=RANGES)
    assert plan.returncode == 0, plan.stderr
    assert sim("find", "/DC0/vm/truenorth", "-type", "m").strip()  # a plan changes nothing

    got = sim.tool("apply", "--dc", "DC0", ranges=RANGES)
    assert got.returncode == 0, got.stdout + got.stderr
    paths = _paths(sim)
    label = f"ubuntu-lab-{RID[:8]}"
    for n in ("ubu01", "ubu02", "ubu03"):
        assert f"/DC0/vm/TrueNorth/Ranges/{label}/{RID[:8]}-{n}" in paths
    assert "/DC0/vm/TrueNorth/Templates/_retired/kali-linux" in paths  # moved, not deleted
    assert "/DC0/vm/TrueNorth/Templates/ubuntu-lts@esx03-20261020" in paths  # Templates stays flat
    assert not any(p.startswith("/DC0/vm/truenorth") for p in paths)
    assert "/DC0/vm/truenorth" not in sim("ls", "/DC0/vm").split()  # the empty old tree is gone
    assert "/DC0/vm/WIN11-TMPL" in paths and "/DC0/vm/DC0_H0_VM0" in paths  # not TrueNorth's: where they were

    t = "/DC0/vm/TrueNorth/Templates"
    assert _tags(sim, f"{t}/ubuntu-lts") == {"tn-type": "golden-template", "tn-template": "ubuntu-lts",
                                            "tn-os": "ubuntu-2404"}
    assert _tags(sim, f"{t}/ubuntu-lts@esx02-20261020") == {"tn-type": "template-copy", "tn-template": "ubuntu-lts",
                                                           "tn-os": "ubuntu-2404"}
    assert _tags(sim, f"{t}/srv2022")["tn-os"] == "windows-server-2022"
    assert _tags(sim, f"{t}/tn-testclone-srv2022-esx02")["tn-type"] == "test-clone"
    r = f"/DC0/vm/TrueNorth/Ranges/{label}"
    assert _tags(sim, f"{r}/{RID[:8]}-ubu01") == {"tn-type": "range-vm", "tn-range": label,
                                                 "tn-template": "ubuntu-lts", "tn-os": "ubuntu-2404"}
    assert _tags(sim, f"{r}/{RID[:8]}-ubu02") == {"tn-type": "range-vm", "tn-range": label}
    assert _tags(sim, "/DC0/vm/TN-DEPOT01") == {"tn-type": "depot"}
    assert _tags(sim, "/DC0/vm/TN-MGMT01") == {"tn-type": "mgmt"}
    for untouched in ("/DC0/vm/WIN11-TMPL", f"{t}/_retired/kali-linux", "/DC0/vm/DC0_H0_VM0"):
        assert _tags(sim, untouched) == {}

    again = sim.tool("plan", "--dc", "DC0", ranges=RANGES)
    assert again.returncode == 0 and "nothing to do" in again.stdout, again.stdout


def test_apply_refuses_while_a_range_is_provisioning(sim):
    busy = [*RANGES, {"id": "0badc0de-0000-4000-8000-000000000000", "name": "x", "state": "provisioning"}]
    got = sim.tool("apply", "--dc", "DC0", ranges=busy)
    assert got.returncode == 3 and "provisioning" in got.stderr
    assert sim("find", "/DC0/vm/truenorth", "-type", "m").strip()  # nothing moved
    assert json.loads(sim("tags.category.ls", "-json") or "[]") in ([], None)  # nothing tagged


def test_without_the_database_range_folders_keep_their_id(sim):
    got = sim.tool("apply", "--dc", "DC0")
    assert got.returncode == 0, got.stdout + got.stderr
    paths = _paths(sim)
    assert f"/DC0/vm/TrueNorth/Ranges/{RID[:8]}/{RID[:8]}-ubu01" in paths
    assert _tags(sim, f"/DC0/vm/TrueNorth/Ranges/{RID[:8]}/{RID[:8]}-ubu01")["tn-range"] == RID[:8]
    # With the database later, the id-only folder is renamed in place.
    got = sim.tool("apply", "--dc", "DC0", ranges=RANGES)
    assert got.returncode == 0, got.stdout + got.stderr
    assert f"/DC0/vm/TrueNorth/Ranges/ubuntu-lab-{RID[:8]}/{RID[:8]}-ubu01" in _paths(sim)


def test_a_name_taken_in_the_target_stays_where_it_is(sim):
    sim.vm("/DC0/vm/truenorth/templates", "srv2022-old", template=True)
    sim("object.rename", "/DC0/vm/truenorth/templates/srv2022-old", "srv2022")  # a second srv2022
    got = sim.tool("apply", "--dc", "DC0", ranges=RANGES)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "already in /DC0/vm/TrueNorth/Templates; left where it is" in got.stdout
    assert "/DC0/vm/truenorth/templates/srv2022" in _paths(sim)  # its folder stays: not empty
    assert "/DC0/vm/truenorth/templates" in sim("ls", "/DC0/vm/truenorth").split()


def test_tag_replaces_the_value_of_a_single_category(sim):
    path = "/DC0/vm/TrueNorth/Templates/kali"
    for tn_type in ("golden-template", "template-copy"):
        got = sim.tool("tag", path, "--type", tn_type, "--template", "kali")
        assert got.returncode == 0, got.stderr
    assert _tags(sim, path) == {"tn-type": "template-copy", "tn-template": "kali", "tn-os": "kali-2024"}
