#!/usr/bin/env python3
"""TrueNorth's vCenter objects: one ``TrueNorth`` folder tree and tn-* tags (ADR 0012 follow-up).

Run through scripts/lab/vcenter-organize.sh (credentials, preflight, the TrueNorth range
list) or, for one VM, from the template kit (build.sh replicate, test-clone.sh):

    vcenter_organize.py plan  --dc DC-Lab [--ranges ranges.json]   # print the plan only
    vcenter_organize.py apply --dc DC-Lab [--ranges ranges.json]   # make it so
    vcenter_organize.py tag VM --type template-copy --template ubuntu-lts [--dry-run]

Talks to vCenter only through ``govc`` (its GOVC_* environment); stdlib only.

Target layout (the provisioner's VSPHERE_RANGE_FOLDER is ``TrueNorth/Ranges``):

    /<dc>/vm/TrueNorth/Templates/<id>, <id>@<host>-<yyyymmdd>   golden templates, host copies
    /<dc>/vm/TrueNorth/Templates/_retired/                       old TrueNorth templates (tn-type=retired)
    /<dc>/vm/TrueNorth/Ranges/<range-name>-<key>/                one folder per range

``<key>`` is the first 12 hex digits of the range id; earlier builds named folders ``<id8>``.

What ``apply`` does, each step idempotent:

1. Reads the TrueNorth ranges itself (``--ranges-cmd``, run fresh, then once more just before
   the first change) and refuses unless every range is in a settled state (ready, running,
   stopped, failed, destroyed): anything else may be creating or removing VMs.
2. Folders: creates the three above where missing.
3. Only TrueNorth's VMs under the earlier ``truenorth/`` move: catalogue templates and their
   host copies (to ``TrueNorth/Templates``), templates TrueNorth registered or the operator
   names (``--retire``; to ``_retired``, never while powered on, never deleted), range VMs
   (a TrueNorth range annotation, or ``<id8>-*`` in that range's folder) and test clones.
   Anything else is reported and left. A name already in the target stays put, reported.
4. A range folder of only TrueNorth VMs moves whole to ``TrueNorth/Ranges`` and is renamed
   ``<range-name>-<key>`` (when exactly one range has that id8); otherwise its VMs move one
   by one. An ``<id8>`` folder already in ``TrueNorth/Ranges`` is renamed the same way.
5. An old ``truenorth`` folder is removed only if this run emptied it, nothing in this run
   failed, and the property collector reports no children the moment before (a vCenter
   folder delete deletes what is in it).
6. Tags (single-cardinality categories created where missing) on golden templates, copies,
   retired templates, test clones, range VMs, TN-DEPOT01 and TN-MGMT01. Only tn-*
   categories are touched; a range VM's tn-os / tn-template set at build is kept.

Never touched: any VM not recognised as TrueNorth's (WIN11-TMPL, vCenter, vCLS agents,
other people's templates).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CATALOGUE = REPO / "content/catalogue/vm_iso_catalogue.csv"
VARIANTS = REPO / "infra/vsphere/packer/variants"

# Same definitions as control-plane/worker/worker/provisioners/vsphere_tags.py (tests/contracts).
CATEGORIES: dict[str, str] = {
    "tn-type": "TrueNorth object type: golden-template, template-copy, range-vm, test-clone, depot, mgmt, retired",
    "tn-os": "TrueNorth OS alias of the VM (windows-server-2022, ubuntu-2404, pfsense, ...)",
    "tn-template": "TrueNorth catalogue id of the golden template the VM came from",
    "tn-range": "TrueNorth range the VM belongs to: <range-name>-<first 12 hex digits of its id>",
}
TYPES = ("golden-template", "template-copy", "range-vm", "test-clone", "depot", "mgmt", "retired")
ROOT, TEMPLATES, RANGES = "TrueNorth", "TrueNorth/Templates", "TrueNorth/Ranges"
RETIRED = "TrueNorth/Templates/_retired"
LEGACY = "truenorth"
LEGACY_MAP = {"templates": "Templates", "ranges": "Ranges"}
# Settled range states: no VM of the range is being created or removed. Anything else
# (created, provisioning, destroying, starting, stopping, a state added later) is busy.
SETTLED_STATES = ("ready", "running", "stopped", "failed", "destroyed")
RANGE_TAG = re.compile(r"truenorth-range:\s*([0-9A-Za-z][0-9A-Za-z-]*)")  # worker vsphere_api.RANGE_TAG
KEY_LEN = 12
TEST_CLONE = re.compile(r"^tn-testclone-(?P<id>.+)-(?P<host>[A-Za-z0-9_]+)$")
_COPY = re.compile(r"^(?P<base>[^@]+)@(?P<host>[A-Za-z0-9_-]+)-(?P<date>\d{8})$")
_NAME_LIMIT = 40


# -- names (same rules as vsphere_tags / template_copies in the worker) ----------------------

def slug(text: str | None, limit: int = _NAME_LIMIT) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return out[:limit].rstrip("-")


def range_key(range_id: str) -> str:
    return re.sub(r"[^0-9a-f]", "", str(range_id).lower())[:KEY_LEN]


def range_label(name: str | None, range_id: str) -> str:
    key = range_key(range_id)
    base = slug(name or "")
    if base == key or base.endswith(f"-{key}"):
        base = base[: -len(key)].rstrip("-")
    return f"{base}-{key}" if base else key


def folder_key(name: str) -> str | None:
    """The range key a folder is named for: a 12-hex ``-<key>`` suffix, or exactly ``<id8>``."""
    low = str(name or "").lower()
    if m := re.search(rf"(?:^|-)([0-9a-f]{{{KEY_LEN}}})$", low):
        return m.group(1)
    return low if re.fullmatch(r"[0-9a-f]{8}", low) else None


def belongs_to(name: str, range_id: str) -> bool:
    key = folder_key(name)
    return bool(key) and range_key(range_id).startswith(key)


def is_legacy_folder(name: str) -> bool:
    return len(folder_key(name) or "") == 8


def annotated_range(annotation: str | None) -> str:
    m = RANGE_TAG.search(annotation or "")
    return m.group(1) if m else ""


def copy_base(name: str) -> str | None:
    m = _COPY.match(str(name or ""))
    return m["base"] if m else None


def template_id(source: str | None) -> str:
    name = str(source or "")
    if name.startswith("library:"):
        name = name[len("library:"):]
    return copy_base(name) or name


# -- the catalogue: id -> OS alias ----------------------------------------------------------

def load_os_map(catalogue: Path = CATALOGUE, variants: Path = VARIANTS) -> dict[str, str]:
    """Catalogue id -> the OS alias ranges name it by (first of os_aliases_in_ranges, else the
    id itself); variants from their ``variant_os_aliases`` line."""
    out: dict[str, str] = {}
    if catalogue.exists():
        with catalogue.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                cid = (row.get("template_id") or "").strip()
                raw = (row.get("os_aliases_in_ranges") or "").strip()
                aliases = [a.strip() for a in raw.replace(",", ";").split(";") if a.strip() not in ("", "-", "n/a")]
                if cid:
                    out[cid] = aliases[0] if aliases else cid
    for f in sorted(variants.glob("*.pkrvars.hcl")) if variants.exists() else []:
        text = f.read_text(encoding="utf-8")
        name = re.search(r'^\s*variant_name\s*=\s*"([^"]+)"', text, re.M)
        aliases = re.search(r'^\s*variant_os_aliases\s*=\s*\[\s*"([^"]+)"', text, re.M)
        cid = name.group(1) if name else f.name.removesuffix(".pkrvars.hcl")
        out.setdefault(cid, aliases.group(1) if aliases else cid)
    return out


# -- inventory ------------------------------------------------------------------------------

@dataclass
class Vm:
    moid: str
    path: str  # /<dc>/vm/a/b/name
    template: bool = False
    tags: dict[str, str] = field(default_factory=dict)  # tn-* category -> tag name
    annotation: str = ""
    powered_on: bool = False

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[1]

    @property
    def ref(self) -> str:
        return f"VirtualMachine:{self.moid}"


@dataclass
class Inventory:
    dc: str
    vms: list[Vm]
    folders: set[str]  # relative to /<dc>/vm, e.g. "TrueNorth/Templates"

    def rel(self, path: str) -> str:
        """``/<dc>/vm/a/b`` -> ``a/b``."""
        prefix = f"/{self.dc}/vm/"
        return path[len(prefix):] if path.startswith(prefix) else path

    def abs(self, rel: str) -> str:
        return f"/{self.dc}/vm/{rel}".rstrip("/")


Run = Callable[[list[str]], str]


def govc(args: list[str]) -> str:
    return subprocess.run(["govc", *args], check=True, capture_output=True, text=True).stdout


def _rows(text: str) -> list[tuple[str, str]]:
    """``govc find -l -I`` lines: ``<moid>  <path>`` (a path may hold spaces)."""
    out = []
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            out.append((parts[0], parts[1]))
    return out


def read_inventory(dc: str, run: Run = govc) -> Inventory:
    root = f"/{dc}/vm"
    vms = {moid: Vm(moid, path) for moid, path in _rows(run(["find", "-l", "-I", root, "-type", "m"]))}
    for moid, props in _collect(run(["object.collect", "-json", "-type", "m", root, "config.template",
                                     "config.annotation", "runtime.powerState"])).items():
        if moid in vms:
            vms[moid].template = bool(props.get("config.template"))
            vms[moid].annotation = str(props.get("config.annotation") or "")
            vms[moid].powered_on = props.get("runtime.powerState") == "poweredOn"
    inv = Inventory(dc, sorted(vms.values(), key=lambda v: v.path), set())
    inv.folders = {inv.rel(p.strip()) for p in run(["find", root, "-type", "f"]).splitlines()
                   if p.strip() and p.strip() != root}
    for cat, tag, objects in read_tags(run):
        for obj in objects:
            vm = vms.get(obj.split(":", 1)[-1])
            if vm is not None:
                vm.tags[cat] = tag
    return inv


def _collect(text: str) -> dict[str, dict]:
    """``govc object.collect -json -type m``: a stream of JSON objects, one per VM, each
    ``{"obj": {"value": moid}, "changeSet": [{"name", "val"}]}`` -> moid -> {property: value}."""
    out: dict[str, dict] = {}
    dec, i, text = json.JSONDecoder(), 0, text or ""
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        doc, i = dec.raw_decode(text, i)
        for item in doc if isinstance(doc, list) else [doc]:
            moid = (item.get("obj") or {}).get("value")
            if moid:
                out.setdefault(moid, {}).update({c.get("name"): c.get("val") for c in item.get("changeSet") or []})
    return out


def has_children(run: Run, path: str) -> bool:
    """The folder's ``childEntity`` from the property collector, now. Anything unreadable
    counts as children: a folder is only ever deleted when it is known to be empty."""
    try:
        reply = json.loads(run(["object.collect", "-json", path, "childEntity"]) or "null")
        (prop,) = [p for p in reply if p.get("name") == "childEntity"]
        val = prop.get("val")
        if isinstance(val, dict):
            val = val.get("_value", val.get("ManagedObjectReference"))
        return bool(val)
    except (ValueError, TypeError, AttributeError, subprocess.CalledProcessError):
        return True


def read_tags(run: Run = govc) -> list[tuple[str, str, list[str]]]:
    """(category, tag name, attached object refs) for every tag in a tn-* category."""
    out = []
    cats = json.loads(run(["tags.category.ls", "-json"]) or "[]") or []
    for cat in cats:
        if cat.get("name") not in CATEGORIES:
            continue
        for tag in json.loads(run(["tags.ls", "-c", cat["name"], "-json"]) or "[]") or []:
            objects = json.loads(run(["tags.attached.ls", "-json", tag["id"]]) or "[]") or []
            out.append((cat["name"], tag["name"], [str(o) for o in objects]))
    return out


# -- the plan -------------------------------------------------------------------------------

@dataclass
class Action:
    kind: str  # mkdir | move | rename | rmdir | tag
    args: tuple
    why: str = ""

    def __str__(self) -> str:
        what = {
            "mkdir": lambda a: f"create folder {a[0]}",
            "move": lambda a: f"move {a[0]} -> {a[1]}/",
            "rename": lambda a: f"rename {a[0]} -> {a[1]}",
            "rmdir": lambda a: f"remove empty folder {a[0]}",
            "tag": lambda a: f"tag {a[1]}: " + ", ".join(f"{k}={v}" for k, v in sorted(a[2].items())),
        }[self.kind](self.args)
        return f"{what}{f'  ({self.why})' if self.why else ''}"


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    refuse: list[str] = field(default_factory=list)

    def add(self, kind: str, *args, why: str = "") -> None:
        self.actions.append(Action(kind, args, why))


def load_ranges(data) -> tuple[list[dict], list[str]]:
    """tn_ranges_inprocess.py output ``{"ranges": [...], "templates": [...]}`` (or a bare list
    of ranges) -> (ranges, template names TrueNorth registered)."""
    if isinstance(data, list):
        return data, []
    data = data or {}
    return list(data.get("ranges") or []), [str(t) for t in data.get("templates") or [] if t]


def busy(ranges: Iterable[dict]) -> list[str]:
    """Every range not in a settled state, as a refusal line."""
    return [f"range {r.get('name')!r} ({str(r.get('id'))[:8]}) is {r.get('state')}"
            for r in ranges if str(r.get("state")) not in SETTLED_STATES]


def _range_templates(ranges: Iterable[dict]) -> dict[str, str]:
    """Range VM name -> the template it was cloned from (provisioner_output vms[].template)."""
    out = {}
    for r in ranges:
        for vm in r.get("vms") or []:
            if vm.get("name") and vm.get("template"):
                out[str(vm["name"])] = str(vm["template"])
    return out


def _parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def make_plan(inv: Inventory, ranges: list[dict] | None, os_map: dict[str, str], *,
              known_templates: Iterable[str] = (), retire: Iterable[str] = (),
              depot: str = "TN-DEPOT01", mgmt: str = "TN-MGMT01") -> Plan:
    """Everything ``apply`` would do, in order. ``ranges`` None: the TrueNorth database was not
    read (range folders keep their names; range VMs are recognised by annotation or folder)."""
    plan = Plan()
    live = [r for r in ranges or [] if r.get("state") != "destroyed"]
    by_id = {str(r["id"]).lower(): r for r in live}
    known_templates, retire = set(known_templates), set(retire)
    if ranges is None:
        plan.notes.append("TrueNorth ranges not read: range folders keep their current names")
    plan.refuse.extend(busy(ranges or []))

    def ranges_for(key: str) -> list[dict]:
        return [r for r in live if range_key(r["id"]).startswith(key)]

    def label_of(r: dict) -> str:
        return range_label(r.get("name"), str(r["id"]))

    def is_range_folder(rel: str) -> bool:
        return _parent(rel) in (RANGES, f"{LEGACY}/ranges") and rel.count("/") == 2

    def vm_range(vm: Vm, folder: str) -> str | None:
        """The tn-range label of a TrueNorth range VM, or None for any other VM: its range
        annotation, else ``<id8>-*`` in that range's folder or for exactly one known range."""
        if rid := annotated_range(vm.annotation):
            row = by_id.get(rid.lower())
            return label_of(row) if row else range_label(None, rid)
        id8 = vm.name.split("-", 1)[0].lower()
        if "-" not in vm.name or not re.fullmatch(r"[0-9a-f]{8}", id8):
            return None
        name = folder.rsplit("/", 1)[-1]
        if is_range_folder(folder) and (folder_key(name) or "")[:8] == id8:
            cands = ranges_for(folder_key(name))
            return label_of(cands[0]) if len(cands) == 1 else name
        cands = ranges_for(id8)
        return label_of(cands[0]) if len(cands) == 1 else None

    folders = set(inv.folders)
    final: dict[str, str] = {vm.moid: inv.rel(vm.path) for vm in inv.vms}  # moid -> relative path after moves
    moved_out: set[str] = set()  # legacy folders this plan moves a VM out of
    gone: set[str] = set()  # legacy folders this plan moves away whole

    def mkdir(rel: str) -> None:
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            p = "/".join(parts[:i])
            if p not in folders:
                plan.add("mkdir", inv.abs(p))
                folders.add(p)

    def names_in(rel_folder: str) -> set[str]:
        return {p.rsplit("/", 1)[1] for p in final.values() if _parent(p) == rel_folder}

    def move_vm(vm: Vm, to: str, why: str) -> bool:
        if vm.name in names_in(to):
            plan.notes.append(f"{vm.path}: a VM named {vm.name!r} is already in {inv.abs(to)}; left where it is")
            return False
        mkdir(to)
        moved_out.add(_parent(final[vm.moid]))
        plan.add("move", inv.abs(final[vm.moid]), inv.abs(to), why=why)
        final[vm.moid] = f"{to}/{vm.name}"
        return True

    def leave(vm: Vm, why: str) -> None:
        plan.notes.append(f"{vm.path}: {why}; left where it is")

    for rel in (TEMPLATES, RANGES):
        mkdir(rel)

    legacy_vms = [vm for vm in inv.vms if inv.rel(vm.path).startswith(f"{LEGACY}/")]

    # Templates anywhere under truenorth/ (range folders hold range VMs: below).
    for vm in legacy_vms:
        if not vm.template or is_range_folder(_parent(final[vm.moid])):
            continue
        if vm.name in os_map or (copy_base(vm.name) or "") in os_map:
            move_vm(vm, TEMPLATES, "catalogue template")
        elif vm.name in retire or vm.name in known_templates:
            if vm.powered_on:
                leave(vm, "powered on: never retired")
            else:
                move_vm(vm, RETIRED, "TrueNorth's old template: retired, not deleted")
        else:
            leave(vm, "a template TrueNorth does not know (not in the catalogue or the registry; "
                      "--retire NAME retires it)")

    # Range folders under truenorth/ranges: whole when only TrueNorth range VMs are in it.
    for rel in sorted(f for f in inv.folders if f.startswith(f"{LEGACY}/ranges/") and f.count("/") == 2):
        name = rel.rsplit("/", 1)[1]
        key = folder_key(name)
        inside = [vm for vm in legacy_vms if final[vm.moid].startswith(f"{rel}/")]
        if not inside:
            plan.notes.append(f"{inv.abs(rel)} has no VMs; left (not emptied by this run)")
            continue
        cands = ranges_for(key) if key else []
        if len(cands) > 1:
            plan.notes.append(f"{inv.abs(rel)}: {len(cands)} TrueNorth ranges share id {key}; folder name kept")
        label = label_of(cands[0]) if len(cands) == 1 else name
        own = {vm.moid: vm_range(vm, rel) for vm in inside}
        whole = all(own.values()) and not any(f.startswith(f"{rel}/") for f in inv.folders)
        existing = next((f for f in sorted(folders) if _parent(f) == RANGES and f.count("/") == 2
                         and (f == f"{RANGES}/{label}" or (len(cands) == 1 and not is_legacy_folder(f.rsplit("/", 1)[1])
                                                           and belongs_to(f.rsplit("/", 1)[1], cands[0]["id"])))), None)
        if whole and not existing and f"{RANGES}/{name}" not in folders:
            plan.add("move", inv.abs(rel), inv.abs(RANGES), why=f"range {key or name}")
            gone.add(rel)
            target = f"{RANGES}/{name}"
            if label != name:
                plan.add("rename", inv.abs(target), label, why="named for the range")
                target = f"{RANGES}/{label}"
            folders.add(target)
            for vm in inside:
                final[vm.moid] = target + final[vm.moid][len(rel):]
            continue
        for vm in inside:
            if own[vm.moid]:
                move_vm(vm, existing or f"{RANGES}/{own[vm.moid]}", f"range {key or name}")
            else:
                leave(vm, "not a TrueNorth range VM")

    # An earlier build's <id8> folder already in TrueNorth/Ranges: renamed when unambiguous.
    for rel in sorted(f for f in folders if _parent(f) == RANGES and f.count("/") == 2
                      and is_legacy_folder(f.rsplit("/", 1)[1])):
        cands = ranges_for(rel.rsplit("/", 1)[1])
        if len(cands) != 1 or (new := f"{RANGES}/{label_of(cands[0])}") in folders:
            continue
        plan.add("rename", inv.abs(rel), new.rsplit("/", 1)[1], why="named for the range")
        folders.discard(rel)
        folders.add(new)
        for moid, path in final.items():
            if path.startswith(f"{rel}/"):
                final[moid] = new + path[len(rel):]

    # Anything else still under truenorth/: TrueNorth's VMs move, the rest is reported.
    for vm in legacy_vms:
        rel = final[vm.moid]
        if not rel.startswith(f"{LEGACY}/") or vm.template or is_range_folder(_parent(rel)):
            continue  # moved, a template (above) or in a range folder (above)
        if label := vm_range(vm, _parent(rel)):
            move_vm(vm, f"{RANGES}/{label}", "range VM")
        elif TEST_CLONE.match(vm.name) or vm.name in (depot, mgmt):
            parts = rel.split("/")[1:-1]
            if parts:
                parts[0] = LEGACY_MAP.get(parts[0], parts[0])
            move_vm(vm, "/".join([ROOT, *parts]), "out of the earlier truenorth folder")
        else:
            leave(vm, "not recognised as TrueNorth's")

    # Old truenorth folders this run empties, deepest first (checked again before each delete).
    left = {p for p in final.values() if p.startswith(f"{LEGACY}/")}
    removed: set[str] = set()

    def away(f: str) -> bool:
        return f in removed or f in gone or any(f.startswith(f"{g}/") for g in gone)

    for rel in sorted((f for f in inv.folders if f == LEGACY or f.startswith(f"{LEGACY}/")),
                      key=lambda f: (-f.count("/"), f)):  # deepest first; stable across runs
        if away(rel):
            continue
        if any(p.startswith(f"{rel}/") for p in left) or any(
                f.startswith(f"{rel}/") and not away(f) for f in inv.folders):
            plan.notes.append(f"{inv.abs(rel)} is not empty after the moves; kept")
            continue
        if rel not in moved_out and not any(_parent(f) == rel and away(f) for f in inv.folders):
            plan.notes.append(f"{inv.abs(rel)} was empty before this run; left (not this run's to remove)")
            continue
        plan.add("rmdir", inv.abs(rel))
        removed.add(rel)

    # Tags.
    templates = _range_templates(ranges or [])
    for vm in inv.vms:
        rel = final[vm.moid]
        folder = _parent(rel)
        want: dict[str, str] = {}
        keep_existing: set[str] = set()
        if vm.template and folder == RETIRED:
            want = {"tn-type": "retired"}
        elif vm.template and folder == TEMPLATES:
            if base := copy_base(vm.name):
                want = {"tn-type": "template-copy", "tn-template": base}
            else:
                want = {"tn-type": "golden-template", "tn-template": vm.name}
        elif vm.template and (base := copy_base(vm.name)) and base in os_map and not rel.startswith(f"{LEGACY}/"):
            want = {"tn-type": "template-copy", "tn-template": base}
        elif m := TEST_CLONE.match(vm.name):
            want = {"tn-type": "test-clone", "tn-template": m["id"]}
        elif vm.name == depot:
            want = {"tn-type": "depot"}
        elif vm.name == mgmt:
            want = {"tn-type": "mgmt"}
        elif not vm.template and (label := vm_range(vm, folder)):
            want = {"tn-type": "range-vm", "tn-range": label}
            if tmpl := template_id(templates.get(vm.name)):
                want["tn-template"] = tmpl
            keep_existing = {"tn-os", "tn-template"}  # set by the build: more precise
        if not want:
            continue
        if want.get("tn-template") and (os_alias := os_map.get(want["tn-template"])):
            want["tn-os"] = slug(os_alias, 80)
        change = {k: v for k, v in want.items()
                  if vm.tags.get(k) != v and not (k in keep_existing and vm.tags.get(k))}
        if change:
            plan.add("tag", vm.ref, inv.abs(rel), change)
    return plan


