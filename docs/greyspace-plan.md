# Greyspace — simulated internet module (plan + mockup only)

## Context

Ranges today are no-egress islands. Students and red cells need a believable "internet":
popular sites, search, webmail, video, root DNS, backbone routing, attacker infrastructure,
and NPC users making noise. SimSpace sells this as "Greyspace" (rehosted sites, core routers
+ BGP, root DNS, user traffic). TrueNorth has none of it — only a `tgen01` node in
`content/ranges/red-vs-blue/template.yaml:586`, todo catalogue rows `svc-emulators` /
`usersim`, and GHOSTS suggested in `docs/vm-build-sheet.md:164`.

Decisions taken with the user (2026-10-03):
- v1 scope: **all four** — core routing + root DNS, rehosted sites, threat-actor infra, NPC traffic.
- Integration: a **range template block** the Range Designer drops into any range,
  provisioned by the existing worker.
- **Corpus is 10 TB+** of scraped pages and video → lives once on NetApp
  (`docs/dell-netapp-mobile-kit-inventory.md:177`, ~600 TB, NFS), mounted **read-only**.
- Greyspace runtime is a **Docker stack** pointing at that data.
- Exercises hide **breadcrumbs** in some sites → per-range writable overlay, never the corpus.
- Deliverable this session: **this plan + a UI mockup**. No code.

## Architecture

```
            ┌──────────── Range N ────────────────────────────────────────────┐
 student/   │ corp VLANs ── rtr/fw ──► GREYSPACE VLAN (public-looking space)  │
 NPC VMs ───┤                         gs-core  (Docker host VM)               │
 (GHOSTS    │                          ├─ frr        BGP "ISPs", announces /16s│
  clients)  │                          ├─ dns-root   root + TLD + authoritative│
            │                          ├─ dns-resolver (ISP recursive)       │
            │                          ├─ ca         fake root CA (trusted)   │
            │                          ├─ webfarm    nginx vhosts by Host/SNI │
            │                          │    overlay(rw, per range) ▸ corpus(ro)│
            │                          ├─ video      HTTP range/HLS from corpus│
            │                          ├─ search     index over corpus         │
            │                          ├─ mail       postfix/dovecot/roundcube │
            │                          ├─ ghosts-api GHOSTS server (NPC brain) │
            │                          └─ log-ship   access logs → OpenSearch  │
            │                         gs-threat (Docker, per exercise)        │
            │                          C2 / phishing / malware-drop domains    │
            └──────────────────────────────┬──────────────────────────────────┘
                                           │ NFS ro
                         NetApp  /greyspace-corpus  (10 TB+, snapshots, versioned)
```

Key design points:
- **Corpus is shared and immutable.** One NetApp volume `greyspace-corpus`, versioned by
  snapshot (`corpus@2026.10`). Ranges pin a corpus version. Layout:
  `sites/<fqdn>/<path>` (WARC-extracted static), `video/<id>/…`, `manifest.json`
  (fqdn → ip, category, size, TLS). Ingest tooling (WARC/HTTrack → static tree + manifest)
  is a separate offline job, not in the range path.
- **IP realism.** Each rehosted fqdn gets a stable "public" IP from the manifest; `gs-core`
  holds them as secondary addresses (or macvlan per service) and FRR announces the covering
  prefixes. Blue teams see distinct IPs for google-alike vs. attacker C2.
- **Breadcrumbs = overlay.** nginx `try_files $overlay$uri $corpus$uri`. Overlay is a small
  per-range volume (local disk or MinIO-synced). Scenario YAML declares breadcrumbs; a new
  `greyspace_breadcrumb` injector plants them at exercise start (with per-team templated
  values), removes them on reset. Access logs → OpenSearch → existing `opensearch_query`
  validator scores "team fetched breadcrumb X".
- **Threat-actor infra** is a separate small compose project on `gs-threat`, started/stopped
  by scenario injectors, which also push DNS records to `dns-root` via its API (dynamic
  update / CoreDNS file reload).
- **NPC traffic:** GHOSTS server in the stack; GHOSTS clients baked into the `usersim` and
  workstation images; timelines selected per range (`npc_profile: office-day`).

## Delivery slices (each passes `bash scripts/dod.sh`)

0. **Prerequisites the module cannot work without** (from exploration — current gaps):
   - Worker has **no post-deploy step**: add a `configure_range` Celery stage after
     `provision_range` (`control-plane/worker/worker/tasks.py:148`) that runs per-role
     configurators (cloud-init user-data first; Ansible later). Greyspace needs this to
     start the compose stack and mount NFS.
   - **Single NIC** in `render.py:92` / Terraform `main.tf:67` and VLAN tags ignored by
     `proxmox_api`/`vsphere_api`: the router between corp and greyspace VLANs must be
     multi-homed. Add `nics[]` to vm_definitions (back-compat: one NIC from `vlan`).
   - **Injector params are dropped**: `get_injector` does `cls()` with no params
     (`scenario_engine/injectors/__init__.py:127`), `run_inject` never forwards `params`
     (`:143-156`), and `runner/run.py:120` calls `execute(params, ctx)` against a
     `BaseInjector.execute(context)` signature. Fix + regression test before slices 4–5,
     which depend on injectors receiving site/path/domain params.
   - **`RangeContext` has no network identity** (`__init__.py:33`): add `greyspace`
     (fqdn→ip map, resolver ip, overlay handle) so injectors and the currently-unrendered
     `{{ c2_domain }}` / `{{ attacker_ip }}` placeholders
     (`content/scenarios/apt-nation-state/scenario.yaml:62`,
     `content/inject-packs/phishing-campaign.yaml:24`) resolve to real Greyspace names.
   - Mock provisioner stays the dev path (this box is mock by design).
