"""Build manifest: the evidence record for one range build.

Inputs:  build/<ID>/plan.json   (compiled plan)
         build/<ID>/results.json (written by the Ansible build/validate playbooks)
Outputs: build/<ID>/manifest.json, build/<ID>/manifest.md, optional manifest.json.asc
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .common import RangeError, load_json, now_utc, sha256_obj, write_json
from .plan import plan_dir


def build_manifest(root: Path, rid: str, sign: bool = False) -> Path:
    d = plan_dir(root, rid)
    plan = load_json(d / "plan.json")
    rpath = d / "results.json"
    if not rpath.exists():
        raise RangeError(f"no results for {rid}: build/{rid}/results.json is written by range_build/range_validate")
    results = load_json(rpath)

    checks = []
    for iid, inst in (results.get("instances") or {}).items():
        for c in inst.get("validation", []) or []:
            checks.append({"instance": iid, **c})
    failed = [c for c in checks if not c.get("pass")]
    expected_instances = {i["id"] for i in plan["instances"]}
    missing = sorted(expected_instances - set((results.get("instances") or {}).keys()))

    isolation_checks = [c for c in checks if c.get("check") == "isolation"]
    verdict = "PASS"
    reasons = []
    if failed:
        verdict, reasons = "FAIL", reasons + [f"{len(failed)} validation check(s) failed"]
    if missing:
        verdict, reasons = "FAIL", reasons + [f"no results for instance(s): {', '.join(missing)}"]
    if not isolation_checks:
        verdict, reasons = "FAIL", reasons + ["no isolation checks were recorded"]
    if results.get("plan_sha256") != plan["plan_sha256"]:
        verdict, reasons = "FAIL", reasons + ["results were produced from a different plan"]

    manifest = {
        "manifest_version": 1,
        "range_id": rid,
        "generated_at": now_utc().isoformat(timespec="seconds"),
        "verdict": verdict,
        "reasons": reasons,
        "plan_sha256": plan["plan_sha256"],
        "source_sha256": plan["source_sha256"],
        "source_file": plan["source_file"],
        "range": plan["range"],
        "templates": plan["templates"],
        "totals": plan["totals"],
        "instances": [{
            "id": i["id"],
            "slot": i["slot"],
            "cidr_block": i["cidr_block"],
            "networks": [{k: n[k] for k in ("portgroup", "vlan", "cidr", "gateway")} for n in i["networks"]],
            "vms": [{
                "vm_name": v["vm_name"],
                "template_alias": v["template_alias"],
                "addresses": [n["ip"] for n in v["nics"]],
                "moid": ((results.get("instances") or {}).get(i["id"], {}).get("vms", {}) or {})
                        .get(v["vm_name"], {}).get("moid"),
            } for v in i["vms"]],
        } for i in plan["instances"]],
        "run": {k: results.get(k) for k in ("action", "started", "finished", "operator",
                                             "ansible_version", "collections")},
        "checks": checks,
    }
    manifest["manifest_sha256"] = sha256_obj(manifest)
    out = d / "manifest.json"
    write_json(out, manifest)
    (d / "manifest.md").write_text(render_md(manifest))

    if sign:
        gpg = shutil.which("gpg")
        if not gpg:
            raise RangeError("--sign requested but gpg is not installed")
        subprocess.run([gpg, "--batch", "--yes", "--armor", "--detach-sign", str(out)], check=True)
    return out


def render_md(m: dict) -> str:
    L = [f"# Range manifest: {m['range_id']}", "",
         f"**Verdict:** {m['verdict']}" + (f" ({'; '.join(m['reasons'])})" if m["reasons"] else ""), "",
         "| Field | Value |", "|---|---|",
         f"| Generated | {m['generated_at']} |",
         f"| Owner | {m['range']['owner']} |",
         f"| Exercise | {m['range']['exercise']} |",
         f"| Expires | {m['range']['expires']} |",
         f"| Plan SHA-256 | `{m['plan_sha256']}` |",
         f"| Manifest SHA-256 | `{m['manifest_sha256']}` |",
         f"| Instances / VMs | {m['totals']['instances']} / {m['totals']['vms']} |", "",
         "## Templates", "", "| Alias | vCenter name | Build ID | Build date | Source SHA-256 |",
         "|---|---|---|---|---|"]
    for a, t in m["templates"].items():
        L.append(f"| {a} | {t.get('vcenter_name')} | {t.get('build_id')} | {t.get('build_date')} | "
                 f"`{str(t.get('source_sha256'))[:16]}…` |")
    L += ["", "## Validation", "", "| Instance | Check | From | Target | Expected | Result | Pass |",
          "|---|---|---|---|---|---|---|"]
    for c in m["checks"]:
        L.append(f"| {c.get('instance')} | {c.get('check')} | {c.get('vm', '')} | {c.get('target', '')} | "
                 f"{c.get('expected', '')} | {c.get('result', '')} | {'yes' if c.get('pass') else 'NO'} |")
    return "\n".join(L) + "\n"