# -- doing it -------------------------------------------------------------------------------

class Tagger:
    """Categories and tags created on first use; a value of a tn category replaced, not added."""

    def __init__(self, run: Run, act: Callable[[list[str]], None]) -> None:
        self.run, self.act = run, act
        self.cat_names: dict[str, str] | None = None  # category id -> name
        self.cats: set[str] = set()
        self.tags: dict[str, set[str]] = {}

    def _categories(self) -> dict[str, str]:
        if self.cat_names is None:
            listed = json.loads(self.run(["tags.category.ls", "-json"]) or "[]") or []
            self.cat_names = {c.get("id"): c.get("name") for c in listed}
            self.cats = set(self.cat_names.values())
        return self.cat_names

    def _ensure(self, cat: str, name: str) -> None:
        self._categories()
        if cat not in self.cats:
            self.act(["tags.category.create", "-t", "VirtualMachine", "-d", CATEGORIES[cat], cat])
            self.cats.add(cat)
        if cat not in self.tags:
            listed = json.loads(self.run(["tags.ls", "-c", cat, "-json"]) or "[]") if cat in self.cats else []
            self.tags[cat] = {t.get("name") for t in listed or []}
        if name not in self.tags[cat]:
            self.act(["tags.create", "-c", cat, name])
            self.tags[cat].add(name)

    def apply(self, ref: str, want: dict[str, str]) -> None:
        current: dict[str, str] = {}
        cats = self._categories()
        for row in json.loads(self.run(["tags.attached.ls", "-json", "-l", "-r", ref]) or "[]") or []:
            for tag in row.get("tags") or []:
                cat = cats.get(tag.get("category_id"), tag.get("category_id"))
                if cat in CATEGORIES:
                    current[cat] = tag["name"]
        for cat, name in sorted(want.items()):
            if current.get(cat) == name:
                continue
            self._ensure(cat, name)
            if cat in current:  # single cardinality: the old value goes first
                self.act(["tags.detach", "-c", cat, current[cat], ref])
            self.act(["tags.attach", "-c", cat, name, ref])


