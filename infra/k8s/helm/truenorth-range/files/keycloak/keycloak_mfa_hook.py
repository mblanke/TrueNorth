"""The Helm hook's runner (templates/job-keycloak-mfa.yaml): wait for Keycloak, take an
admin token, and run keycloak_mfa.py with it on stdin, as the installer's 60-keycloak does.

Environment: KC_URL (with /auth when Keycloak serves there), KC_REALM, KC_ADMIN_REALM,
KC_ADMIN_USER, KC_ADMIN_PASSWORD (from a hook-scoped Secret; never given to the api pods),
KC_RP_ID, KC_GROUPS (optional), TN_API_ADMIN_CLIENT_ID / TN_API_ADMIN_CLIENT_SECRET
(optional: the API's view-realm client), KC_WAIT_SECONDS (default 300).
Standard library only; runs in the api image. Never prints a credential.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def admin_token(base: str) -> str:
    data = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli",
                                   "username": os.environ["KC_ADMIN_USER"],
                                   "password": os.environ["KC_ADMIN_PASSWORD"]}).encode()
    realm = os.environ.get("KC_ADMIN_REALM", "master")
    with urllib.request.urlopen(f"{base}/realms/{realm}/protocol/openid-connect/token", data, timeout=15) as r:
        return json.load(r)["access_token"]


def main() -> int:
    base = os.environ["KC_URL"].rstrip("/")
    deadline = time.monotonic() + int(os.environ.get("KC_WAIT_SECONDS", "300"))
    while True:
        try:
            token = admin_token(base)
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403):
                print(f"keycloak-mfa: Keycloak refused the admin credential (HTTP {exc.code})", file=sys.stderr)
                return 1
            err = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            err = type(exc).__name__
        if time.monotonic() > deadline:
            print(f"keycloak-mfa: Keycloak at {base} did not answer ({err})", file=sys.stderr)
            return 1
        time.sleep(5)

    import keycloak_mfa

    argv = ["--base", base, "--realm", os.environ.get("KC_REALM", "truenorth"),
            "--spec", os.path.join(HERE, "mfa-spec.json")]
    if os.environ.get("KC_RP_ID"):
        argv += ["--rp-id", os.environ["KC_RP_ID"]]
    if os.environ.get("KC_GROUPS"):
        argv += ["--groups", os.environ["KC_GROUPS"]]
    if os.environ.get("TN_API_ADMIN_CLIENT_ID"):
        argv += ["--ensure-api-client", os.environ["TN_API_ADMIN_CLIENT_ID"]]
    sys.stdin = io.StringIO(token + "\n")
    return keycloak_mfa.main(argv)


if __name__ == "__main__":
    sys.exit(main())
