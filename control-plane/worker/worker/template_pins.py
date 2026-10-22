"""Pin every range VM to its golden image's promoted build (ADR 0013 §2).

Runs in the provision task after the topology is rendered (and gs-core planned), before
anything is reserved or built. For a backend that clones golden templates
(``PINNED_BACKENDS``), each VM's ``template_name`` is looked up in the golden-image
registry and the VM gets ``template_build``:

* **promoted**: ``{"promoted": True, "catalogue_id", "build_id", "checksum", "build_date",
  "iso_checksum", "template_name", "copies": {host: copy name}}``. The provisioner then
  places the VM only on a host holding that build's copy and clones exactly that copy
  (provisioners/vsphere_api.py). Every VM of one image in a range gets the same build:
  it is resolved once here.
* **transition**: ``{"promoted": False, "catalogue_id", "template_name", "allowed_by"}``,
  only while ``TN_TEMPLATE_ALLOW_UNPROMOTED`` allows the image (below). The provisioner
  clones the image's plain template, and the range carries a loud warning.

Anything else fails the build before it starts (``TemplatePinError``, final: a retry
cannot help): a VM whose template is not a registered, enabled golden image, or whose
image has no promoted build and is not allowed by the flag. There is no fallback.

``TN_TEMPLATE_ALLOW_UNPROMOTED`` (installer ``tn_template_allow_unpromoted``):

* ``false`` / empty (the installer default): off.
* ``true``: every image whose ``build_status`` is ``built`` may be used unpromoted.
* a comma list of catalogue ids (``ubuntu-lts``): only those, and only when ``built``.

It **turns off per image**: an image with a promoted build always clones that build,
whatever the flag says; the flag only ever covers images that have none yet. It exists so
the work site's current, unpromoted images keep building ranges until builds are promoted.

``PLATFORM_TEMPLATES`` is the explicit allow-list of templates the worker adds to a range
itself (Greyspace's gs-core, ``greyspace-host``). They are catalogue images like any other
and pin the same way; only while their catalogue row is not imported yet may the flag let
them through by name (when it allows their catalogue id). Any other unregistered template
fails.

A role image (render.py ``role_fallback_template``) must pin like any image. If it cannot,
its VM fails clearly: the plain OS is never built in its place.
"""

from __future__ import annotations

import json
import logging
import os

from . import db_ops

logger = logging.getLogger(__name__)

FLAG = "TN_TEMPLATE_ALLOW_UNPROMOTED"
# Templates TrueNorth itself adds to a range, by template name -> catalogue id (see above).
PLATFORM_TEMPLATES: dict[str, str] = {"greyspace-host": "greyspace-host"}
PINNED_BACKENDS = {"vsphere_api": "vsphere"}
_ON = {"1", "true", "yes", "on"}
_OFF = {"", "0", "false", "no", "off"}


class TemplatePinError(RuntimeError):
    """A VM's template cannot be pinned to a promoted build: the build must not start."""


def flag_allows() -> tuple[bool, frozenset[str]]:
    """(every built image, named catalogue ids) the transition flag allows, ignoring promotions."""
    raw = os.getenv(FLAG, "").strip()
    if raw.lower() in _OFF:
        return False, frozenset()
    if raw.lower() in _ON:
        return True, frozenset()
    return False, frozenset(p.strip() for p in raw.split(",") if p.strip())


def _copies(raw) -> dict[str, str]:
    try:
        got = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return {str(k).lower(): str(v) for k, v in got.items()} if isinstance(got, dict) else {}


def _index(rows: list[dict]) -> dict[str, dict]:
    """Template name -> image row: the image's template_name, else its catalogue id (as
    render.golden_image_resolver resolves), then the catalogue id itself."""
    out: dict[str, dict] = {}
    for row in rows:
        out.setdefault(row["image_template"] or row["catalogue_id"], row)
    for row in rows:
        out.setdefault(row["catalogue_id"], row)
    return out


