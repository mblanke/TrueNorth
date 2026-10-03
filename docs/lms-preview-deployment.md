# LMS and readiness test previews

Deployed locally on 2026-09-27. These are interactive, fictional design previews,
not backend-connected learning or readiness records.

- Learning hub: `/learning/previews?view=development`
- Commander view: `/learning/previews?view=readiness`
- Moodle entry: `/learning/previews?view=moodle`
- Separate Moodle test service: `http://localhost:8083/`

The Angular route retains the learning hub's existing guards. The iframe documents
are public static assets containing fictional data only. They run with scripts
allowed but without same-origin privileges, forms, or network connections.
Preview state resets on reload; no enrolments, grades, tasking, or messages are written.
The preview exports have no external runtime dependencies.

Moodle retains its existing database and data volumes. Its startup hook
`infra/platform/nginx/moodle-public-port.sh` makes PHP report public port 8083,
matching the compose SITE_URL and avoiding an internal-port redirect loop.
Keep those values aligned if changing the test port. The hook runs after the
upstream image's nginx rewrite, so nginx.conf must not be bind-mounted directly.

Rebuild and deploy only the affected service:

```sh
docker compose -p truenorth -f infra/platform/docker/compose.dev.yml -f infra/platform/docker/compose.moodle.yml build web
docker compose -p truenorth -f infra/platform/docker/compose.dev.yml -f infra/platform/docker/compose.moodle.yml up -d --no-deps web moodle-db moodle
```

Verification: Angular lint passed; Docker's production Angular build passed;
deployed commander evidence switching and LMS phase switching worked in the browser;
Moodle login succeeded and its dashboard was displayed. Moodle currently shows
no courses in the administrator's course overview. LTI/grade passback/cmi5 was
not configured or verified by this deployment.

Repository-wide DoD did not pass: `.venv/bin/python` is a Linux x86-64 ELF executable
on macOS. The gate stops at its Ruff availability check before running tests.
