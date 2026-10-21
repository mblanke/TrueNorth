"""vCenter tags and range folder names for TrueNorth's objects (ADR 0012 follow-up).

Four tag categories, each single-cardinality on VirtualMachine (one value per VM), so the
vCenter UI can filter TrueNorth's VMs by them and the Infrastructure page can group them:

    tn-type      golden-template | template-copy | range-vm | test-clone | depot | mgmt | retired
    tn-os        the OS alias (windows-server-2022, ubuntu-2404, pfsense, ...)
    tn-template  the catalogue id the VM came from (ubuntu-lts, srv2022, ...)
    tn-range     the range: ``<range-name>-<key>``, one tag shared by all of its VMs

The template kit (scripts/lab/vcenter_organize.py, through govc) applies the same
categories to templates, host copies and test clones; tests/contracts keeps the two
definitions equal. The provisioner tags range VMs as it builds them, best effort: a tag
that cannot be applied is logged and the build goes on.

Range folders are ``<VSPHERE_RANGE_FOLDER>/<range-name>-<key>``, ``<key>`` the first 12 hex
digits of the range id (``range_key``; 8 would collide across ranges far too soon). They
are found again by that suffix (``belongs_to``), never by name, so a renamed range still
resolves; so does an earlier build's folder, named exactly ``<id8>`` (``truenorth/ranges``),
which is only ever found, never reused for a new build (it cannot prove which range of
that id8 it is).

Tagging is bounded: every call times out after ``timeout`` seconds, and the first failure
(or reaching ``deadline``, the build's budget) stops tagging for the rest of the build with
one warning. Tags are never worth a range.

Tagging goes through the vSphere Automation (CIS) tagging API over the provisioner's own
REST session, in both of its dialects: ``/api/cis/tagging/...`` (vSphere 7.0U2+) and the
legacy ``/rest/com/vmware/cis/tagging/...`` (older vCenters, vcsim; bodies wrapped in
``create_spec`` / ``value``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable

from . import template_copies

logger = logging.getLogger(__name__)

CATEGORIES: dict[str, str] = {
    "tn-type": "TrueNorth object type: golden-template, template-copy, range-vm, test-clone, depot, mgmt, retired",
    "tn-os": "TrueNorth OS alias of the VM (windows-server-2022, ubuntu-2404, pfsense, ...)",
    "tn-template": "TrueNorth catalogue id of the golden template the VM came from",
    "tn-range": "TrueNorth range the VM belongs to: <range-name>-<first 12 hex digits of its id>",
}
TYPES = ("golden-template", "template-copy", "range-vm", "test-clone", "depot", "mgmt", "retired")
CARDINALITY = "SINGLE"
ASSOCIABLE = ["VirtualMachine"]
# Earlier builds put range folders here; they are still found (destroy, organize).
LEGACY_RANGE_ROOTS = ("truenorth/ranges",)
# Old hand-made templates moved aside (vcenter-organize.sh): never a clone source.
RETIRED_FOLDER = "_retired"
KEY_LEN = 12
_NAME_LIMIT = 40  # of the range-name part: vCenter allows 80 for a folder, 256 for a tag


def slug(text: str, limit: int = _NAME_LIMIT) -> str:
    """Lowercase letters, digits and single dashes: safe as a folder and a tag name."""
    out = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return out[:limit].rstrip("-")


def range_key(range_id: str) -> str:
    """The first 12 hex digits of a range id: what its folder and tn-range tag are named for."""
    return re.sub(r"[^0-9a-f]", "", str(range_id).lower())[:KEY_LEN]


def range_label(name: str | None, range_id: str) -> str:
    """``<range-name>-<key>`` (the folder and the tn-range tag), or ``<key>`` with no usable name."""
    key = range_key(range_id)
    base = slug(name or "")
    if base == key or base.endswith(f"-{key}"):
        base = base[: -len(key)].rstrip("-")  # a name that already ends in the key
    return f"{base}-{key}" if base else key


def folder_key(folder_name: str) -> str | None:
    """The range key a folder or tag is named for: a 12-hex ``-<key>`` suffix (or the whole
    name), or exactly ``<id8>`` (an earlier build's folder). None for anything else."""
    name = str(folder_name or "").lower()
    if m := re.search(rf"(?:^|-)([0-9a-f]{{{KEY_LEN}}})$", name):
        return m.group(1)
    return name if re.fullmatch(r"[0-9a-f]{8}", name) else None


def belongs_to(folder_name: str, range_id: str) -> bool:
    """Whether a range folder (or tn-range tag) is range ``range_id``'s, by its key."""
    key = folder_key(folder_name)
    return bool(key) and range_key(range_id).startswith(key)


def is_legacy_folder(folder_name: str) -> bool:
    """An earlier build's ``<id8>`` folder: shared by every range of that id8."""
    return len(folder_key(folder_name) or "") == 8


def template_id(source: str | None) -> str:
    """The catalogue id behind a clone source: a host copy's base, a library item's name."""
    name = str(source or "")
    if name.startswith("library:"):
        name = name[len("library:"):]
    copy = template_copies.parse(name)
    return copy.base if copy else name


def range_vm_tags(vm_def: dict, label: str) -> dict[str, str]:
    """The tags of one range VM: category -> value (a category with no value is left out)."""
    wanted = {"tn-type": "range-vm", "tn-range": label}
    if os_alias := slug(vm_def.get("os") or "", 80):
        wanted["tn-os"] = os_alias
    if tmpl := template_id(vm_def.get("_source") or vm_def.get("template_name")):
        wanted["tn-template"] = tmpl
    return wanted


# CIS tagging paths per REST dialect (see the provisioner's _authenticate).
_API = {
    "categories": ("GET", "/api/cis/tagging/category"),
    "category": ("GET", "/api/cis/tagging/category/{id}"),
    "create_category": ("POST", "/api/cis/tagging/category"),
    "tags_for_category": ("POST", "/api/cis/tagging/tag?action=list-tags-for-category"),
    "tag": ("GET", "/api/cis/tagging/tag/{id}"),
    "create_tag": ("POST", "/api/cis/tagging/tag"),
    "delete_tag": ("DELETE", "/api/cis/tagging/tag/{id}"),
    "attached": ("POST", "/api/cis/tagging/tag-association?action=list-attached-tags"),
    "attach": ("POST", "/api/cis/tagging/tag-association/{id}?action=attach"),
    "detach": ("POST", "/api/cis/tagging/tag-association/{id}?action=detach"),
}
_REST = {
    "categories": ("GET", "/rest/com/vmware/cis/tagging/category"),
    "category": ("GET", "/rest/com/vmware/cis/tagging/category/id:{id}"),
    "create_category": ("POST", "/rest/com/vmware/cis/tagging/category"),
    "tags_for_category": ("POST", "/rest/com/vmware/cis/tagging/tag/id:{id}?~action=list-tags-for-category"),
    "tag": ("GET", "/rest/com/vmware/cis/tagging/tag/id:{id}"),
    "create_tag": ("POST", "/rest/com/vmware/cis/tagging/tag"),
    "delete_tag": ("DELETE", "/rest/com/vmware/cis/tagging/tag/id:{id}"),
    "attached": ("POST", "/rest/com/vmware/cis/tagging/tag-association?~action=list-attached-tags"),
    "attach": ("POST", "/rest/com/vmware/cis/tagging/tag-association/id:{id}?~action=attach"),
    "detach": ("POST", "/rest/com/vmware/cis/tagging/tag-association/id:{id}?~action=detach"),
}

# (method, absolute path, JSON body or None) -> the parsed JSON reply (None for no body).
Call = Callable[[str, str, dict | None], Awaitable[object]]


class TagClient:
    """Categories and tags created on first use (idempotent, race-safe), then attached.

    One per build: it caches ids and holds an asyncio lock of the build's event loop."""

    def __init__(self, call: Call, dialect: str, *, timeout: float = 5.0, deadline: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._call = call
        self._timeout = timeout
        self._deadline = deadline  # a monotonic time: no tagging after it
        self._clock = clock
        self.stopped: str | None = None  # why tagging stopped for this build
        self._rest = dialect == "rest"
        self._paths = _REST if self._rest else _API
        self._lock = asyncio.Lock()
        self._categories: dict[str, str] | None = None  # name -> id
        self._tags: dict[tuple[str, str], str] = {}  # (category name, tag name) -> id
        self._tag_category: dict[str, str] = {}  # tag id -> category name (ours only)
        self._listed: set[str] = set()  # category names whose tags were listed

    async def _do(self, op: str, *, id: str = "", body: dict | None = None):  # noqa: A002 -- the path's own word
        method, path = self._paths[op]
        reply = await asyncio.wait_for(self._call(method, path.format(id=id), body), self._timeout)
        return reply.get("value") if self._rest and isinstance(reply, dict) else reply

    def _spec(self, spec: dict) -> dict:
        return {"create_spec": spec} if self._rest else spec

    async def _category_ids(self) -> dict[str, str]:
        if self._categories is None:
            found: dict[str, str] = {}
            for cid in await self._do("categories") or []:
                info = await self._do("category", id=cid) or {}
                if info.get("name") in CATEGORIES:
                    found[info["name"]] = cid
            self._categories = found
        return self._categories

    async def _category(self, name: str) -> str:
        cats = await self._category_ids()
        if name not in cats:
            spec = {"name": name, "description": CATEGORIES[name], "cardinality": CARDINALITY,
                    "associable_types": ASSOCIABLE}
            try:
                cats[name] = await self._do("create_category", body=self._spec(spec))
                logger.info("vCenter tag category %s created", name)
            except Exception:  # another worker made it first: list again
                self._categories = None
                cats = await self._category_ids()
                if name not in cats:
                    raise
        return cats[name]

    async def _list_tags(self, category: str, cid: str) -> None:
        if category in self._listed:
            return
        body = None if self._rest else {"category_id": cid}
        for tid in await self._do("tags_for_category", id=cid, body=body) or []:
            info = await self._do("tag", id=tid) or {}
            if info.get("name"):
                self._tags[(category, info["name"])] = tid
                self._tag_category[tid] = category
        self._listed.add(category)

    async def _tag(self, category: str, name: str) -> str:
        cid = await self._category(category)
        await self._list_tags(category, cid)
        if (category, name) not in self._tags:
            spec = {"name": name, "description": "", "category_id": cid}
            try:
                tid = await self._do("create_tag", body=self._spec(spec))
            except Exception:  # made meanwhile by another build: list again
                self._listed.discard(category)
                await self._list_tags(category, cid)
                if (category, name) not in self._tags:
                    raise
            else:
                self._tags[(category, name)] = tid
                self._tag_category[tid] = category
        return self._tags[(category, name)]

    async def ensure(self, wanted: dict[str, str]) -> dict[str, str]:
        """Category -> tag id for every wanted value, created where missing."""
        async with self._lock:
            if self.stopped is not None:  # stopped while this VM waited for the lock
                raise RuntimeError(self.stopped)
            return {cat: await self._tag(cat, value) for cat, value in wanted.items()}

    def _stop(self, why: str) -> None:
        if self.stopped is None:
            self.stopped = why
            logger.warning("vCenter tagging stopped for the rest of this build (the build goes on): %s", why)

    async def apply(self, vm_id: str, wanted: dict[str, str]) -> bool:
        """``_apply``, bounded: never raises. False once tagging has stopped for this build
        (a failed or timed-out call, or the build's ``deadline`` reached); one warning."""
        if self.stopped is None and self._deadline is not None and self._clock() >= self._deadline:
            self._stop("the build's time budget is nearly spent")
        if self.stopped is not None:
            return False
        try:
            await self._apply(vm_id, wanted)
            return True
        except TimeoutError:  # asyncio.wait_for (3.11+: the builtin)
            self._stop(f"a vCenter tagging call took longer than {self._timeout:g}s")
        except Exception as exc:  # noqa: BLE001 -- tags are for people browsing vCenter, not the build
            self._stop(str(exc) or type(exc).__name__)
        return False

    async def _apply(self, vm_id: str, wanted: dict[str, str]) -> None:
        """Give the VM exactly these values in these categories: another value of the same
        category (single cardinality) is detached first. Other categories are untouched."""
        ids = await self.ensure(wanted)
        obj = {"object_id": {"type": "VirtualMachine", "id": vm_id}}
        attached = set(await self._do("attached", body=obj) or [])
        for tid in attached:
            cat = self._tag_category.get(tid)
            if cat in ids and ids[cat] != tid:
                await self._do("detach", id=tid, body=obj)
        for tid in ids.values():
            if tid not in attached:
                await self._do("attach", id=tid, body=obj)

    async def delete_range_tags(self, range_id: str) -> list[str]:
        """Delete the tn-range tags of range ``range_id`` (its VMs are gone), matched by its
        full 12-hex key only (tags never had the 8-digit form). Returns their names."""
        async with self._lock:
            cats = await self._category_ids()
            if "tn-range" not in cats:
                return []
            await self._list_tags("tn-range", cats["tn-range"])
            gone = [(name, tid) for (cat, name), tid in self._tags.items()
                    if cat == "tn-range" and folder_key(name) == range_key(range_id)]
            for name, tid in gone:
                await self._do("delete_tag", id=tid)
                self._tags.pop(("tn-range", name), None)
            return [name for name, _ in gone]