1. **Template block / composition.** Add `includes:` to template YAML
   (`- block: greyspace, version: 1, params: {...}`), expanded server-side before render
   and before `diagram_to_template` (`range_topology.py:568`) so the designer round-trips
   it as one locked "Greyspace" zone. Block lives in `content/blocks/greyspace/block.yaml`.
   Extend `scenario-engine/schemas/template.schema.json` for `includes` + typed block params
   (corpus version, site packs, npc profile, threat infra on/off, public prefixes).
   Tests: expansion, id namespacing, CIDR collision, designer round-trip
   (`tests/api/test_designer_topology.py` pattern).
2. **Greyspace stack, core.** `infra/greyspace/compose.yaml` + configs: frr, dns-root,
   resolver, ca, webfarm (nginx + overlay), log-ship. Generator that turns the corpus
   `manifest.json` + block params into zone files, nginx vhosts, FRR config, cert bundle
   (pure Python, unit-tested, no Docker required in CI). Golden image `svc-emulators`
   → rename/realise as `greyspace-host` (ubuntu-lts + docker + nfs-common) in Packer.
3. **Rich services.** video, search (index built offline per corpus version, shipped on
   the volume), mail/webmail. Site packs selectable per range (news, social, gov, dev, video).
4. **Breadcrumbs.** Scenario schema `greyspace.breadcrumbs[]` (`site, path, content|template,
   per_team, hint_style`); `greyspace_breadcrumb` injector in
   `scenario-engine/scenario_engine/injectors/` via the existing registry (`base.py`);
   reset removes overlay; validator recipe using `opensearch_query.py`. Tenant/range
   scoping on any new API.
5. **Threat-actor infra.** `infra/greyspace/threat/compose.yaml` (C2 redirector, phishing
   site, payload host); injectors to stand up/tear down + register DNS; ties into existing
   `c2 beacon` / `email_phish` injectors so they target greyspace fqdns, not raw IPs.
6. **NPC traffic.** GHOSTS server container; client install in `usersim` image; timeline
   library `content/greyspace/npc-profiles/*.json`; range param selects profile.
7. **UI.** Range Designer: the existing "Internet/Cloud" stencil
   (`range-designer.component.ts:925`, today drawing-only via `_NON_VM_TYPES` in
   `range_topology.py:402`) becomes the Greyspace block — dropping it emits the
   `includes: greyspace` entry instead of nothing — with a properties panel; a Greyspace page per range (status, sites,
   breadcrumbs planted/found, NPC activity). Scenario editor: breadcrumb authoring +
   preview. Built against the approved mockup.
8. **Corpus ops (offline).** Ingest CLI (WARC → static tree + manifest), corpus versioning
   via NetApp snapshot, size/dedupe report. Admin "Corpus" page read-only.

## Risks / open items to flag

- Slice 0 is real platform work (multi-NIC, post-deploy config) that other modules also
  need; it is the long pole, not Greyspace itself.
- Scraped content: licensing/redistribution and objectionable-content review before it
  goes onto a CAF kit; keep provenance in `manifest.json`.
- NFS read-only mount must not be reachable from student VLANs (only `gs-core` mounts it).
- Breadcrumb content must never be written to the shared corpus (overlay-only, enforced by
  ro mount).

## Mockup (this session, first step after approval)

Static HTML following the existing preview convention
(`control-plane/web/src/assets/previews/*.html`, shown via
`features/learning-preview/learning-preview.component.ts`; fictional data, no network) —
`assets/previews/greyspace.html`, also published as an Artifact for review.
Content: Range Designer with Greyspace zone + properties panel
(corpus version, site packs, prefixes, NPC profile, threat infra toggle), and the per-range
Greyspace page (services health, site list, breadcrumbs table with found/not-found per
team). Styled per the LMS-preview direction (less is more, role menus).

## Verification (for the build slices)

- `bash scripts/dod.sh` green each slice (per-worktree uv venv on Mac).
- Unit: block expansion, config generators (zones/vhosts/FRR) from a fixture manifest,
  breadcrumb injector plant/remove, schema validation.
- API: tenant isolation tests for any new endpoint (404 for foreign range).
- Mock provisioner end-to-end: create range with `includes: greyspace` → provision →
  configure stage emits compose + configs.
- On the demo env: `dig @root` resolves fake TLDs, `curl --resolve` hits a rehosted site
  with trusted cert, a planted breadcrumb is served and its fetch shows in OpenSearch.
