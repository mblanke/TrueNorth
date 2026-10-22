"""Mark a golden image ``built`` with its template name, from inside the api image (ADR 0012).

The last step of ADR 0012 decision 9: after a template is built, replicated and a test
clone has taken TrueNorth's customization, it is marked ``built`` in the golden-image list.
On an installed host there is no API token to hand ``build.sh``'s register-back, so this
does it in-process as a named administrator (scripts/_inprocess_api.py): the API's routes,
``infra:write`` permission, CSRF and validation all apply, and no token or password is used.

Run by scripts/lab/build-phase.sh as a one-off api container on TN-MGMT01:

    docker compose -f <app>/infra/platform/docker/compose.prod.yml --env-file <.env.production> \\
      run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v <kit checkout>:/srcapp:ro \\
      --entrypoint python api /srcapp/scripts/register_golden_image_inprocess.py \\
      --admin <upn> --id <catalogue id> --template <template name>

Catalogue images (imported from content/catalogue/vm_iso_catalogue.csv) are looked up by
``catalogue_id`` on the hypervisor and PATCHed (``/golden-images/{id}``) only where they
differ: ``template_name``, ``build_status`` and, when given, ``datastore``. A custom
variant (``--variant``) that is not registered yet is created with ``POST /golden-images``
from the metadata flags, as ``build.sh register`` would.

With ``--build-record`` (``infra/vsphere/packer/logs/<id>.build.json``, written by
``build.sh`` and filled in by ``build.sh replicate``) the build is recorded too (ADR 0013
§2): build id, checksum, build date, ISO checksum and per-host copies. Recording never
promotes; an admin promotes a build on the Golden Images page, and only then do ranges
clone it.

Idempotent: an image already built with the same template is reported unchanged.

Exit: 0 registered or unchanged; 1 not registered (no such image, refused, API error);
2 the administrator does not exist.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

SRC = Path("/srcapp")


def _ok(status: Any) -> bool:
    return isinstance(status, int) and 200 <= status < 300


def _brief(body: Any, limit: int = 200) -> str:
    if isinstance(body, dict):
        body = body.get("message") or body.get("detail") or body
    return " ".join(str(body).split())[:limit]


def register(
    api: Any,
    *,
    catalogue_id: str,
    template: str,
    hypervisor: str = "vsphere",
    status: str = "built",
    datastore: str | None = None,
    variant: dict | None = None,
    build: dict | None = None,
) -> int:
    """Reconcile one image. ``api`` has ``get``/``post``/``patch`` returning ``(status, body)``.
    ``variant`` (os_family, version, role, os_aliases) allows creating a missing custom image.
    ``build`` (``build_body``) is recorded with the image (never promoted)."""
    code, rows = api.get("/golden-images", params={"hypervisor": hypervisor})
    if not _ok(code) or not isinstance(rows, list):
        print(f"golden image: FAIL listing images: {code} {_brief(rows)}")
        return 1
    img = next(
        (
            r
            for r in rows
            if isinstance(r, dict)
            and r.get("catalogue_id") == catalogue_id
            and r.get("hypervisor", hypervisor) == hypervisor
        ),
        None,
    )

    if img is None:
        if variant is None:
            print(
                f"golden image: FAIL no image '{catalogue_id}' on {hypervisor}. Import the catalogue first "
                "(Golden images page, or POST /golden-images/import-catalogue)"
            )
            return 1
        payload = {
            "catalogue_id": catalogue_id,
            "hypervisor": hypervisor,
            "template_name": template,
            "build_status": status,
            "enabled": True,
            **{k: v for k, v in variant.items() if v is not None},
        }
        if datastore:
            payload["datastore"] = datastore
        code, out = api.post("/golden-images", json=payload)
        if not _ok(code) or not isinstance(out, dict):
            print(f"golden image: FAIL create variant '{catalogue_id}': {code} {_brief(out)}")
            return 1
        print(f"golden image: created variant {catalogue_id} -> {out.get('build_status')} (template {template})")
        return _record_build(api, out, catalogue_id, build)

    want = {"template_name": template, "build_status": status}
    if datastore:
        want["datastore"] = datastore
    drift = {k: v for k, v in want.items() if img.get(k) != v}
    if not drift:
        print(f"golden image: unchanged {catalogue_id} ({status}, template {template})")
        return _record_build(api, img, catalogue_id, build)
    code, out = api.patch(f"/golden-images/{img['id']}", json=drift)
    if not _ok(code) or not isinstance(out, dict):
        print(f"golden image: FAIL update '{catalogue_id}': {code} {_brief(out)}")
        return 1
    print(
        f"golden image: {catalogue_id} -> {out.get('build_status')} (template {out.get('template_name')}; "
        f"changed {', '.join(sorted(drift))})"
    )
    if not out.get("enabled", True):
        print(f"golden image: NOTE {catalogue_id} is disabled in the catalogue; ranges will not resolve to it")
    return _record_build(api, out, catalogue_id, build)


def build_body(record: dict) -> dict:
    """The API's ``build`` from a build.sh record (``logs/<id>.build.json``)."""
    return {"build_id": record["build_id"], "checksum": record["checksum"],
            "build_date": record.get("build_date") or None, "iso_checksum": record.get("iso_checksum") or "",
            "template_name": record.get("template") or "", "copies": record.get("copies") or {}}


