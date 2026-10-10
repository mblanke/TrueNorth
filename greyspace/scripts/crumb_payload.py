#!/usr/bin/env python3
"""Build a breadcrumb payload with the scenario engine's greyspace_breadcrumb injector.

    crumb_payload.py --params PARAMS.json --exercise ID --out PAYLOAD.json
    crumb_payload.py --t0 SITE --exercise ID --out PAYLOAD.json   (check-t0.sh's three crumbs)

Writes the payload ``bin/gs crumb plant --file`` takes (exactly what the worker sends to
a gs-core VM) and prints ``<crumb id>=<token>`` for each crumb, so a check or an
instructor can see the values the exercise's breadcrumbs carry.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scenario-engine"))

from scenario_engine.injectors.greyspace_breadcrumb import (  # noqa: E402 — after the path insert
    GreyspaceBreadcrumbInjector,
    breadcrumb_token,
)


def t0_params(site: str) -> dict:
    zone = ".".join(site.split(".")[-2:])
    return {
        "salt": "t0",
        "crumbs": [
            {"id": "note", "kind": "web", "site": site, "path": "/notes/handover.txt", "content": "vpn token {{ token }}\n"},
            {"id": "txt", "kind": "dns", "name": f"crumb.{zone}", "type": "TXT", "value": "{{ token }}"},
            {"id": "ioc", "kind": "threat_feed", "type": "domain", "indicator": "update-cdn-sync.net",
             "note": "AMBER HERON"},
        ],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--params")
    src.add_argument("--t0", metavar="SITE")
    p.add_argument("--exercise", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    params = t0_params(args.t0) if args.t0 else json.loads(Path(args.params).read_text(encoding="utf-8"))
    result = GreyspaceBreadcrumbInjector(params).execute({"exercise_id": args.exercise})
    Path(args.out).write_text(json.dumps(result["greyspace"]["payload"]) + "\n", encoding="utf-8")
    for crumb in params["crumbs"]:
        print(f"{crumb['id']}={breadcrumb_token(args.exercise, crumb['id'], params.get('salt'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
