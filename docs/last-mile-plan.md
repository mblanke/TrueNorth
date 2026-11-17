# Last-mile plan: v1.2.0 GA (written 2026-11-16, GA target 2026-11-24)

Version: v1.1, 2026-11-16. Applies to: v1.2.0-rc21. Author: Marc-Andre Blanke.
Committed here so it is readable at the office; edits add a line to the Revision log.
Every item below has an owner (Claude, Taz, owner, site), an issue or PR number, and the
evidence that closes it. "Land #N" means the PR already exists; do not redo the work.

## Context
rc21 is published and signed (tag `cbc88e57`, Release run 43756198315 green, 16 assets). The
audit-443 fixes, the console relay (S1 to S4), classes, feeds, the capacity gate, the
readiness page and AD status sync are all in main. What is left is not features. It is:
(1) main's CI is red; (2) 14 ready PRs not merged; (3) the docs a stranger cannot follow;
(4) all the on-site proof: golden images, 8 certified templates, 97 courses, a real class, the
restore drill and the upgrade drill; (5) owner decisions that need an explicit accept or defer.

Sources read: every status doc (RESUME, current-state, release notes, module matrix, audit
triage, CHANGELOG, codereview1 rev 3, the 10x pack, NEXT-VISIT x2), the six guides plus
install and ops docs, all ADRs 0001-0020, the catalogue, 104 courses, 8 range templates, 100+
open issues, 23 open PRs, and the plan and memory files. Other files in `~/.claude/plans`
(FamilyHub, donation pin, Goldridge model, recipe app, birthdays) are personal projects with
no TrueNorth content; the three `yes-proud-thacker-agent-*` files are finished slices
(#299, screenshots, role-images).

## Facts that shape the plan
- **GA bar (owner, 2026-11-07, memory `ga-keep-97-courses`):** 97 non-held courses (37
  developmental + 60 upskilling) technically proven on real ranges, ~11-24, no scope cut.
  Today: 0 courses `technically_proven`; all 104 `draft`; the 8 range templates have no
  recorded Certified state in the repo (it is a database record). Do not re-propose a cut;
  flag slippage early.
- **Upgrade baseline is rc19, not rc20.** rc20 was never published. rc19 to rc21 is 71
  commits and **no Alembic migration** (checked). So the upgrade drill is an image bump, but it
  must start from the version actually installed at the work site (UNKNOWN: ask).
- **Site state is UNKNOWN.** Nothing in the repo records what happened at the 2026-11-09
  visit, which golden images built in the 3 weeks away, or which templates certified.
- **Main CI is red at `cbc88e57`.** `ad` (not required, red on every run) and test shard 2/5.
- Repo is public; GHCR package visibility not checked (needs `read:packages`).
- Guide 03 contradicts guide 02 on first sign-in (verified): 03:54-56 and 03:71 say that
  with AD you sign in as your AD account; 02 §7.1 and `install/README.md:461` say the
  break-glass `tnadmin@<vm>.local` signs in first and approves the AD admin.

## Tier 0: make main green and land what already exists (Mon-Tue)
| # | Item | Owner | Closes when |
|---|---|---|---|
| 0.1 | **Diagnose `test_f30_companion_an_independent_request_is_not_starved_behind_the_lrs_proxy`** (shard 2/5; failed on #497 run 1, and on main's own push run after #498; passed once). It is an audit oracle for event-loop starvation, so do not call it a flake until shown. Loop it under `TZ=UTC` in the CI shard layout; test the `/openapi.json` cold-start (2 s limit) theory and the opentelemetry 1.45.2 interaction. | Claude | 5 consecutive green shard-2 runs; cause written down |
| 0.2 | **Diagnose the `ad` lane**: red on every PR and main. If it is the installer's own CI, a red installer lane before a clean-room install is a GA risk, not noise. | Claude | green, or an ADR-style note saying why it is not required |
| 0.3 | **Land ready PRs, small first**, each after "merge main" (they predate the opentelemetry fix): #419 (duplicate routes, R3-07), #431 (relay savepoint test), #462 (audit443 marker), #418 (untrack `.venv`, closes #383, currently BLOCKED), #432 (10x pack + codereview1, untracked today), #458 (13 courses, unstable), #488, #485 (ADR tripwires), #486 (per-template lab checklists, 8 files), #483 (guides/runbooks claim-tested, 2.7k lines), #484 (coverage and ratchet baselines, BLOCKED: rebase), #420 (Students get exercise credit, #375: needs `adversarial-reviewer`), #355 (observer consoles var; default off per #363), #385, #365. Sample 5 claims in #483 before merge; check which of guides 01-06 it touches before any new guide edit. | Claude | merged, main CI green |
| 0.4 | **Defer, do not merge before GA:** support/wiki stack #417, #421, #423-#426 (not in ADR 0014 scope; churn risk); #92 coordinator and #51 plan (close as superseded). Owner confirms. | owner | in accept/defer list |

## Tier 1: before the colleague starts (clean-room blockers)
| # | Item | Owner | Closes when |
|---|---|---|---|
| 1.1 | **Fix the first-sign-in contradiction** in guide 03 (§1), `install/README.md:1223`; bootstrap = local `tnadmin@<vm>.local`, AD admin approved afterwards. Add a docs-claims test that greps all guides for the UPN rule. | Claude | guides agree; test exists |
| 1.2 | **Template path for a site with no test site** (the biggest clean-room gap). 02:364 and 06:419-422 take build passwords "from the test site's record"; clean-room needs templates the owner has already ported (06 §4). Split the clean-room into **Phase A** (mock provisioner, no vCenter, like the 11-09 rehearsal) and **Phase B** (real vCenter with ported templates). Define the undefined placeholders (06:495 `<range-host-1>`, datastore), the build-password source, and which templates Phase B needs. | Claude, owner | colleague can finish Phase A with no owner help; Phase B prerequisites listed |
| 1.3 | **#487 rehearsal findings in one PR:** installer `--check` fails on a fresh host (`roles/tn_release/tasks/main.yml:142`, `tn_keycloak/tasks/admin_token.yml:25`); say `-K` vs `--ask-vault-pass`; `tn_cosign_path` is the binary; break-glass UPN wording; HSTS "thisisunsafe"; when to set `tn_load_shipped_content`; ARC² defaults to the AI node when AI is on. | Claude | #487 closed |
| 1.4 | **Guide gaps:** SMTP stays **parked by the owner** (do not ask), so add a worked `site.yml` block, say plainly what does not work without it (invites, forfeit notices, alerts; Students sign in by hand); Moodle enablement (`tn_moodle_enabled`, port 8443, `runbooks/moodle.md`); remove the `TN-MGMT01` and "not re-read" banners (03:8-11, 03:58, 04:8-12, 05:6-9); Appendix C placeholders and the owner-policy sentence about model origin (02:887); Appendix A needs a step that produces `.env.production` offline. | Claude | agents re-read it; #410-#416 each answered or marked UNKNOWN |
| 1.5 | **Scrub lab-specific text** in `install/README.md` (TN-MGMT01, TN-DC01, `COTE/TrueNorth-Demo` PowerCLI path, VLANs 100-199 vs 2000-2999, `id_ed25519_lab`, staging `192.168.1.x`) and `install/lab/README.md`; mark `infra/vsphere/packer/README.md` as lab-specific; delete leftover `terraform-plan.yml`, `packer-build.yml`, `infra/proxmox/terraform`, and the Proxmox line at packer README:431. | Claude | grep for those strings returns nothing outside `lab/` |
| 1.6 | **rc21 release notes and upgrade notes** (`docs/release-notes/` only has v1.2.0; CHANGELOG rc list stops at v1.0.0). 06:77 points to a per-version file that must exist. State "no migration" for rc19 to rc21. | Claude | file exists, linked from 06 §2 |
| 1.7 | **Cut rc22** once Tier 0 and 1 are merged (target Wed 11-18). The guides clone the repo at the tag, so a colleague only sees these fixes in a new tag. Colleague installs rc22. Add guide PDFs and one-page checklists to the Release workflow (decision 11; rc21 assets have none, R3-05). | Claude | rc22 Release green with PDFs |
| 1.8 | **Credential rotation: DEFERRED by the owner (2026-11-16).** The work site is a test environment; nothing is rotated until it moves to pre-prod or production. The list (OpenSearch admin, `soadmin`/`COTEPass*`, Open WebUI router key, TN-MGMT01 recovery admin) becomes a **pre-prod promotion checklist item** in guide 06, not a clean-room blocker. The colleague installs on their own fresh VM with their own secrets, so they never need the test-site credentials. Do not raise it again before promotion. | owner | listed in the promotion checklist |
| 1.10 | **Authorship and timestamps (owner request).** Today git records the author as `taz-glm-5.3-flash` and rc tags are annotated by `taz`. Set the repo author identity to Marc-Andre Blanke so commits and tags carry the owner's name. Add a short header to source files: `Author: Marc-Andre Blanke`, `Created: <ISO-8601 UTC>`. Created comes from the file's first commit (`git log --diff-filter=A`), so it is true and never goes stale; the modification history stays in git (a hand-kept "modified" stamp would drift and lie). A script adds headers once, and a lint check requires one on every new source file. Exclusions: generated files (`openapi.json`, `schema.d.ts`), vendored code, lockfiles, YAML content, and anything a licence forbids altering. Headers add lines to the ratcheted god-files (`models.py`, `schemas.py`, `worker/tasks.py`), which would trip the MOSA line-count ratchet (ADR 0003): skip those three or lower their counts another way. It is one mechanical PR after the open PRs land, or every open PR conflicts. It is an authorship notice, not a copyright or licence statement; say if the work belongs to an organisation. | Claude | script, lint check and PR; `git log` shows the owner as author |
| 1.9 | **New `NEXT-VISIT-2026-11-16.md`** retargeted to rc22 (the 11-09 file pins rc19), recording the 11-09 outcome. | Claude | file exists |

| 1.11 | **Version control for the plan and docs (owner: both documents and in-app content).** Now: `docs/last-mile-plan.md` is committed with a version stamp (`v1, 2026-11-16`) and a dated **Revision log** at the bottom; every later edit adds a line (date, what changed, who) and bumps the version. Each guide (01-06, clean-room) gets a front-matter line `Version / Applies to release / Last reviewed`, and `test_docs_claims.py` fails if "Applies to" names a release older than the newest tag, which also catches the stale-rc problem in 2.14. Git history is the source of truth for the diffs; no second system. | Claude | plan and guides carry the stamp; the test exists |
| 1.12 | **In-app revision history for courses, range templates and exercise templates: scoped now, built post-GA.** First write down what exists (wiki has revisions; golden images have build ids; content-port bundles are signed; courses and templates are YAML files in git, not in-app records). Then specify: who changed what and when, diff view, rollback by restore-as-new-revision, and the audit trail into the existing audit log. It touches the curriculum spine, so it needs an ADR and `adversarial-reviewer`. Out of the GA path unless the owner says otherwise. | Claude | ADR drafted; issue filed with R1 label |

## Tier 2: the GA gate (by 11-24)
| # | Item | Owner | Closes when |
|---|---|---|---|
| 2.1 | **Golden-image audit, go/no-go.** One hour on site: for each of the 31 catalogue rows, registry state vs vCenter, per-host copies, test-clone customization, `template-queue-summary.log`. Known past failures: Windows `vm.customize`/Sysprep, pfSense keystroke halt, kali cloud-init/depot (all Packer or guest issues, not Ansible/Terraform). Tripwire: if fewer than 3 templates are Certified two days after the audit, the 11-24 date is at risk and the owner is told that day. Branches: (a) 3+ good: continue; (b) otherwise: rebuild queue per phase in the order soc-training, small, medium, large, cloud, red-team, greyspace, red-vs-blue. Depot #204 and VS ISOs #195 gate the later ones. | owner at site, Claude | dated audit record in the repo |
| 2.2 | **Certify the 8 templates and run each template's lab checklist** (#478 cards, #486 files); certification record is the evidence. | site, Taz drives, Claude validates | 8 Certified or a named list of which are not |
| 2.3 | **97 courses technically proven.** Definition (ADR 0014/0017, `content_port/proven.py`): range Certified + labs run + quiz keys checked. Tracker: drafted to validated to proven to SME-signed, per course. Flip `readiness: technically_proven` in the YAML only with a certification record cited. This is the long pole; report weekly. Do not conflate the YAML `readiness` enum with the computed "proven" in the port. | Taz/Claude | tracker shows 97, or slippage reported |
| 2.4 | **Install rc22 on the work host from the version actually installed, then run strict acceptance S and H** (module matrix is still "blocked: S/H not run" for every module). This is the upgrade drill (rc19 to rc22, no migration if rc22 adds none; check). | site, Claude | acceptance JSON in the repo |
| 2.5 | **Clean-room install by the colleague**, Phase A then Phase B, with each stumble filed as an issue and fixed. | colleague | no help requested, or every request became a doc fix |
| 2.6 | **Backup/restore drill on real data** to the second management host (06 §3): escrow-key custodians present, measure RTO against the 4 h target, record the compose project name prompt. | owner, Claude | drill record |
| 2.7 | **Real class of 12-15 Students**, roll call to close-out. The owner parked booking it; it is on the GA list, so it needs a date by ~11-20. | owner | class report |
| 2.8 | **Console on real ESXi 8 (#367)**: noVNC, view-only observer (#369), the guide-01 claims on ESXi certificates and the 8 h ceiling. Then verify and close #401, #364, #366, #368, #370, #441 against merged PRs (these look done). | site | issues closed with evidence |
| 2.9 | **Offline cosign verification against rc22** (`release.md:78`, never run; releases are signed now). | Claude | recorded |
| 2.10 | **Alerting (#479, R0):** no API-down or PostgreSQL-down alert, and every route goes to the null receiver. SMTP is parked, so route to a webhook (`vault_alertmanager_webhook_url`), run `alert-test.yml`. | Claude, owner (a credential) | alert fires in test |
| 2.11 | **Assessment integrity:** re-check audit-triage a1/a2 at rc21 (the `firewall_rule` validator credits any non-empty list; `process_killed`, `user_disabled`, `network_isolated` always fail). False credit is a correctness bug in a training product. | Claude | tests or a documented limit |
| 2.12 | **#443 ledger.** Seven fixes merged, 42 findings held. Map each finding to a PR, issue or accepted-risk so #443 can close. From #493, fix before GA: a deactivated Student's lab is not ended (capacity gate, security), and an admin class extension skips the capacity re-check. The rest of #493 (RangeOut guard, credential settle invariant, partial-teardown report) goes post-GA with issues. | Claude | ledger in the repo; #443 closed |
| 2.13 | **AD cut-over qualification** on TN-DC01 (Windows AD DS/AD CS never qualified; nested groups; role removal) and fix the doc contradiction on role MFA (`v1.2.0.md:63` says "not on main"; the matrix says landed). | site, Claude | M2 run recorded |
| 2.14 | **One rc22 evidence record** (R3-01 / F0): commit, image digests, every CI job, installed version, strict acceptance, permitted skips. Then refresh the stale claims: RESUME.md:14,20,30,34,37; current-state.md:3-5,37,46,55-56,64-65; v1.2.0.md:7,63,150-154 (empty staging record); module matrix (frozen at rc4); audit-triage (frozen at rc4, 30 of 30 "reproduced"); CHANGELOG:62; docs/AGENTS.md:62,66 (Terraform/Proxmox); agent-toolkit.md:3-4 and the `.venv` ELF note; ADR 0013 header "Code: none yet"; duplicate ADR number 0004. Taz cards #451, #466, #471 cover part; check overlap with #483 first. | Claude, Taz | `test_docs_claims.py` extended to pin the rc number |
| 2.15 | **Re-aim codereview1 gates 1 and 2** from rc18 run IDs to rc22; make `journeys` a required check (#197, R3-04; repo-admin action by the owner, I propose the change). | Claude, owner | branch protection lists `journeys` |
| 2.16 | **Tag v1.2.0** only after 2.4 to 2.8, 2.14 and 2.15 are evidenced. | Claude | signed release, no "pre-release" |

## Tier 3: owner accept/defer list (one sitting, my recommendation first)
| Item | Recommendation |
|---|---|
| i18n/French, team scoring and leaderboards, global shortcuts (`v1.2.0.md:21-25`) | Accept as documented GA limits |
| Synthetic injectors, no host sensors, no Student deliverable submission path | Accept as documented limits |
| R3-03: no KMS, HSM or TDE (plain PostgreSQL, Redis, MinIO, Keycloak) | Owner and security authority: accept with a written statement, or defer |
| Windows activation (KMS/MAK, ADR 0012 decision 3; not in code) | Defer until a production KMS exists, already in the ADR |
| ADR 0016 Exercise Studio (design only, #327-#340), ADR 0018/0019 pathways | Post-GA |
| 10x program D01-D07, F4/F6/F7, `program_plan_owner_accepted: false` | Post-GA |
| Support/wiki PR stack (#417, #421, #423-#426) | v1.2.1 |
| `codereview1` "freeze new content" vs the 11-07 "keep 97 courses" decision | Record that 11-07 supersedes the freeze |
| #363: ADR 0014 s19 says observer consoles on; owner decision is off by default | Edit the ADR to match; #355 adds the variable |
| #238 isolation: reference design is `vlan_trunk_no_l3`; `enforce` flips when the network team signs | No GA action unless they sign |
| Scenario-engine image is published but not deployed (N4) | Drop from the manifest or deploy: decide |

## Tier 4: hygiene (alongside, not blocking)
Update memory `audit443-fix-stack.md` (all merged, rc20 failed, rc21 published, the `TZ=UTC`
lesson); write the `AGENT_LOG.md` entry; update the Kanzo board (project slug from
`list_projects`); remove worktrees audit-fix-1..8 and gate-*; delete stale branches; keep
`/private/tmp` scripts out of the repo.

## Calendar (Mon 11-16 to Tue 11-24; owner on-site days set the pace)
- **Mon 11-16:** 0.1, 0.2, start 0.3 (small PRs); 1.1, 1.3 in progress.
- **Tue 11-17:** finish 0.3; 1.2, 1.4-1.6. (Credential rotation is deferred, 1.8.)
- **Wed 11-18:** cut rc22 (1.7); owner on site for the golden-image audit (2.1); tripwire clock starts.
- **Thu 11-19 to Fri 11-20:** colleague Phase A (2.5); 2.4 install and S/H on the work host; 2.8 console; class date fixed.
- **Sat-Sun:** template rebuild queue runs overnight if branch (b).
- **Mon 11-23:** restore drill (2.6), class (2.7) if booked, evidence record (2.14).
- **Tue 11-24:** go/no-go and v1.2.0 (2.16), or a dated slip announced with the named reasons.

## Verification
- Every merged PR: `TZ=UTC bash scripts/dod.sh` (per-worktree venv recipe in memory), CI green, `adversarial-reviewer` for anything touching auth, tenancy, scoring or provisioning.
- Docs: a fresh agent follows guides 01-06 cold and the colleague's stumbles become issues. Extend `test_docs_claims.py` so cross-guide contradictions (first sign-in UPN, rc number) fail the build.
- Evidence closes an item; a claim of "done" without it does not. Site-dependent items stay UNKNOWN until someone records them.

## Owner answers recorded (2026-11-16)
- Golden-image state: **not looked at yet**, so UNKNOWN. The audit (2.1) is the first on-site job.
- Credential rotation: deferred until pre-prod or production (1.8).
- Authorship: the owner is the author; names and creation timestamps in source files (1.10).
- Version control: documents now (1.11), in-app content history scoped for after GA (1.12).

## Revision log
- v1, 2026-11-16: first version, from all status docs, guides, ADRs, issues and PRs.
- v1.1, 2026-11-16: owner changes: credential rotation deferred (1.8); authorship headers (1.10); version control (1.11, 1.12).

---
