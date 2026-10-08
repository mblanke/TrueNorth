#!/usr/bin/env python3
"""Regenerate content/mitre/enterprise-attack-techniques.json from MITRE's STIX bundle.

The catalogue is the ATT&CK Enterprise matrix reduced to what TrueNorth validates
against: every technique and sub-technique id with its name, its tactics (as TA ids) and
whether MITRE has deprecated or revoked it (and, when revoked, by what), plus the tactic
ids themselves. Read by control-plane/{api/app,worker/worker}/attack_catalogue.py.

    python scripts/update_attack_catalogue.py                     # download, write
    python scripts/update_attack_catalogue.py --source bundle.json  # a local copy
    python scripts/update_attack_catalogue.py --check             # fail if the file differs

The source is MITRE's public CTI repository (enterprise-attack/enterprise-attack.json).
Using it is subject to MITRE's ATT&CK terms of use; see content/mitre/README.md, which
must stay next to the generated file.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "content/mitre/enterprise-attack-techniques.json"
SOURCE_URL = "https://raw.githubusercontent.com/mitre/cti/master/enterprise-attack/enterprise-attack.json"
ATTRIBUTION = (
    "MITRE ATT&CK(R) Enterprise, (c) The MITRE Corporation. Reproduced and distributed "
    "with the permission of The MITRE Corporation under the ATT&CK terms of use "
    "(https://attack.mitre.org/resources/legal-and-branding/terms-of-use/)."
)


def _attack_id(obj: dict) -> str | None:
    for ref in obj.get("external_references") or []:
        if ref.get("source_name") == "mitre-attack" and ref.get("external_id"):
            return ref["external_id"]
    return None


def build(bundle: dict, source: str) -> dict:
    objects = bundle.get("objects") or []
    by_stix_id: dict[str, str] = {}
    tactics: dict[str, dict] = {}
    shortname_to_ta: dict[str, str] = {}
    # The bundle carries no ATT&CK release number; its newest modification date pins it.
    modified = max((obj.get("modified", "") for obj in objects), default="")[:10] or None
    copyright_ = next(
        (
            obj["definition"]["statement"]
            for obj in objects
            if obj.get("type") == "marking-definition" and (obj.get("definition") or {}).get("statement")
        ),
        None,
    )

    for obj in objects:
        ext = _attack_id(obj)
        if ext:
            by_stix_id[obj["id"]] = ext
        if obj.get("type") == "x-mitre-tactic" and ext and not obj.get("revoked"):
            shortname = obj.get("x_mitre_shortname", "")
            tactics[ext] = {"name": obj.get("name", ""), "shortname": shortname}
            if obj.get("x_mitre_deprecated"):
                tactics[ext]["deprecated"] = True
            shortname_to_ta[shortname] = ext

    revoked_by: dict[str, str] = {}
    for obj in objects:
        if obj.get("type") == "relationship" and obj.get("relationship_type") == "revoked-by":
            src, dst = by_stix_id.get(obj.get("source_ref", "")), by_stix_id.get(obj.get("target_ref", ""))
            if src and dst:
                revoked_by[src] = dst

    techniques: dict[str, dict] = {}
    for obj in objects:
        if obj.get("type") != "attack-pattern":
            continue
        ext = _attack_id(obj)
        if not ext or not ext.startswith("T"):
            continue
        entry: dict = {
            "name": obj.get("name", ""),
            "tactics": sorted(
                {
                    shortname_to_ta[p["phase_name"]]
                    for p in obj.get("kill_chain_phases") or []
                    if p.get("kill_chain_name") == "mitre-attack" and p.get("phase_name") in shortname_to_ta
                }
            ),
        }
        if obj.get("x_mitre_deprecated"):
            entry["deprecated"] = True
        if obj.get("revoked"):
            entry["revoked"] = True
            if ext in revoked_by:
                entry["revoked_by"] = revoked_by[ext]
        # Two objects can share an id (an old one revoked, a live one). The live one wins.
        current = techniques.get(ext)
        if current is None or (current.get("revoked") and not entry.get("revoked")):
            techniques[ext] = entry

    return {
        "attribution": ATTRIBUTION,
        "source": source,
        "copyright": copyright_,
        "attack_modified": modified,
        "generated_by": "scripts/update_attack_catalogue.py",
        "tactics": dict(sorted(tactics.items())),
        "techniques": dict(sorted(techniques.items())),
    }


def render(catalogue: dict) -> str:
    """JSON with one tactic / technique per line, so an ATT&CK release is a readable diff."""
    lines = ["{"]
    head = [k for k in catalogue if k not in ("tactics", "techniques")]
    for key in head:
        lines.append(f"  {json.dumps(key)}: {json.dumps(catalogue[key], ensure_ascii=False)},")
    for section, last in (("tactics", False), ("techniques", True)):
        items = list(catalogue[section].items())
        lines.append(f"  {json.dumps(section)}: {{")
        for i, (k, v) in enumerate(items):
            comma = "," if i < len(items) - 1 else ""
            lines.append(f"    {json.dumps(k)}: {json.dumps(v, ensure_ascii=False, sort_keys=True)}{comma}")
        lines.append("  }" + ("" if last else ","))
    lines.append("}")
    return "\n".join(lines) + "\n"


def load_bundle(source: str) -> dict:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=300) as resp:  # noqa: S310 - fixed public URL or operator's choice
            return json.load(resp)
    return json.loads(Path(source).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=SOURCE_URL, help="STIX bundle URL or local path (default: MITRE CTI master)")
    ap.add_argument("--output", type=Path, default=OUT)
    ap.add_argument("--check", action="store_true", help="exit 1 if the output file would change")
    args = ap.parse_args(argv)

    source_label = args.source if args.source.startswith(("http://", "https://")) else SOURCE_URL
    text = render(build(load_bundle(args.source), source_label))
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != text:
            print(f"{args.output} is out of date with {args.source}", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    data = json.loads(text)
    print(
        f"wrote {args.output}: ATT&CK as of {data['attack_modified']}, {len(data['techniques'])} techniques, "
        f"{len(data['tactics'])} tactics"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
