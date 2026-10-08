# TrueNorth Range

A cyber training range and learning platform: courses and Student progress against the
QSP/CFITES spine, exercises on ranges provisioned on vSphere, scenario injects,
detection credit, telemetry, after-action reports and a per-tenant Moodle.

FastAPI + Celery + PostgreSQL + OpenSearch behind an Angular 21 web app, with Keycloak
for sign-in. Release notes: [`CHANGELOG.md`](CHANGELOG.md).

## Quickstart

**Install (production / lab host).** The Ansible installer takes a host with Docker to a
running, AD-federated platform. Start at [`install/README.md`](install/README.md):

```bash
cd install
ansible-playbook site.yml --ask-vault-pass --check   # dry run
ansible-playbook site.yml --ask-vault-pass
```

**Releases.** A `vX.Y.Z` tag on `main` publishes five images by digest with a Trivy gate,
SBOMs and `release-manifest.json`; nothing deploys automatically. See
[`docs/release.md`](docs/release.md), and the upgrade notes in `CHANGELOG.md` before
moving an existing site.

**Develop.**

```bash
uv venv .venv --python 3.11
uv pip install --python .venv/bin/python -r requirements-test.txt "ruff==0.16.3"
bash scripts/dod.sh          # the Definition of Done gate (same checks as CI)
```

The development compose stack is `infra/platform/docker/compose.dev.yml`; the web app is
served on `:4200` and requires sign-in.

## Where to read next

- `RESUME.md`: current operational state, CI lanes and gotchas.
- `docs/current-state.md`: module-by-module status.
- `docs/deployment.md`: deployment options; Helm: `infra/k8s/README.md`.
- `docs/adr/`: architecture decisions (adapters, API contract, module boundaries).
- `CLAUDE.md` and `docs/AGENTS.md`: rules for agents working in this repository.

Proprietary; see `LICENSE`.
