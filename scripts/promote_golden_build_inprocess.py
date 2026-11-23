# Author: Marc-Andre Blanke
# Created: 2026-11-23T20:47:00Z
"""Promote a golden image's newest recorded build, in-process as a named platform administrator.

Stopgap for issue #527: the Golden Images page has no Promote button yet; the API has
``POST /golden-images/{image_id}/builds/{build_id}/promote`` (platform administrator,
audited, ADR 0013 §2). This uses the same harness as register_golden_image_inprocess.py
(scripts/_inprocess_api.py): the API's own route, RBAC (require_platform_admin), CSRF and
audit all apply, and no token or password is used. Run on TN-MGMT01 as a one-off api
container:

    docker compose -f <app>/infra/platform/docker/compose.prod.yml --env-file <.env.production> \\
      run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v <kit checkout>:/srcapp:ro \\
      --entrypoint python api /srcapp/scripts/promote_golden_build_inprocess.py \\
      --admin <upn> --reason "<why>" <catalogue id>...

Promotes the newest build of each image (the API lists builds newest first), unless one is
already promoted: that is reported and left alone. Exit 0 all promoted or already promoted;
1 any failure; 2 the administrator does not exist.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

SRC = Path("/srcapp")


def _ok(status: Any) -> bool:
    return isinstance(status, int) and 200 <= status < 300


def promote(api: Any, *, catalogue_ids: list[str], reason: str, hypervisor: str = "vsphere") -> int:
    """Promote each image's newest build. 0 all promoted or already promoted, 1 any failure."""
    code, rows = api.get("/golden-images", params={"hypervisor": hypervisor})
    if not _ok(code) or not isinstance(rows, list):
        print(f"promote: FAIL listing golden images: {code} {rows}")
        return 1
    by_id = {r.get("catalogue_id"): r for r in rows}
    failed = 0
    for cid in catalogue_ids:
        img = by_id.get(cid)
        if not img:
            print(f"promote: FAIL {cid}: no such golden image on {hypervisor}")
            failed += 1
            continue
        code, body = api.get(f"/golden-images/{img['id']}/builds")
        if not _ok(code) or not isinstance(body, dict):
            print(f"promote: FAIL {cid}: listing builds: {code} {body}")
            failed += 1
            continue
        if body.get("promoted_build_id"):
            print(f"promote: {cid} already promoted ({body['promoted_build_id']}); left alone")
            continue
        builds = body.get("builds") or []
        if not builds:
            print(f"promote: FAIL {cid}: no recorded build")
            failed += 1
            continue
        build_id = builds[0]["build_id"]
        code, out = api.post(f"/golden-images/{img['id']}/builds/{build_id}/promote", json={"reason": reason})
        if _ok(code):
            print(f"promote: {cid} -> {build_id} promoted")
        else:
            print(f"promote: FAIL {cid} {build_id}: {code} {out}")
            failed += 1
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--admin", required=True, help="UPN of an existing platform administrator")
    ap.add_argument("--reason", required=True, help="recorded in the audit entry")
    ap.add_argument("--hypervisor", default="vsphere")
    ap.add_argument("ids", nargs="+", help="catalogue ids, e.g. srv2022 ubuntu-lts")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(SRC / "scripts"))
    from _inprocess_api import connect

    connected = connect(a.admin)
    if connected is None:
        return 2
    _client, api = connected
    return promote(api, catalogue_ids=a.ids, reason=a.reason, hypervisor=a.hypervisor)


if __name__ == "__main__":
    sys.exit(main())