def execute(plan: Plan, run: Run = govc, *, log=print) -> int:
    """Carry the plan out; one failed step is reported and the rest still run. Returns failures."""
    failed = 0

    def act(argv: list[str]) -> None:
        log(f"  + govc {' '.join(argv)}")
        run(argv)

    tagger = Tagger(run, act)
    for a in plan.actions:
        try:
            if a.kind == "mkdir":
                act(["folder.create", a.args[0]])
            elif a.kind == "move":
                act(["object.mv", a.args[0], a.args[1]])
            elif a.kind == "rename":
                act(["object.rename", a.args[0], a.args[1]])
            elif a.kind == "rmdir":
                # vCenter deletes a folder WITH what is in it: only ever one this run emptied
                # without a hitch, and only if the property collector says it has no children now.
                if failed:
                    log(f"  ! {a.args[0]} kept: an earlier step of this run failed")
                    continue
                if has_children(run, a.args[0]):
                    log(f"  ! {a.args[0]} is not empty now (or could not be read); kept")
                    continue
                act(["object.destroy", a.args[0]])
            elif a.kind == "tag":
                tagger.apply(a.args[0], a.args[2])
        except subprocess.CalledProcessError as exc:
            failed += 1
            log(f"  ! {a}: {(exc.stderr or '').strip() or exc}")
    return failed


