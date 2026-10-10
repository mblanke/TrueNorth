# ADR 0007 — Greyspace: a simulated internet attached to a range

- Status: accepted (stage-4 scope 2026-10-07; decisions 6-10 amended 2026-10-09 for the
  vSphere gs-core VM, proven on vcsim and the T0 Docker stack, not on a real vCenter)
- Date: 2026-10-07
- MOSA pillar: modular design, designated key interfaces
- Source: `docs/greyspace-plan.md` (decisions of 2026-10-03), mockup
  `control-plane/web/src/assets/previews/greyspace.html`
- Numbering: 0005 and 0006 are taken by open branches (detection credit, scheduler), so
  this is 0007.

## Context
Ranges are no-egress islands. Students and red cells need a believable internet:
backbone routing, root DNS, rehosted sites, attacker infrastructure, and later simulated
users. The content is large (10 TB+ for the full corpus) and must never be written to by
an exercise, while exercises need to hide breadcrumbs in some sites per range.

## Decision
1. **One corpus layout and one manifest format for every tier.** A corpus is a directory
   with `manifest.json` (`greyspace-corpus/1`), `checksums.sha256` and `sites/<fqdn>/…`
   (static trees), optionally `warc/` for provenance. The manifest assigns each site its
   "public" address, names the ISPs that own the prefixes, lists the threat-actor
   domains, and records source and licence per site. Tiers differ only in size and where
   they live:

   | Tier | What | Cap | Where | Built by |
   |---|---|---|---|---|
   | T0 | ~20 synthetic CC0 sites, a tiny video, 2 threat domains | 100 MB | generated per build | `greyspace/scripts/corpus.py t0` |
   | T1 | Mac test sample, shallow mirrors of permissive content | **5 GB hard** | `~/greyspace-corpus` | `greyspace/scripts/build-sample.sh --tier mac` |
   | T2 | Lab corpus | ~50 GB | NetApp, NFS read-only | planned, `docs/greyspace-corpus.md` |
   | Full | Rehosted internet | 10 TB+ | NetApp, NFS read-only | planned, `docs/greyspace-corpus.md` |

   Nothing large is committed; T0 is generated (only small text fixtures live in git).
2. **Everything about the runtime is generated from the manifest and the range's block**
   (`control-plane/api/app/greyspace/config.py`, pure standard library, shared by the API
   and `greyspace/scripts/corpus.py`): a Docker Compose stack of FRR routers (one per ISP,
   eBGP full mesh, each originating its prefix), CoreDNS root, TLD and authoritative
   servers (a real delegation chain with glue), an Unbound resolver iterating from the
   Greyspace root, an nginx web farm, and a threat-actor stub. Light images only:
   `frrouting/frr`, `coredns/coredns`, `nginx:alpine`, `alpine` + unbound.
3. **Corpus read-only, breadcrumbs in a per-range overlay.** The web farm mounts the
   corpus `:ro` and a per-range volume rw, and tries the overlay first
   (`try_files /overlay/… /corpus/…`). The overlay mirrors the corpus layout.
4. **Addressing.** The first ISP's first /24 is the infrastructure segment (routers .2+,
   root DNS .10, TLD .11, authoritative .12, resolver .53, threat .66, web farm .80).
   The Docker network spans the supernet of all ISP prefixes (internal: no route out),
   so Docker's isolation rules pass routed traffic; hosts still reach sites through the
   ISP routers, whose static routes are more specific. ISP prefixes must share a /8.
5. **A range gets Greyspace by a block**, either the template's `greyspace:` key
   (`scenario-engine/schemas/template.schema.json`) or `PUT /ranges/{id}/greyspace`.
   State is one row per range in `range_greyspace` (app/greyspace/models.py, migration
   `a2b3c4d5e6f7`). API: `GET /greyspace/corpora`, `GET|PUT|DELETE
   /ranges/{id}/greyspace`, `GET /ranges/{id}/greyspace/config`; range RBAC
   (RANGE_READ / RANGE_UPDATE), tenant-scoped with 404 for foreign ranges, lab-session
   ranges never changed here, 409 while a range operation is in flight, 422 with the
   reasons when a block cannot render on its corpus.
6. **The worker deploys through a seam, per backend by registry** (ADR 0001):
   `control-plane/worker/worker/greyspace.py` `DEPLOYERS` / `HOSTED`. `mock` records the
   block as `deployed`. `vsphere_api` builds a **gs-core VM with the range**: `plan_host`
   adds it to the rendered template before VLANs are reserved (worker/greyspace_host.py:
   golden image `GREYSPACE_HOST_TEMPLATE`, default `greyspace-host`; one NIC on the
   template network the block's `network` names, at that subnet's last address; cloud-init
   with a service account and a bootstrap; the stack rendered with `host=True` and, for
   T0, the corpus, as a tar.gz in `guestinfo.tn.greyspace.bundle.<n>` chunks). The router
   holding that network's gateway gets `greyspace_route` (VyOS: a static route to the
   public prefix via gs-core, zones allowed to it, the Greyspace resolver). Since gs-core
   is an annotated range VM, placement, rollback and teardown are the range's. Any other
   backend reports `pending_infrastructure`.
