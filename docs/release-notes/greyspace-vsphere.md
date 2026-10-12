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
  The root CA is name-constrained to the stack's zones, lives 825 days (leaves 397,
  renewed at stack start), and its key (0600) is in the `gs-ca` volume, mounted by the
  one-shot `ca` service only: the web farm and threat stub get only their own zones'
  certificates, the NPCs and probe only `root.crt` (amended 2026-10-11; before, every
  one of them could read the root key). A stack started before this makes a new root;
  its old `gs-certs` volume is no longer used (`docker volume rm <project>_gs-certs`).
- **NPC traffic:** `npc_profile: office-day | quiet-night` is now real (simulated users
  in the stack), not recorded only.
- **Breadcrumbs:** `greyspace_breadcrumb` inject (web file, DNS record, threat-feed
  entry; per-exercise token) and a `greyspace_breadcrumb` scoring method (objective
  `validator: greyspace_breadcrumb`, config `crumb_id`, `answer`). Tokens are an HMAC
  under a per-exercise key derived from the installation secret; with no secret, no token
  is issued and no objective is judged. Nothing in the product yet collects a team's
  `answer` for it (no flag-submission UI): until one does, such an objective scores only
  when the answer is put in its config.
- **Range Designer:** the Internet/Cloud stencil is the Greyspace block; export writes
  `greyspace:`, YAML import draws it back.
- **Corpus ops:** `corpus.py ingest | report | sample-warc`; `bin/gs corpus mount|verify`.

## Configuration (worker)

| Variable | Default | Meaning |
|---|---|---|
| `GREYSPACE_HOST_TEMPLATE` | `greyspace-host` | inventory template or Content Library item gs-core is cloned from |
| `GREYSPACE_HOST_CORES` / `_MEMORY_MB` / `_DISK_GB` | 4 / 8192 / 80 | gs-core's size |
| `GREYSPACE_CORPUS_NFS` | empty | leave empty: the bundled T0 corpus. An NFS corpus does not work yet (see Known limitations) |
| `GREYSPACE_MANIFEST_DIR` | empty | `<dir>/<tier>/manifest.json` the worker (and API) render from; T0 is built in |
| `GREYSPACE_CRUMB_KEY` | the first key of `TN_SECRETS_KEY` | the breadcrumb secret (≥32 characters; set the same on the worker and wherever objectives are scored). Unset and no `TN_SECRETS_KEY`: breadcrumb injects fail rather than plant guessable tokens. Changing it changes the tokens of exercises already running |
| `GREYSPACE_CRUMB_SALT` | empty | an optional extra mixed into breadcrumb tokens (not a secret; the key is) |
| `TN_SECRETS_KEY` | (required already) | derives gs-core's service-account password; rotating its first key means rebuilding existing ranges' Greyspace |

## Known limitations

- **Only the T0 corpus works on vSphere.** gs-core has one NIC, on the range's Greyspace
  network; the range router lets zones out to the depot (8081, 3142) only, so an NFS
  export is unreachable and `GREYSPACE_CORPUS_NFS` fails the configure stage at
  `corpus mount`. T1+ needs a second gs-core NIC on a storage port group not routed to
  any zone; not built ([greyspace-corpus.md §3](../greyspace-corpus.md)).
- Templates need a network for gs-core (default name `greyspace`) with a router on it;
  without one the range builds without gs-core and its Greyspace is `failed` (`plan`).
- Only VyOS routers are configured to route the range to Greyspace; pfSense/OPNsense are not.
- No search service, no threat-actor C2/payload compose project, no access-log shipping
  to OpenSearch; breadcrumbs are scored by token, not by fetch logs.
- One Greyspace stack per Docker host (fixed public supernet), as before.
