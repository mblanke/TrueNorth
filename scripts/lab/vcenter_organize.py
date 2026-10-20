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
    /<dc>/vm/TrueNorth/Templates/_retired/                       old hand-made templates
    /<dc>/vm/TrueNorth/Ranges/<range-name>-<id8>/                one folder per range

What ``apply`` does, each step idempotent:

1. Folders: creates the three above where missing.
2. Templates under the earlier ``truenorth/templates``: a catalogue id or a host copy of one
   goes to ``TrueNorth/Templates``; anything else (``kali-linux``) to ``_retired``. Nothing is
   deleted. A name that already exists in the target is left where it is and reported (two
   templates of one name would make the provisioner's choice arbitrary).
3. Range folders under ``truenorth/ranges``: moved to ``TrueNorth/Ranges`` and named
   ``<range-name>-<id8>`` from the TrueNorth database (id8 alone without it); an id-only
   folder already in ``TrueNorth/Ranges`` is renamed the same way. Found by the id8 suffix,
   like the provisioner does, so a renamed range keeps its folder.
4. Anything else left under ``truenorth/`` moves to the same place under ``TrueNorth/``.
5. ``truenorth`` folders are removed bottom-up, each only if vCenter reports it empty at
   that moment (a vCenter folder delete would delete what is in it).
6. Tags (categories created single-cardinality where missing): golden templates, host copies,
   test clones (``tn-testclone-<id>-<host>``), range VMs (in a range folder, or named
   ``<id8>-*`` for a TrueNorth range), TN-DEPOT01 (``depot``) and TN-MGMT01 (``mgmt``).
   Only tn-* categories are touched; a range VM's tn-os / tn-template set at build is kept.

Never touched: any VM outside those folders and names (WIN11-TMPL, vCenter, vCLS agents,
other people's templates). ``apply`` refuses while a TrueNorth range is provisioning or
destroying.
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
    "tn-type": "TrueNorth object type: golden-template, template-copy, range-vm, test-clone, depot, mgmt",
    "tn-os": "TrueNorth OS alias of the VM (windows-server-2022, ubuntu-2404, pfsense, ...)",
    "tn-template": "TrueNorth catalogue id of the golden template the VM came from",
    "tn-range": "TrueNorth range the VM belongs to: <range-name>-<id8>",
}
TYPES = ("golden-template", "template-copy", "range-vm", "test-clone", "depot", "mgmt")
ROOT, TEMPLATES, RANGES = "TrueNorth", "TrueNorth/Templates", "TrueNorth/Ranges"
RETIRED = "TrueNorth/Templates/_retired"
LEGACY = "truenorth"
LEGACY_MAP = {"templates": "Templates", "ranges": "Ranges"}
BUSY_STATES = ("provisioning", "destroying")
TEST_CLONE = re.compile(r"^tn-testclone-(?P<id>.+)-(?P<host>[A-Za-z0-9_]+)$")
_COPY = re.compile(r"^(?P<base>[^@]+)@(?P<host>[A-Za-z0-9_-]+)-(?P<date>\d{8})$")
_NAME_LIMIT = 40


# -- names (same rules as vsphere_tags / template_copies in the worker) ----------------------

def slug(text: str | None, limit: int = _NAME_LIMIT) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return out[:limit].rstrip("-")


def range_label(name: str | None, range_id: str) -> str:
    id8 = str(range_id)[:8].lower()
    base = slug(name or "")
    if base == id8 or base.endswith(f"-{id8}"):
        base = base[: -len(id8)].rstrip("-")
    return f"{base}-{id8}" if base else id8


def folder_id8(name: str) -> str | None:
    """The id8 a range folder is named for (``<id8>`` or ``*-<id8>``), else None."""
    m = re.search(r"(?:^|-)([0-9a-f]{8})$", str(name or "").lower())
    return m.group(1) if m else None


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
    for moid, _ in _rows(run(["find", "-l", "-I", root, "-type", "m", "-config.template", "true"])):
        if moid in vms:
            vms[moid].template = True
    inv = Inventory(dc, sorted(vms.values(), key=lambda v: v.path), set())
    inv.folders = {inv.rel(p.strip()) for p in run(["find", root, "-type", "f"]).splitlines()
                   if p.strip() and p.strip() != root}
    for cat, tag, objects in read_tags(run):
        for obj in objects:
            vm = vms.get(obj.split(":", 1)[-1])
            if vm is not None:
                vm.tags[cat] = tag
    return inv


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


def _ranges_by_id8(ranges: Iterable[dict]) -> dict[str, dict]:
    return {str(r["id"])[:8].lower(): r for r in ranges if r.get("state") != "destroyed"}


def _range_templates(ranges: Iterable[dict]) -> dict[str, str]:
    """Range VM name -> the template it was cloned from (provisioner_output vms[].template)."""
    out = {}
    for r in ranges:
        for vm in r.get("vms") or []:
            if vm.get("name") and vm.get("template"):
                out[str(vm["name"])] = str(vm["template"])
    return out


def make_plan(inv: Inventory, ranges: list[dict] | None, os_map: dict[str, str], *,
              depot: str = "TN-DEPOT01", mgmt: str = "TN-MGMT01") -> Plan:
    """Everything ``apply`` would do, in order. ``ranges`` None: the TrueNorth database was not
    read (folders keep their id8 names; range VMs are found by their folder only)."""
    plan = Plan()
    known = _ranges_by_id8(ranges or [])
    if ranges is None:
        plan.notes.append("TrueNorth ranges not read: range folders keep their current names; "
                          "range VMs are recognised by their folder only")
    for r in ranges or []:
        if r.get("state") in BUSY_STATES:
            plan.refuse.append(f"range {r.get('name')!r} ({str(r['id'])[:8]}) is {r['state']}")

    folders = set(inv.folders)
    final: dict[str, str] = {vm.moid: inv.rel(vm.path) for vm in inv.vms}  # moid -> relative path after moves

    def mkdir(rel: str) -> None:
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            p = "/".join(parts[:i])
            if p not in folders:
                plan.add("mkdir", inv.abs(p))
                folders.add(p)

    def names_in(rel_folder: str) -> set[str]:
        return {p.rsplit("/", 1)[1] for p in final.values() if p.rsplit("/", 1)[0] == rel_folder}

    def move_vm(vm: Vm, to: str, why: str) -> bool:
        if vm.name in names_in(to):
            plan.notes.append(f"{vm.path}: a VM named {vm.name!r} is already in {inv.abs(to)}; left where it is")
            return False
        mkdir(to)
        plan.add("move", inv.abs(final[vm.moid]), inv.abs(to), why=why)
        final[vm.moid] = f"{to}/{vm.name}"
        return True

    for rel in (TEMPLATES, RANGES):
        mkdir(rel)

    legacy_vms = [vm for vm in inv.vms if inv.rel(vm.path).startswith(f"{LEGACY}/")]

    # 2. templates under truenorth/templates
    for vm in legacy_vms:
        rel = inv.rel(vm.path)
        if not rel.startswith(f"{LEGACY}/templates/"):
            continue
        ours = vm.name in os_map or (copy_base(vm.name) or "") in os_map
        move_vm(vm, TEMPLATES if ours else RETIRED,
                "catalogue template" if ours else "not a catalogue template: retired, not deleted")

    # 3. range folders: truenorth/ranges/<f> -> TrueNorth/Ranges/<label>; id-only ones renamed
    def label_for(folder: str) -> str:
        id8 = folder_id8(folder)
        r = known.get(id8 or "")
        return range_label(r.get("name"), str(r["id"])) if r else folder

    moved_folders: dict[str, str] = {}  # old rel -> new rel
    for rel in sorted(f for f in inv.folders if f.startswith(f"{LEGACY}/ranges/") and f.count("/") == 2):
        name = rel.rsplit("/", 1)[1]
        label = label_for(name)
        target = f"{RANGES}/{label}"
        existing = next((f for f in sorted(folders) if f.startswith(f"{RANGES}/") and f.count("/") == 2
                         and folder_id8(f.rsplit("/", 1)[1]) == folder_id8(name) and folder_id8(name)), None)
        if existing:  # the range already has a folder in the new tree: move its VMs in
            for vm in legacy_vms:
                if inv.rel(vm.path).startswith(f"{rel}/"):
                    move_vm(vm, existing, f"range {folder_id8(name)}")
            continue
        plan.add("move", inv.abs(rel), inv.abs(RANGES), why=f"range {folder_id8(name) or name}")
        folders.add(f"{RANGES}/{name}")
        if label != name:
            plan.add("rename", inv.abs(f"{RANGES}/{name}"), label, why="named for the range")
            folders.discard(f"{RANGES}/{name}")
            folders.add(target)
        moved_folders[rel] = target if label != name else f"{RANGES}/{name}"
        for vm in legacy_vms:
            if inv.rel(vm.path).startswith(f"{rel}/"):
                final[vm.moid] = moved_folders[rel] + inv.rel(vm.path)[len(rel):]
    for rel in sorted(f for f in inv.folders if f.startswith(f"{RANGES}/") and f.count("/") == 2):
        name = rel.rsplit("/", 1)[1]
        if folder_id8(name) == name and (label := label_for(name)) != name and f"{RANGES}/{label}" not in folders:
            plan.add("rename", inv.abs(rel), label, why="named for the range")
            folders.discard(rel)
            folders.add(f"{RANGES}/{label}")
            for vm in inv.vms:
                if final[vm.moid].startswith(f"{rel}/"):
                    final[vm.moid] = f"{RANGES}/{label}" + final[vm.moid][len(rel):]

    # 4. anything else still under truenorth/
    for vm in legacy_vms:
        rel = final[vm.moid]
        if not rel.startswith(f"{LEGACY}/"):
            continue
        parts = rel.split("/")[1:-1]
        if parts:
            parts[0] = LEGACY_MAP.get(parts[0], parts[0])
        move_vm(vm, "/".join([ROOT, *parts]), "out of the earlier truenorth folder")

    # 5. empty truenorth folders, deepest first (checked empty again before each delete)
    left = {p for p in final.values() if p.startswith(f"{LEGACY}/")}
    gone_folders = set(moved_folders)
    for rel in sorted((f for f in inv.folders if f == LEGACY or f.startswith(f"{LEGACY}/")),
                      key=lambda f: (-f.count("/"), f)):  # deepest first; stable across runs
        if rel in gone_folders or any(rel.startswith(f"{m}/") for m in moved_folders):
            gone_folders.add(rel)  # moved along with its parent
            continue
        if any(p.startswith(f"{rel}/") for p in left) or any(
                f.startswith(f"{rel}/") and f not in gone_folders for f in inv.folders):
            plan.notes.append(f"{inv.abs(rel)} is not empty after the moves; kept")
            continue
        plan.add("rmdir", inv.abs(rel))
        gone_folders.add(rel)

    # 6. tags
    templates = _range_templates(ranges or [])
    for vm in inv.vms:
        rel = final[vm.moid]
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        want: dict[str, str] = {}
        keep_existing: set[str] = set()
        if vm.template and folder == TEMPLATES:
            if base := copy_base(vm.name):
                want = {"tn-type": "template-copy", "tn-template": base}
            else:
                want = {"tn-type": "golden-template", "tn-template": vm.name}
        elif vm.template and (base := copy_base(vm.name)) and base in os_map:
            want = {"tn-type": "template-copy", "tn-template": base}
        elif m := TEST_CLONE.match(vm.name):
            want = {"tn-type": "test-clone", "tn-template": m["id"]}
        elif vm.name == depot:
            want = {"tn-type": "depot"}
        elif vm.name == mgmt:
            want = {"tn-type": "mgmt"}
        elif not vm.template:
            in_folder = folder.startswith(f"{RANGES}/") and folder.count("/") == 2
            id8 = folder_id8(folder.rsplit("/", 1)[1]) if in_folder else None
            prefix = vm.name.split("-", 1)[0].lower()
            if id8 is None and prefix in known and len(prefix) == 8:
                id8 = prefix
            if id8 and (in_folder or id8 in known):
                r = known.get(id8)
                label = range_label(r.get("name"), str(r["id"])) if r else folder.rsplit("/", 1)[1]
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
                # vCenter deletes a folder WITH what is in it: only ever an empty one.
                if run(["ls", a.args[0]]).strip():
                    log(f"  ! {a.args[0]} is not empty now; kept")
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
        p.add_argument("--ranges", help="JSON list of TrueNorth ranges {id, name, state, vms}; omit if unavailable")
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

    ranges = json.loads(Path(args.ranges).read_text()) if args.ranges else None
    plan = make_plan(read_inventory(args.dc), ranges, os_map, depot=args.depot, mgmt=args.mgmt)
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
            print("  nothing changed: run again once no TrueNorth range is provisioning or destroying", file=sys.stderr)
            return 3
    if args.cmd == "plan":
        return 0
    failed = execute(plan)
    print(f"  done: {len(plan.actions) - failed} step(s) ok, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
