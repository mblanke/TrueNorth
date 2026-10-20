"""An in-memory vSphere Automation (CIS) tagging service, for tests.

Serves the ``/api/cis/tagging`` (vSphere 7.0U2+) and legacy
``/rest/com/vmware/cis/tagging`` (``create_spec`` / ``value`` wrapped, ``id:`` paths,
``~action``) forms the provisioner speaks (worker/provisioners/vsphere_tags.py). The legacy
forms match what govmomi's vcsim v0.56.0 answered when probed; the ``/api`` forms follow
VMware's API reference (no simulator serves them).

Unlike vcsim it enforces SINGLE cardinality, as vCenter does: a second tag of a single
category on one object is a 400, so code that does not detach first fails here.
"""

from __future__ import annotations

import itertools
import json
import re

import httpx

_ids = itertools.count(1)


class FakeCis:
    def __init__(self) -> None:
        self.categories: dict[str, dict] = {}  # id -> {name, cardinality, ...}
        self.tags: dict[str, dict] = {}  # id -> {name, category_id}
        self.attached: dict[str, set[str]] = {}  # object id -> tag ids
        self.calls: list[tuple[str, str]] = []
        self.fail: str | None = None  # answer 500 to this action ("attach", ...) or to everything ("*")

    # -- state helpers for assertions -------------------------------------------------
    def category_id(self, name: str) -> str | None:
        return next((i for i, c in self.categories.items() if c["name"] == name), None)

    def add_category(self, name: str, cardinality: str = "SINGLE") -> str:
        cid = f"urn:cat:{next(_ids)}"
        self.categories[cid] = {"id": cid, "name": name, "cardinality": cardinality,
                                "description": "", "associable_types": ["VirtualMachine"]}
        return cid

    def add_tag(self, category: str, name: str) -> str:
        cid = self.category_id(category) or self.add_category(category)
        tid = f"urn:tag:{next(_ids)}"
        self.tags[tid] = {"id": tid, "name": name, "category_id": cid}
        return tid

    def tags_of(self, obj: str) -> dict[str, str]:
        """category name -> tag name of every tag on ``obj``."""
        return {self.categories[self.tags[t]["category_id"]]["name"]: self.tags[t]["name"]
                for t in self.attached.get(obj, set())}

    # -- HTTP ---------------------------------------------------------------------------
    def handles(self, path: str) -> bool:
        return path.startswith(("/api/cis/tagging/", "/rest/com/vmware/cis/tagging/"))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, query = request.url.path, request.url.query.decode()
        legacy = path.startswith("/rest/")
        body = json.loads(request.content) if request.content else {}
        base = "/rest/com/vmware/cis/tagging" if legacy else "/api/cis/tagging"
        rel = path[len(base):]
        action = re.sub(r"^~?action=", "", query) if query else ""
        rel = re.sub(r"/id:", "/", rel)
        self.calls.append((request.method, f"{path}?{query}" if query else path))
        if self.fail and self.fail in ("*", action):
            return httpx.Response(500, json={"error_type": "ERROR"})

        def ok(value=None):
            if value is None:
                return httpx.Response(200 if legacy else 204)
            # vcsim answers an empty list as {"value": null} on the legacy API.
            return httpx.Response(200, json={"value": value if value != [] else None} if legacy else value)

        def bad(kind: str):
            return httpx.Response(400, json={"type": f"com.vmware.vapi.std.errors.{kind}"} if legacy
                                  else {"error_type": kind.upper()})

        spec = body.get("create_spec", body) if legacy else body
        if rel == "/category" and request.method == "GET":
            return ok(list(self.categories))
        if m := re.fullmatch(r"/category/([^/]+)", rel):
            return ok(self.categories[m.group(1)]) if m.group(1) in self.categories else bad("not_found")
        if rel == "/category" and request.method == "POST":
            if self.category_id(spec["name"]):
                return bad("already_exists")
            cid = self.add_category(spec["name"], spec.get("cardinality", "SINGLE"))
            self.categories[cid].update(description=spec.get("description", ""),
                                        associable_types=spec.get("associable_types", []))
            return ok(cid)
        if action == "list-tags-for-category":
            cid = rel.split("/")[2] if legacy else body["category_id"]
            return ok([t for t, v in self.tags.items() if v["category_id"] == cid])
        if m := re.fullmatch(r"/tag/([^/]+)", rel):
            tid = m.group(1)
            if tid not in self.tags:
                return bad("not_found")
            if request.method == "DELETE":
                del self.tags[tid]
                for held in self.attached.values():
                    held.discard(tid)
                return ok()
            return ok(self.tags[tid])
        if rel == "/tag" and request.method == "POST":
            if any(v["name"] == spec["name"] and v["category_id"] == spec["category_id"] for v in self.tags.values()):
                return bad("already_exists")
            tid = f"urn:tag:{next(_ids)}"
            self.tags[tid] = {"id": tid, "name": spec["name"], "category_id": spec["category_id"]}
            return ok(tid)
        if action == "list-attached-tags":
            return ok(sorted(self.attached.get(body["object_id"]["id"], set())))
        if action == "list-attached-objects-on-tags":
            return ok([{"tag_id": t, "object_ids": [{"type": "VirtualMachine", "id": o}
                                                    for o, held in self.attached.items() if t in held]}
                       for t in body["tag_ids"]])
        if m := re.fullmatch(r"/tag-association/([^/]+)", rel):
            tid, obj = m.group(1), body["object_id"]["id"]
            held = self.attached.setdefault(obj, set())
            if action == "attach":
                cat = self.categories[self.tags[tid]["category_id"]]
                if cat["cardinality"] == "SINGLE" and any(
                        self.tags[t]["category_id"] == cat["id"] and t != tid for t in held):
                    return bad("invalid_argument")
                held.add(tid)
                return ok()
            if action == "detach":
                held.discard(tid)
                return ok()
        return httpx.Response(404, json={"error_type": "NOT_FOUND", "path": path})