def _promoted(row: dict) -> dict:
    return {
        "promoted": True,
        "catalogue_id": row["catalogue_id"],
        "build_id": row["build_id"],
        "checksum": row["checksum"],
        "build_date": str(row["build_date"]) if row.get("build_date") else "",
        "iso_checksum": row.get("iso_checksum") or "",
        "template_name": row.get("build_template") or row["image_template"] or row["catalogue_id"],
        "copies": _copies(row.get("copies")),
    }


class Pinner:
    """Resolves each template name to one build, once per range build."""

    def __init__(self, rows: list[dict]) -> None:
        self.by_name = _index(rows)
        self.allow_all, self.named = flag_allows()
        self._cache: dict[str, dict | str] = {}

    def pin(self, template_name: str) -> dict | str:
        """The pin for ``template_name``, or why it has none (a str)."""
        if template_name not in self._cache:
            self._cache[template_name] = self._resolve(template_name)
        return self._cache[template_name]

    def _flag_allows(self, cid: str) -> bool:
        return self.allow_all or cid in self.named

    def _resolve(self, name: str) -> dict | str:
        row = self.by_name.get(name)
        if row is None:
            # A platform-owned template the worker adds itself (PLATFORM_TEMPLATES) before its
            # catalogue row is imported: only the transition flag lets it through, unverified.
            cid = PLATFORM_TEMPLATES.get(name)
            if cid and self._flag_allows(cid):
                return {"promoted": False, "catalogue_id": cid, "template_name": name, "allowed_by": FLAG}
            why = f"template {name!r} is not an enabled golden image on this hypervisor"
            if cid:
                why += f" (import the catalogue's {cid!r} row, register and promote a build; or name it in {FLAG})"
            return why
        if row.get("build_id"):
            return _promoted(row)
        cid = row["catalogue_id"]
        why = f"golden image {cid!r} has no promoted build: register a build and promote it on the Golden Images page"
        if not (self.allow_all or self.named):
            return why
        if not self._flag_allows(cid):
            return why + f" ({FLAG} does not name it)"
        if row.get("build_status") != "built":
            return why + f" ({FLAG} allows only built images; it is {row.get('build_status')!r})"
        return {"promoted": False, "catalogue_id": cid, "template_name": name, "allowed_by": FLAG}


def pin_vms(pinner: Pinner, vms: list[dict]) -> tuple[list[dict], list[str]]:
    """``vms`` with ``template_build`` set on each, and the errors of those that cannot be pinned.

    A role image that cannot be pinned fails its VM (ADR 0013 §2): the plain-OS fallback
    render.py offers (``role_fallback_template``) is never taken here, so a range never
    quietly builds the bare OS where the template asked for the role."""
    out: list[dict] = []
    errors: list[str] = []
    for vm in vms:
        vm = dict(vm)
        got = pinner.pin(vm.get("template_name") or "")
        fallback = vm.pop("role_fallback_template", None)
        if isinstance(got, str):
            if fallback:
                got += f" (a role image: the plain OS {fallback!r} is not built in its place)"
            errors.append(f"VM {vm.get('name')!r}: {got}")
        else:
            vm["template_build"] = got
        out.append(vm)
    return out, errors


def pin(session_factory, backend: str, template: dict) -> dict:
    """``template`` with every VM pinned (module docstring); unchanged for other backends.

    Raises ``TemplatePinError`` naming every VM that cannot be pinned; nothing is built."""
    hypervisor = PINNED_BACKENDS.get(backend)
    if hypervisor is None or not template.get("vms"):
        return template
    with session_factory() as db:
        pinner = Pinner(db_ops.golden_image_pin_rows(db, hypervisor))
    vms, errors = pin_vms(pinner, template["vms"])
    if errors:
        raise TemplatePinError("; ".join(errors))
    unpromoted = sorted({v["template_build"]["catalogue_id"] for v in vms if not v["template_build"]["promoted"]})
    if unpromoted:
        logger.warning("[pins] UNPROMOTED golden images in this range (allowed by %s, transition only): %s",
                       FLAG, ", ".join(unpromoted))
    return {**template, "vms": vms}
