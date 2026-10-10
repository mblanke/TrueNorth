# Greyspace on vSphere (branch `claude/greyspace-vsphere`): release notes

Written 2026-10-09. Design: [ADR 0007](../adr/0007-greyspace.md) (decisions 6–10);
slice status: [docs/greyspace-plan.md](../greyspace-plan.md).

## What it was proven on, and what it was not

| | Proven on | Not proven on |
|---|---|---|
| The stack: DNS chain, BGP, HTTP/HTTPS, threat stub, overlay, mail, NTP, NPC traffic, breadcrumbs (plant/serve/check/remove), WARC ingest | the T0 Docker stack, CI job `greyspace` (`greyspace/scripts/check-t0.sh`), and locally on Docker 29 (arm64) | — |
| gs-core VM: clone, NIC on the range's Greyspace port group, cloud-init and bundle in guestinfo, router route, teardown with the range | **vcsim** v0.56.0 (CI job `vsphere-sim`, `tests/integration/test_vsphere_sim.py`) | **a real vCenter / ESXi** |
| Configure stage (cloud-init wait, corpus, `bin/gs up`, health) and breadcrumb delivery on gs-core | unit tests with a fake guest channel (`tests/worker/test_greyspace_host.py`) | VMware guest operations: vcsim runs no VMware Tools and cannot do them |
| `greyspace-host` golden image (Packer `derived.pkr.hcl`, `files/linux/roles/greyspace-host.sh`) | a contract test that it pulls every pinned image | it has never been built |

No real vSphere was available. Before relying on it, build `greyspace-host`, provision
`content/ranges/greyspace-internet` on the lab vCenter, and check the range's Greyspace
reaches `deployed` (the Greyspace page shows each configure step).

## New

- **vSphere:** a range with a Greyspace block gets a `gs-core` VM in its build and a
  post-deploy `configure_range` task that starts the stack on it. Status `configuring`
  → `deployed` / `failed` (with the step that failed).
- **Services:** mail (SMTP, MX, webmail), NTP, HTTPS from a Greyspace root CA.
- **NPC traffic:** `npc_profile: office-day | quiet-night` is now real (simulated users
  in the stack), not recorded only.
- **Breadcrumbs:** `greyspace_breadcrumb` inject (web file, DNS record, threat-feed
  entry; per-exercise token) and a `greyspace_breadcrumb` validator.
- **Range Designer:** the Internet/Cloud stencil is the Greyspace block; export writes
  `greyspace:`, YAML import draws it back.
- **Corpus ops:** `corpus.py ingest | report | sample-warc`; `bin/gs corpus mount|verify`.

## Configuration (worker)

| Variable | Default | Meaning |
|---|---|---|
| `GREYSPACE_HOST_TEMPLATE` | `greyspace-host` | inventory template or Content Library item gs-core is cloned from |
| `GREYSPACE_HOST_CORES` / `_MEMORY_MB` / `_DISK_GB` | 4 / 8192 / 80 | gs-core's size |
| `GREYSPACE_CORPUS_NFS` | empty | `server:/export` of the corpus, mounted read-only on gs-core; empty: the bundled T0 corpus |
| `GREYSPACE_MANIFEST_DIR` | empty | `<dir>/<tier>/manifest.json` the worker (and API) render from; T0 is built in |
| `GREYSPACE_CRUMB_SALT` | empty | mixed into breadcrumb tokens (set the same on the worker and wherever the validator runs) |
| `TN_SECRETS_KEY` | (required already) | derives gs-core's service-account password; rotating its first key means rebuilding existing ranges' Greyspace |

## Known limitations

- Templates need a network for gs-core (default name `greyspace`) with a router on it;
  without one the range builds without gs-core and its Greyspace is `failed` (`plan`).
- Only VyOS routers are configured to route the range to Greyspace; pfSense/OPNsense are not.
- No search service, no threat-actor C2/payload compose project, no access-log shipping
  to OpenSearch; breadcrumbs are scored by token, not by fetch logs.
- One Greyspace stack per Docker host (fixed public supernet), as before.