def tag_one(vm: str, tn_type: str, template: str | None, range_label_: str | None, *, os_map: dict[str, str],
            run: Run = govc, dry_run: bool = False, log=print) -> None:
    """The kit's one-VM tagging: ``vm`` is an inventory path or a ``VirtualMachine:<moid>``."""
    if tn_type not in TYPES:
        raise SystemExit(f"--type must be one of {', '.join(TYPES)}")
    want = {"tn-type": tn_type}
    if template:
        want["tn-template"] = template
        if alias := os_map.get(template):
            want["tn-os"] = slug(alias, 80)
    if range_label_:
        want["tn-range"] = range_label_
    if dry_run:
        log(f"  [dry-run] tag {vm}: " + ", ".join(f"{k}={v}" for k, v in sorted(want.items())))
        return

    def act(argv: list[str]) -> None:
        run(argv)

    Tagger(run, act).apply(vm, want)
    log(f"  tagged {vm.rsplit('/', 1)[-1]}: " + ", ".join(f"{k}={v}" for k, v in sorted(want.items())))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--dc", default=os.environ.get("GOVC_DATACENTER") or "DC-Lab")
        p.add_argument("--ranges", help="tn_ranges_inprocess.py output (a file); omit if unavailable")
        p.add_argument("--ranges-cmd", help="a shell command printing it; apply runs it fresh, twice")
        p.add_argument("--retire", action="append", default=[], help="also retire this template (repeatable)")
        p.add_argument("--depot", default="TN-DEPOT01")
        p.add_argument("--mgmt", default="TN-MGMT01")
    t = sub.add_parser("tag")
    t.add_argument("vm")
    t.add_argument("--type", required=True, dest="tn_type")
    t.add_argument("--template")
    t.add_argument("--range", dest="range_label")
    t.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    os_map = load_os_map()

    if args.cmd == "tag":
        tag_one(args.vm, args.tn_type, args.template, args.range_label, os_map=os_map, dry_run=args.dry_run)
        return 0

    def read_ranges() -> tuple[list[dict] | None, list[str]]:
        if args.ranges_cmd:
            out = subprocess.run(["bash", "-c", args.ranges_cmd], check=True, capture_output=True, text=True).stdout
            return load_ranges(json.loads(out))
        if args.ranges:
            return load_ranges(json.loads(Path(args.ranges).read_text()))
        return None, []

    try:
        ranges, registered = read_ranges()
    except (subprocess.CalledProcessError, ValueError, OSError) as exc:
        print(f"  REFUSED: the TrueNorth ranges could not be read ({exc.__class__.__name__})", file=sys.stderr)
        return 3
    plan = make_plan(read_inventory(args.dc), ranges, os_map, known_templates=registered, retire=args.retire,
                     depot=args.depot, mgmt=args.mgmt)
    for note in plan.notes:
        print(f"  note: {note}")
    if not plan.actions:
        print("  nothing to do: TrueNorth's folders and tags are already in order")
    for a in plan.actions:
        print(f"  {'plan' if args.cmd == 'plan' else 'do  '}  {a}")
    if plan.refuse:
        for why in plan.refuse:
            print(f"  REFUSED: {why}", file=sys.stderr)
        if args.cmd == "apply":
            print("  nothing changed: run again once every TrueNorth range is settled", file=sys.stderr)
            return 3
    if args.cmd == "plan":
        return 0
    if ranges is not None:  # once more, the moment before the first change
        try:
            again, _ = read_ranges()
        except (subprocess.CalledProcessError, ValueError, OSError) as exc:
            print(f"  REFUSED: the TrueNorth ranges could not be read again ({exc.__class__.__name__})", file=sys.stderr)
            return 3
        if late := busy(again or []):
            for why in late:
                print(f"  REFUSED: {why}", file=sys.stderr)
            print("  nothing changed: a range started changing while the plan was made", file=sys.stderr)
            return 3
    failed = execute(plan)
    print(f"  done: {len(plan.actions) - failed} step(s) ok, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
