"""Withdraw a fixture course (C105) on an installed TrueNorth, from inside the api image.

Hosts installed before #147 published C105 (a CI / CATAPULT cmi5 fixture) to Students,
perhaps with its quizzes and a Moodle course. This calls
``POST /admin/fixture-courses/{code}/withdraw`` in-process as a named platform
administrator (scripts/_inprocess_api.py: the route's own permission check, CSRF and
validation still apply; no password or token is used). Without ``--apply`` it only
prints what would change. Nothing is deleted; running it again changes nothing.

    docker compose run --rm --no-deps -w /app -e PYTHONPATH=/app \\
      -v <app checkout>:/srcapp:ro --entrypoint python api \\
      /srcapp/scripts/withdraw_fixture_course.py --admin <upn> [--apply] [--tenant <id>] [--json]

Exit status: 0 done (or nothing to do), 1 the API reported errors (re-run after fixing
them), 2 refused or could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC = Path("/srcapp")


def summarise(body: dict) -> list[str]:
    head = "DRY RUN: nothing changed" if body["dry_run"] else ("APPLIED" if body["changed"] else "nothing to change")
    lines = [f"{body['code']} in tenant {body['tenant_id']}: {head}"]
    if not body["courses"]:
        lines.append(f"  no course with catalogue code {body['code']} in this tenant")
    for c in body["courses"]:
        lines.append(f"  course {c['id']} {c['name']!r}: {'published' if c['is_published'] else 'draft'}")
        for q in c["quizzes"]:
            state = "published" if q["is_published"] else "draft"
            lines.append(f"    quiz {q['id']} {q['title']!r}: {state}, {q['attempts']} attempts"
                         f" ({q['submitted_attempts']} graded)")
        lines.append(f"    enrolments {c['enrolments_total']} ({c['enrolments_graded']} with a grade),"
                     f" module progress {c['module_progress']}, quiz attempts {c['quiz_attempts']},"
                     f" Moodle results {c['moodle_results']}")
        for md in c["moodle"]:
            err = f" ({md['error']})" if md["error"] else ""
            lines.append(f"    moodle {md['platform_name']} course {md['live_idnumber']}: {md['visibility']}{err}")
        if not c["moodle"]:
            lines.append("    moodle: never published")
    verb = "would change" if body["dry_run"] else "changed"
    lines.append(f"  {verb}: {len(body['changes'])}")
    lines += [f"    {line}" for line in body["changes"]]
    kept = ", ".join(f"{k} {v}" for k, v in body["preserved"].items())
    lines.append(f"  kept as they are: {kept}")
    lines += [f"  ERROR {e}" for e in body["errors"]]
    if body["dry_run"] and body["changes"]:
        lines.append("  re-run with --apply to make these changes")
    return lines


def run(api, code: str, apply: bool, tenant: str | None, as_json: bool) -> int:
    params = {"dry_run": "false" if apply else "true"}
    if tenant:
        params["tenant_id"] = tenant
    status, body = api.post(f"/admin/fixture-courses/{code}/withdraw", params=params)
    if status != 200 or not isinstance(body, dict):
        detail = body.get("detail") if isinstance(body, dict) else body
        print(f"refused: {status} {detail}", file=sys.stderr)
        return 2
    print(json.dumps(body, indent=2) if as_json else "\n".join(summarise(body)))
    return 1 if body["errors"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--admin", required=True, help="email/UPN of an existing platform administrator")
    ap.add_argument("--code", default="C105", help="the fixture course's catalogue code (default C105)")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    ap.add_argument("--tenant", help="the tenant whose course it is (default: the administrator's)")
    ap.add_argument("--json", action="store_true", help="print the API's report as JSON")
    args = ap.parse_args()

    sys.path.insert(0, str(SRC / "scripts"))
    from _inprocess_api import connect  # the shared harness: identity override, fresh CSRF, 429 backoff

    connected = connect(args.admin)
    if connected is None:
        return 2
    _client, api = connected
    return run(api, args.code, args.apply, args.tenant, args.json)


if __name__ == "__main__":
    sys.exit(main())
