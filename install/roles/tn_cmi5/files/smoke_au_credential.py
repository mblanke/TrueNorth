"""Smoke check: cmi5 launching is not fail-closed, and its credential works (roles/tn_cmi5).

Run in the RUNNING api container (`docker compose exec -T api python -c`), so it sees the
environment the API actually serves with, not the env file. Prints one line, never a
secret; exit 0 only when all three hold:

  1. app.cmi5.lms.au_credential() returns: the exact gate every cmi5 launch, LTI launch and
     AU request passes first, which answers 503 when CMI5_LRS_AUTH is unset or is LRS_AUTH;
  2. the LRS accepts that credential (a State read: 404 or 200, not 401);
  3. it is least-privilege: the LRS refuses it a statement read (403, docs/cmi5.md).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse

import httpx
from app.cmi5 import lms

PROBE = "https://truenorth.invalid/installer-smoke"


def main() -> int:
    try:
        au = lms.au_credential()
    except lms.Cmi5Error as exc:
        print(f"FAIL launches answer {exc.status}: {exc.message}")
        return 1
    base = os.getenv("LRS_URL", "http://lrs:8080").rstrip("/")
    headers = {"Authorization": f"Basic {au}", "X-Experience-API-Version": "1.0.3"}
    agent = json.dumps({"objectType": "Agent", "account": {"homePage": PROBE, "name": "installer-smoke"}})
    query = urllib.parse.urlencode({"activityId": PROBE, "agent": agent, "stateId": "installer-smoke"})
    with httpx.Client(timeout=15.0) as client:
        state = client.get(f"{base}/xapi/activities/state?{query}", headers=headers).status_code
        read = client.get(f"{base}/xapi/statements?limit=1", headers=headers).status_code
    if state not in (200, 404):
        print(f"FAIL the LRS answers {state} to the AU credential (expected 404 or 200): re-run 50-stack-up")
        return 1
    if read != 403:
        print(f"FAIL the AU credential may read statements ({read}, expected 403): it is not least-privilege")
        return 1
    print(f"OK au_credential set; LRS state read {state}; statement read {read}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
