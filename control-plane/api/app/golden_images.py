"""Golden-image (template library) import + resolution.

Seeds the GoldenImage registry from vm_iso_catalogue.csv and resolves a topology
OS alias (e.g. 'windows-server-2019') to the hypervisor template name a range
should clone. Deterministic; no LLM.
"""

from __future__ import annotations

import csv
import io
import json

from sqlalchemy.orm import Session

from .models import GoldenImage

_TRUE = {"yes", "true", "y", "1"}

# OS identifiers that were renamed. Stored ranges, saved designer diagrams and older
# golden-image registries still carry the old names, so they must keep resolving.
# Mirrored in control-plane/worker/worker/render.py and
# scenario-engine/template_engine/renderer.py (a test keeps the three in step).
DEPRECATED_OS_ALIASES: dict[str, str] = {
    "ubuntu-2204": "ubuntu-2404",
    "ubuntu2204": "ubuntu-2404",
    "ubuntu-22.04": "ubuntu-24.04",
}

# Spellings of one golden image. A registry imported before a rename only knows the
# old spelling, and one imported after only the new, so resolution tries them all.
EQUIVALENT_OS_ALIASES: tuple[tuple[str, ...], ...] = (
    ("ubuntu-2404", "ubuntu-24.04", "ubuntu-2204", "ubuntu-22.04", "ubuntu2204"),
)


def canonical_os(os_alias: str) -> str:
    """Map a deprecated OS identifier to its current name; anything else is unchanged."""
    alias = (os_alias or "").strip()
    return DEPRECATED_OS_ALIASES.get(alias, alias)


def os_alias_candidates(os_alias: str) -> list[str]:
    """Identifiers to try, in order: the current name, as given, then its equivalents."""
    alias = (os_alias or "").strip()
    out = [canonical_os(alias), alias]
    for group in EQUIVALENT_OS_ALIASES:
        if alias in group:
            out.extend(group)
    return list(dict.fromkeys(a for a in out if a))


def _aliases(row: dict, catalogue_id: str) -> list[str]:
    raw = (row.get("os_aliases_in_ranges") or "").strip()
    out: set[str] = {catalogue_id}
    if raw and raw not in ("-", "n/a"):
        for part in raw.replace(",", ";").split(";"):
            p = part.strip()
            if p and p not in ("-", "n/a"):
                out.add(p)
    return sorted(out)


def parse_catalogue(csv_text: str, hypervisor: str = "vsphere") -> list[dict]:
    """Pure parse of vm_iso_catalogue.csv into normalized golden-image dicts for one hypervisor."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[dict] = []
    for raw in reader:
        cid = (raw.get("template_id") or "").strip()
        if not cid:
            continue
        packer = (raw.get("packer_status") or "").strip()
        rows.append(
            {
                "catalogue_id": cid,
                "os_family": (raw.get("os_family") or "").strip(),
                "version": (raw.get("version") or "").strip(),
                "role": (raw.get("role") or "").strip(),
                "hypervisor": hypervisor,
                "template_name": cid,  # default; operator or register-back sets the real store name
                "datastore": "",  # operator sets the NFS datastore
                "os_aliases": _aliases(raw, cid),
                "sensor_baked": (raw.get("sensor_baked") or "").strip().lower() in _TRUE,
                "enabled": (raw.get("enabled") or "").strip().lower() in _TRUE,
                # Honest default: nothing is 'built' until register-back confirms a real published
                # template in a reachable hypervisor. packer_status is kept in notes as a hint only.
                "build_status": "planned",
                "golden_gb": int(raw["golden_gb"]) if (raw.get("golden_gb") or "").strip().isdigit() else 0,
                "notes": "; ".join(x for x in [(raw.get("notes") or "").strip(), f"packer_status={packer}"] if x),
            }
        )
    return rows


def import_catalogue(db: Session, csv_text: str, hypervisor: str = "vsphere", tenant_id: str | None = None) -> dict:
    """Upsert GoldenImage rows from the catalogue for one hypervisor. Idempotent."""
    rows = parse_catalogue(csv_text, hypervisor)
    stats = {"created": 0, "updated": 0, "hypervisor": hypervisor, "total": len(rows)}
    for r in rows:
        img = db.query(GoldenImage).filter_by(catalogue_id=r["catalogue_id"], hypervisor=hypervisor).one_or_none()
        if img is None:
            img = GoldenImage(
                catalogue_id=r["catalogue_id"],
                hypervisor=hypervisor,
                template_name=r["template_name"],
                build_status=r["build_status"],
                tenant_id=tenant_id,
            )
            db.add(img)
            stats["created"] += 1
        else:
            stats["updated"] += 1
            # never clobber a real built status/template_name set by register-back
            if img.build_status in (None, "", "planned"):
                img.build_status = r["build_status"]
            if not img.template_name:
                img.template_name = r["template_name"]
        img.os_family = r["os_family"]
        img.version = r["version"]
        img.role = r["role"]
        img.os_aliases = json.dumps(r["os_aliases"])
        img.sensor_baked = r["sensor_baked"]
        img.enabled = r["enabled"]
        img.golden_gb = r["golden_gb"]
        img.notes = r["notes"]
    db.commit()
    return stats


def resolve_template(db: Session, os_alias: str, hypervisor: str = "vsphere") -> str | None:
    """Resolve a topology os alias -> hypervisor template name (enabled images only)."""
    if not os_alias or not os_alias.strip():
        return None
    images = db.query(GoldenImage).filter_by(hypervisor=hypervisor, enabled=True, deleted_at=None).all()
    for alias in os_alias_candidates(os_alias):
        for img in images:
            if img.catalogue_id == alias:
                return img.template_name or img.catalogue_id
        for img in images:
            try:
                aliases = json.loads(img.os_aliases or "[]")
            except json.JSONDecodeError:
                aliases = []
            if alias in aliases:
                return img.template_name or img.catalogue_id
    return None


def resolve_map(db: Session, hypervisor: str = "vsphere") -> dict[str, str]:
    """Full os-alias -> template_name map (for the renderer / UI)."""
    out: dict[str, str] = {}
    for img in db.query(GoldenImage).filter_by(hypervisor=hypervisor, enabled=True, deleted_at=None).all():
        tn = img.template_name or img.catalogue_id
        out[img.catalogue_id] = tn
        try:
            for a in json.loads(img.os_aliases or "[]"):
                out.setdefault(a, tn)
        except json.JSONDecodeError:
            pass
    # Add the current spellings of anything resolvable, so the designer offers the new
    # name even against a registry imported before a rename. Deprecated names are not
    # added: they still resolve, but should not be offered for new designs.
    for group in EQUIVALENT_OS_ALIASES:
        hit = next((out[a] for a in group if a in out), None)
        if hit is not None:
            for a in group:
                if a not in DEPRECATED_OS_ALIASES:
                    out.setdefault(a, hit)
    return out