def _record_build(api: Any, img: dict, catalogue_id: str, build: dict | None) -> int:
    """PATCH the build record onto the image (idempotent on the API's side)."""
    if build is None:
        return 0
    code, out = api.patch(f"/golden-images/{img['id']}", json={"build": build})
    if not _ok(code):
        print(f"golden image: FAIL recording build {build.get('build_id')} of '{catalogue_id}': {code} {_brief(out)}")
        return 1
    hosts = ", ".join(sorted(build.get("copies") or {})) or "no host copies yet"
    print(f"golden image: {catalogue_id} build {build['build_id']} recorded ({hosts}); "
          "an admin promotes it on the Golden Images page before ranges use it")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--admin", required=True, help="email/UPN of an existing administrator (infra:write)")
    ap.add_argument("--id", required=True, dest="catalogue_id", help="golden image catalogue_id, e.g. srv2022")
    ap.add_argument("--template", help="vCenter template name (default: the id)")
    ap.add_argument("--hypervisor", default="vsphere")
    ap.add_argument("--status", default="built", choices=["planned", "building", "built", "failed"])
    ap.add_argument("--datastore", default=None)
    ap.add_argument("--variant", action="store_true", help="create the image as a custom variant if missing")
    ap.add_argument("--os-family", choices=["windows", "linux", "appliance"])
    ap.add_argument("--version", dest="os_version", default=None)
    ap.add_argument("--role", default=None)
    ap.add_argument("--alias", action="append", default=[], help="topology OS alias (repeatable)")
    ap.add_argument("--build-record", default=None,
                    help="build.sh's logs/<id>.build.json: record the build too (ADR 0013; never promotes)")
    args = ap.parse_args(argv)
    build = None
    if args.build_record:
        import json

        try:
            build = build_body(json.loads(Path(args.build_record).read_text()))
        except (OSError, ValueError, KeyError) as exc:
            print(f"golden image: FAIL reading build record {args.build_record}: {exc}")
            return 1
    variant = None
    if args.variant:
        if not args.os_family:
            ap.error("--variant needs --os-family")
        variant = {"os_family": args.os_family, "version": args.os_version, "role": args.role, "os_aliases": args.alias}

    sys.path.insert(0, str(SRC / "scripts"))
    from _inprocess_api import connect  # the shared harness: identity override, fresh CSRF, 429 backoff

    connected = connect(args.admin)
    if connected is None:
        return 2
    _client, api = connected
    return register(
        api,
        catalogue_id=args.catalogue_id,
        template=args.template or args.catalogue_id,
        hypervisor=args.hypervisor,
        status=args.status,
        datastore=args.datastore,
        variant=variant,
        build=build,
    )


if __name__ == "__main__":
    sys.exit(main())
