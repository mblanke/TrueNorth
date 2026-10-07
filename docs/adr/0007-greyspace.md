# ADR 0007 — Greyspace: a simulated internet attached to a range

- Status: accepted (stage-4 scope; the vSphere gs-core VM follows)
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
   `control-plane/worker/worker/greyspace.py` `DEPLOYERS`. `mock` records the block as
   `deployed`; every other backend reports `pending_infrastructure` until it has a
   deployer. The vSphere deployer (one `gs-core` VM from a `greyspace-host` golden image,
   NFS-mounted corpus, the rendered stack started with Compose) is after stage 4; it needs
   the post-deploy configure stage and multi-NIC routers (plan, slice 0).

## Wiring
`worker/tasks.py` calls the seam on one line each, kept apart from other hooks:

```python
# tasks.provision_range, after the range is marked ready:
greyspace.after_provision(range_id, backend, template)
# tasks.destroy_range, after the range is marked destroyed:
greyspace.after_destroy(range_id)
```

Both are no-ops for ranges without a block and never raise into the range task. On the
mock backend a range's Greyspace goes `configured` → `deployed` when it is provisioned
and back to `configured` when it is destroyed.

Designer saves (`POST /ranges/{id}/topology`) keep the template's `greyspace:` key, as
they keep every key the designer does not own.

## Consequences
- The control plane can describe and validate a range's Greyspace without Docker; CI
  job `greyspace` runs the generated T0 stack for real (`greyspace/scripts/check-t0.sh`:
  DNS through the delegation chain, BGP learned between ISPs, HTTP by name, threat stub,
  overlay in front of a read-only corpus).
- One Greyspace stack per Docker host (its network is the fixed public supernet).
- Not in this slice: the root CA and HTTPS, video/search/mail services, NPC traffic
  (GHOSTS), breadcrumb injectors, log shipping to OpenSearch, and the vSphere gs-core VM.
  `npc_profile` and `trust_ca` are recorded only.
- Licensing: every site in a manifest carries `licence` and `source`; tiers built from
  third-party content are reviewed before they leave the machine that built them
  (`docs/greyspace-corpus.md`).