7. **A post-deploy configure stage.** `configure_range` (worker/configure_tasks.py, task
   contract, queue `provision`) runs after `provision_range` has marked the range ready,
   queued by `after_provision` for `HOSTED` backends. It logs in to gs-core through
   `BaseProvisioner.run_in_guest` (vSphere: VMware guest operations; no network path into
   the range) and runs cloud-init wait, `bootstrap.sh configure` (corpus: NFS read-only
   from `GREYSPACE_CORPUS_NFS`, else the bundled T0; `bin/gs up`) and `bin/gs health`.
   Every step's outcome is recorded; the block ends `deployed` or `failed` (with `stage`
   and `error`). The service account's password is never stored: HMAC of
   `TN_SECRETS_KEY` (first key) and the range id; guestinfo holds its SHA-512 crypt hash.
8. **More services, all generated.** Mail (Mailpit; MX for every site zone to
   `mail.gs-infra.net`, webmail at `webmail.<zone>` of webmail sites), NTP (chrony,
   local clock), and with `trust_ca` a Greyspace root CA made at stack start with one
   certificate per zone (SNI), published at `http://pki.gs-infra.net/root.crt`.
   `gs-infra.net` is Greyspace's own zone; a corpus may not use it. Every image is pinned
   by digest; local images build from greyspace/images on pinned bases.
9. **Breadcrumbs.** The `greyspace_breadcrumb` injector (scenario-engine) prepares a
   payload: web files (into the overlay), DNS records (into the authoritative zone files,
   reloaded in seconds), threat-feed entries (`intel.gs-infra.net/feed.txt`), with a
   per-exercise `{{ token }}`. inject_dispatch hands it to `greyspace.deliver_breadcrumbs`,
   which delivers it by backend (`CRUMB_CHANNELS`: mock records; vSphere runs
   `bin/gs crumb plant` on gs-core) and keeps the last 50 operations on the range's
   Greyspace row. No observable telemetry names a breadcrumb (Students search it).
10. **NPC traffic without GHOSTS.** A lightweight agent (app/greyspace/npc_agent.py, in
   the pinned python image) runs a profile's personas inside the stack: browse, resolve,
   mail, marked `GreyspaceNPC/1` in the User-Agent. GHOSTS needs a .NET client per
   endpoint plus its API server and Postgres; its clients in workstation images remain a
   later option.

## Wiring
`worker/tasks.py` calls the seam on one line each, kept apart from other hooks:

```python
# tasks.provision_range, after rendering, before VLANs are reserved:
template = greyspace.plan_host(range_id, backend, template)
# tasks.provision_range, after the range is marked ready (queues configure_range on HOSTED):
greyspace.after_provision(range_id, backend, template)
# tasks.destroy_range, after the range is marked destroyed:
greyspace.after_destroy(range_id)
```

All are no-ops for ranges without a block and never raise into the range task. On the
mock backend a range's Greyspace goes `configured` → `deployed` when it is provisioned
and back to `configured` when it is destroyed. On vSphere: `configuring` (gs-core in the
build) → `deployed` or `failed` (the configure stage), and back to `configured` on destroy.

Designer saves (`POST /ranges/{id}/topology`) keep the template's `greyspace:` key unless
the diagram carries one: the Range Designer's Internet/Cloud stencil holds the block
(`nodeData.greyspace`) and export writes it (range_topology.py); YAML import draws it back.

## Consequences
- The control plane can describe and validate a range's Greyspace without Docker; CI
  job `greyspace` runs the generated T0 stack for real (`greyspace/scripts/check-t0.sh`:
  DNS through the delegation chain, BGP learned between ISPs, HTTP by name, threat stub,
  overlay in front of a read-only corpus, HTTPS, mail, NTP, NPC traffic, breadcrumbs,
  WARC ingest). CI job `vsphere-sim` builds, wires and tears down gs-core on vcsim.
- **Not proven on real vSphere.** vcsim runs no VMware Tools and cannot do guest
  operations; the configure stage and breadcrumb delivery are unit-tested with a fake
  guest channel. The `greyspace-host` Packer image is written but has not been built.
- One Greyspace stack per Docker host (its network is the fixed public supernet).
- gs-core needs a template network for it (default `greyspace`) with a router on it; a
  template without one builds without gs-core and the block says why (`failed`, `plan`).
  Routing from the range is configured for VyOS routers; pfSense/OPNsense edges are not.
- Still open: search, threat-actor C2/payload projects, log shipping to OpenSearch,
  breadcrumb authoring in the scenario editor, GHOSTS clients in workstation images.
- Licensing: every site in a manifest carries `licence` and `source`; tiers built from
  third-party content are reviewed before they leave the machine that built them
  (`docs/greyspace-corpus.md`).
