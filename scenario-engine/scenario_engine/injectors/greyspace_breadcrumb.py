"""TrueNorth Range - Greyspace breadcrumb injector (plan slice 4, ADR 0007).

Plants (or removes) breadcrumbs in the range's Greyspace simulated internet: a page or
file on a rehosted site, a DNS record in a Greyspace zone, an entry in the threat-intel
feed at ``intel.gs-infra.net/feed.txt``. Nothing is ever written to the shared corpus:
web breadcrumbs go to the range's overlay, served in front of it.

This injector only prepares the payload: it validates the crumbs and renders their
templated values per exercise. The worker delivers it (control-plane/worker/worker/
greyspace.py ``deliver_breadcrumbs``) to the range's Greyspace host by its backend's
channel (vSphere: ``bin/gs crumb plant`` on gs-core through VMware guest operations;
mock: recorded only). So it runs on every backend: ``touches_range_hosts`` is False.

Params::

    operation: plant | remove        (default plant; remove takes out this exercise's crumbs,
                                      or only ``ids``)
    crumbs:                           for plant, 1-20 of:
      - id: login-note                letters, digits, . _ : -
        kind: web                     web | dns | threat_feed
        site: pastebox.io             web: a site of the range's Greyspace
        path: /p/7f3k2.txt            web: absolute file path
        content: "creds: {{ token }}" web: text; {{ token }} {{ exercise_id }} {{ crumb_id }}
      - {id: c2-txt, kind: dns, name: cdn-sync.update-cdn-sync.net, type: TXT, value: "{{ token }}"}
      - {id: ioc-1, kind: threat_feed, type: domain, indicator: update-cdn-sync.net, note: AMBER HERON C2}
    salt: optional                    mixed into the token (else GREYSPACE_CRUMB_SALT)

``{{ token }}`` is ``breadcrumb_token(exercise, crumb id, salt)``: different per exercise,
so one team's find is no use to another, and recomputable by the validator
(validators/greyspace_breadcrumb.py) without storing it anywhere.

No observable telemetry: an event saying where a breadcrumb is would be searchable by the
Students looking for it. The run is recorded by the summary event's ground-truth labels.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from typing import Any

from .base import BaseInjector

KINDS = ("web", "dns", "threat_feed")
MAX_CRUMBS = 20
MAX_CONTENT = 32 * 1024
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_PLACEHOLDER = re.compile(r"\{\{\s*(token|exercise_id|crumb_id)\s*\}\}")


def breadcrumb_token(exercise: str, crumb_id: str, salt: str | None = None) -> str:
    """The per-exercise value a crumb's ``{{ token }}`` renders to."""
    salt = os.environ.get("GREYSPACE_CRUMB_SALT", "") if salt is None else salt
    digest = hashlib.sha256(f"greyspace-crumb:{salt}:{exercise}:{crumb_id}".encode()).hexdigest()
    return "GS-" + digest[:12].upper()


def render_text(text: str, exercise: str, crumb_id: str, salt: str | None) -> str:
    values = {"token": breadcrumb_token(exercise, crumb_id, salt), "exercise_id": exercise, "crumb_id": crumb_id}
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


class GreyspaceBreadcrumbInjector(BaseInjector):
    name = "greyspace_breadcrumb"
    description = "Plant or remove breadcrumbs (web files, DNS records, threat-feed entries) in the range's Greyspace."
    touches_range_hosts = False  # delivered by the worker's Greyspace seam, recorded on the mock backend
    execution_mode = "live"

    def validate_params(self) -> None:
        op = self.params.get("operation", "plant")
        assert op in ("plant", "remove"), f"operation must be plant or remove, not {op!r}"
        if op == "remove":
            ids = self.params.get("ids")
            assert ids is None or (isinstance(ids, list) and all(_ID.match(str(i)) for i in ids)), "ids: crumb ids"
            return
        crumbs = self.params.get("crumbs")
        assert isinstance(crumbs, list) and 1 <= len(crumbs) <= MAX_CRUMBS, f"crumbs: 1 to {MAX_CRUMBS} entries"
        seen: set[str] = set()
        for c in crumbs:
            assert isinstance(c, dict), "each crumb must be a mapping"
            cid = str(c.get("id") or "")
            assert _ID.match(cid), f"crumb id {cid!r}: letters, digits, . _ : -"
            assert cid not in seen, f"crumb id {cid} is used twice"
            seen.add(cid)
            kind = c.get("kind")
            assert kind in KINDS, f"crumb {cid}: kind must be one of {list(KINDS)}"
            if kind == "web":
                assert c.get("site") and str(c.get("path", "")).startswith("/"), f"crumb {cid}: site and /path"
                assert isinstance(c.get("content"), str), f"crumb {cid}: content (text)"
                assert len(c["content"].encode()) <= MAX_CONTENT, f"crumb {cid}: content over {MAX_CONTENT} bytes"
            elif kind == "dns":
                assert c.get("name") and c.get("value"), f"crumb {cid}: name and value"
                assert str(c.get("type", "TXT")).upper() in ("TXT", "A"), f"crumb {cid}: type TXT or A"
            else:
                assert c.get("indicator"), f"crumb {cid}: indicator"
                assert c.get("type") in ("domain", "ip", "url", "sha256"), f"crumb {cid}: type domain|ip|url|sha256"

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        self.validate_params()
        exercise = str(context.get("exercise_id") or context.get("range_id") or "")
        salt = self.params.get("salt")
        op = self.params.get("operation", "plant")
        if op == "remove":
            ids = self.params.get("ids")
            return {
                "success": True,
                "injector": self.name,
                "detail": f"remove {'all' if ids is None else len(ids)} breadcrumb(s) of {exercise}",
                "greyspace": {"operation": "remove", "exercise": exercise, "ids": ids},
            }
        out = []
        for c in self.params["crumbs"]:
            cid = str(c["id"])
            crumb: dict[str, Any] = {"id": cid, "kind": c["kind"]}
            if c["kind"] == "web":
                body = render_text(c["content"], exercise, cid, salt).encode("utf-8")
                crumb.update(site=str(c["site"]).lower(), path=str(c["path"]),
                             content_b64=base64.b64encode(body).decode("ascii"))
            elif c["kind"] == "dns":
                crumb.update(name=str(c["name"]).lower(), type=str(c.get("type", "TXT")).upper(),
                             value=render_text(str(c["value"]), exercise, cid, salt))
            else:
                crumb.update(type=c["type"], indicator=render_text(str(c["indicator"]), exercise, cid, salt),
                             note=render_text(str(c.get("note") or ""), exercise, cid, salt))
            out.append(crumb)
        return {
            "success": True,
            "injector": self.name,
            "detail": f"plant {len(out)} breadcrumb(s): " + ", ".join(f"{c['kind']}:{c['id']}" for c in out),
            "greyspace": {"operation": "plant", "exercise": exercise, "payload": {"exercise": exercise, "crumbs": out}},
        }
