"""Compare a live range (build/<ID>/live.json, collected by range_drift_collect.yml)
with its compiled plan. Exit code 3 from the CLI means drift was found.

live.json shape:
{
  "collected_at": "...",
  "instances": {
    "<instance id>": {
      "vms": [{"name": "...", "cpu": 2, "memory_mb": 4096, "portgroups": ["TN-..."]}],
      "portgroups": [{"name": "TN-...", "vlan": 2000}]
    }
  },
  "untracked": [{"type": "VirtualMachine", "name": "..."}]   # tagged with the range but not in any instance
}
"""

from __future__ import annotations

from pathlib import Path

from .common import load_json, write_json
from .plan import plan_dir


def compare(plan: dict, live: dict) -> list[dict]:
    findings: list[dict] = []
    live_inst = live.get("instances", {}) or {}
    for inst in plan["instances"]:
        iid = inst["id"]
        li = live_inst.get(iid)
        if li is None:
            findings.append({"instance": iid, "kind": "instance_missing", "detail": "no live objects found"})
            continue
        lvms = {v["name"]: v for v in li.get("vms", []) or []}
        lpgs = {p["name"]: p for p in li.get("portgroups", []) or []}
        for n in inst["networks"]:
            lp = lpgs.pop(n["portgroup"], None)
            if lp is None:
                findings.append({"instance": iid, "kind": "portgroup_missing", "object": n["portgroup"]})
            elif lp.get("vlan") != n["vlan"]:
                findings.append({"instance": iid, "kind": "vlan_mismatch", "object": n["portgroup"],
                                 "expected": n["vlan"], "actual": lp.get("vlan")})
        for name in sorted(lpgs):
            findings.append({"instance": iid, "kind": "portgroup_unexpected", "object": name})
        for v in inst["vms"]:
            lv = lvms.pop(v["vm_name"], None)
            if lv is None:
                findings.append({"instance": iid, "kind": "vm_missing", "object": v["vm_name"]})
                continue
            for field, exp in (("cpu", v["cpu"]), ("memory_mb", v["memory_mb"])):
                if lv.get(field) != exp:
                    findings.append({"instance": iid, "kind": f"{field}_mismatch", "object": v["vm_name"],
                                     "expected": exp, "actual": lv.get(field)})
            exp_pg = sorted(n["portgroup"] for n in v["nics"])
            act_pg = sorted(lv.get("portgroups", []) or [])
            if exp_pg != act_pg:
                findings.append({"instance": iid, "kind": "nic_mismatch", "object": v["vm_name"],
                                 "expected": exp_pg, "actual": act_pg})
        for name in sorted(lvms):
            findings.append({"instance": iid, "kind": "vm_unexpected", "object": name})
    for u in live.get("untracked", []) or []:
        findings.append({"instance": None, "kind": "tagged_untracked", "object": u.get("name"),
                         "detail": u.get("type")})
    return findings


def run(root: Path, rid: str, live_file: Path | None = None) -> tuple[list[dict], Path]:
    d = plan_dir(root, rid)
    plan = load_json(d / "plan.json")
    live = load_json(live_file or d / "live.json")
    findings = compare(plan, live)
    out = d / "drift.json"
    write_json(out, {"range_id": rid, "plan_sha256": plan["plan_sha256"],
                     "collected_at": live.get("collected_at"), "drift": bool(findings),
                     "findings": findings})
    return findings, out
