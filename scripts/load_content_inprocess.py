"""Load the shipped content (content/) into an installed TrueNorth, from inside the api image.

scripts/load_content.py talks to the API over HTTP with a bearer token. An installed host
has no token to hand it (direct grants are off), so this runs the same loader against the
API in-process, acting as a named administrator: the token check is replaced by that
user's identity, and every request still goes through the API's routes, permission
checks, CSRF and validation. No password or token is used.

Run by the installer (roles/tn_seed, tn_load_shipped_content) as a one-off api container:

    docker compose run --rm --no-deps -w /app -e PYTHONPATH=/app \\
      -v <app checkout>:/srcapp:ro --entrypoint python api \\
      /srcapp/scripts/load_content_inprocess.py --admin <upn> [--publish] [--demo]

Idempotent: importers upsert, create-style steps skip what exists, publishing skips what
is published. --publish publishes every course, quiz and learning path (imports are
drafts); --demo adds the demo people (roster rows; without AD the installer then links
them to local sign-in accounts, app.link_demo_accounts), ranges and exercises (not
provisioned).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

SRC = Path("/srcapp")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--admin", required=True, help="email/UPN of an existing administrator")
    ap.add_argument("--publish", action="store_true", help="publish courses, quizzes and learning paths")
    ap.add_argument("--demo", action="store_true", help="demo people, ranges and exercises (not provisioned)")
    args = ap.parse_args()

    from app import auth
    from app.db import SessionLocal
    from app.main import app
    from app.models import User
    from fastapi.testclient import TestClient

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == args.admin).first()
        if user is None:
            print(f"administrator {args.admin} not found", file=sys.stderr)
            return 2
        ident = auth.TokenPayload(
            sub=user.keycloak_id,
            email=user.email,
            name=user.display_name or user.email,
            preferred_username=user.email,
        )
    finally:
        db.close()
    app.dependency_overrides[auth.get_token_identity] = lambda: ident

    spec = importlib.util.spec_from_file_location("load_content", SRC / "scripts/load_content.py")
    lc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lc)
    lc.ROOT = SRC
    lc.CONTENT = SRC / "content"

    client = TestClient(app, base_url="https://api.internal")
    client.get("/health/live")  # the CSRF middleware sets truenorth_csrf on every response

    class Api(lc.Api):
        def _send(self, method, path, body, ctype):
            # Double-submit CSRF, as the browser does; back off when rate-limited.
            headers = {"x-csrf-token": client.cookies.get("truenorth_csrf", "")}
            if ctype:
                headers["Content-Type"] = ctype
            for _ in range(20):
                r = client.request(method, path, content=body, headers=headers)
                if r.status_code != 429:
                    break
                time.sleep(int(r.json().get("retry_after", 60)) + 1)
            try:
                return r.status_code, (r.json() if r.content else None)
            except ValueError:
                return r.status_code, r.text[:400]

    api = Api("https://api.internal", None)
    if args.demo:
        real_post = api.post_json

        def post_json(path, payload=None):
            if path.endswith("/provision"):
                return "skip", "not provisioned by the installer"
            return real_post(path, payload)

        api.post_json = post_json

    rep = lc.Report()
    lc.load_curriculum(api, rep)
    lc.load_golden_images(api, rep)
    templates = lc.load_yaml_items(
        api, rep, "Range templates", "/templates", sorted((SRC / "content/ranges").glob("*/template.yaml"))
    )
    scenarios = lc.load_yaml_items(
        api, rep, "Scenarios", "/scenarios", sorted((SRC / "content/scenarios").glob("*/scenario.yaml"))
    )
    lc.load_detections(api, rep)
    if args.demo:
        lc.load_demo(api, rep, templates, scenarios)
    failed = False
    if args.publish:
        print("Publishing")
        for label, path in (("courses", "/courses"), ("quizzes", "/quizzes"), ("learning paths", "/learning-paths")):
            status, rows = api.get(path + "?limit=1000")
            if status != 200:
                status, rows = api.get(path)
            if isinstance(rows, dict):
                rows = rows.get("items") or rows.get("results") or []
            done = errors = 0
            for row in rows or []:
                if row.get("is_published"):
                    continue
                st, out = api._send(
                    "PATCH", f"{path}/{row['id']}", json.dumps({"is_published": True}).encode(), "application/json"
                )
                if isinstance(st, int) and 200 <= st < 300:
                    done += 1
                else:
                    errors += 1
                    print(f"  FAIL {label} {row.get('name') or row.get('title') or row['id']}: {st} {str(out)[:160]}")
            failed = failed or errors > 0
            print(f"  {label}: {len(rows or [])} listed, {done} published now, {errors} failed")
    return 1 if (failed or rep.failures) else 0


if __name__ == "__main__":
    sys.exit(main())
