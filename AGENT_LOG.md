# Agent work log

## 2026-11-16 — Mac — last-mile plan, rc20 failed, rc21 published

- **Done:** audit #443 fixes merged (#482, #489-#496); rc20 tagged but its Release failed on a
  Trivy HIGH (musl, CVE-2026-107891, moodle image). Fixed in #497; opentelemetry CVE-2026-126360
  fixed in #498. **rc21 is published and signed** (run 43756198315, 16 assets). rc20 has no release.
- **Written:** `docs/last-mile-plan.md` (v1.1): the tiered plan to GA ~2026-11-24.
- **In progress:** Tier 0 (green main, land the 14 ready PRs), Tier 1 (clean-room doc blockers), rc22.
- **Next step:** diagnose `test_f30_companion_an_independent_request_is_not_starved_behind_the_lrs_proxy`
  (failed 2 of 3 CI runs, including main) and the `ad` lane; then land PRs small-first after merging main.
- **Blocked / unknown:** golden-image and template state at the work site is UNKNOWN (owner has not
  looked); the audit on site is the first on-site job. Upgrade drill baseline is rc19 (rc20 never
  published); rc19 to rc21 has no Alembic migration.
- **Owner decisions today:** credential rotation deferred until pre-prod or production; owner is
  the author (headers and git identity, item 1.10; name only, empty email, set for new commits);
  versioning for docs now, in-app content history after GA.

## 2026-09-26 — navigation proposal and model toolkit

- Reviewed navigation for learners, instructors, and administrators. Created an
  interactive proposal in the task visualization directory; application UI source
  was not changed.
- Added 15 canonical `SKILLS/tn-*/SKILL.md` workflows and 15 corresponding
  `.claude/agents/tn-*.md` roles. Retained the four existing agents and corrected
  stale baseline guidance in `test-engineer.md`.
- Added 30 relative discovery links under `.agents/skills` and `.claude/skills`,
  a root `AGENTS.md` entrypoint, toolkit index, and dated Taz model recommendation.
- Kept model selection inherited and existing host policy intact. No model weights
  downloaded, no Taz service changes, no application deployment.
- Validation: bundled `quick_validate.validate_skill` passed for all 15 skills;
  YAML, role/skill references, source pointers, documentation links, and discovery
  symlinks passed local checks. `git diff --check` passed.
- Prototype validation: Playwright with isolated Chrome passed workspace switching,
  instructor Prepare/Run/Review, learner continuation, administration tabs, and
  1024/736/390/320px overflow checks with no page errors. Desktop screenshot inspected.
- `bash scripts/dod.sh` blocked before tests: it reports Ruff unavailable, but
  `.venv/bin/python -m ruff --version` gives an executable-format error. `file
  .venv/bin/python` identifies a Linux ELF executable in this Mac checkout.
  Full application verification remains pending a compatible environment.
- Existing Compose changes, untracked nginx Dockerfile, and agent patches were
  present before this work and were left intact.

## 2026-11-04 — console relay test flake (Mac)

- `test_failed_dials_do_not_spend_a_grants_connections` (CI shard 4/5) was test isolation,
  not a product defect: failed dials never touch `dials` (only `service.record_dial` does).
  The relay's shielded `finally` clean-up keeps running on a worker thread after the
  TestClient has cancelled the app; under `isolated_db` its savepoint and the next socket's
  share one SQLite connection and release each other ("no such savepoint" →
  PendingRollbackError, then the next test's setup fails on `tenants.slug`).
- The serialization itself landed first in #430 (`_ONE_STATEMENT_AT_A_TIME`); PR #431,
  merged onto it, adds a deterministic regression test (holds the clean-up's savepoint open;
  fails without the lock) and puts the 4409 test's post-socket read under the lock.
- Next: merge the PR once CI is green.

## 2026-11-23 — installer renders Security Onion's install login (Mac)

- Site finding: env.production.j2 never wrote `VSPHERE_SO_INSTALL_USER` / `_PASSWORD`, so SOC
  ranges failed "no install login" on an installed host (hand-added on site; an upgrade drops them).
- Branch `fix/installer-so-install-login` off github/main (rc22): `tn_vsphere_so_install_user`
  (default `soadmin`, the API's name) / `tn_vsphere_so_install_password`
  (`vault_vsphere_so_install_password`, else `vault_template_linux_password`); preflight quote
  guard + vSphere warning; docs, CHANGELOG, tests. Adversarial review done (default changed from
  the Linux user to `soadmin` on its finding). Draft PR, not merged.
- Next: owner confirms the SO template's installer user is `soadmin` with the generic build
  password; on site, move the hand-added values into the inventory before the next upgrade.
## 2026-11-23 — #526 Windows Sysprep time zone (Mac, agent)

- Branch `fix/526-windows-customize-timezone` off github/main (rc22). Worker no longer
  hard-codes `timeZone=85`; `VSPHERE_WINDOWS_TIMEZONE` / `tn_vsphere_windows_timezone`
  (decimal Microsoft index, empty = none = index 0 on the wire) sets one. test-clone.sh
  takes `--tz N` (decimal) and passes hex to govc. Draft PR only.
- Open owner decision: the site's govc `-tz 85` was hex (index 133, invalid); decimal 85 was
  never tried. Site checks: `govc vm.customize -type Windows ... -tz 55` (= 85), and
  `tzutil /g` on a clone customized without a time zone (expect Dateline Standard Time?).
  If 85 works, set `tn_vsphere_windows_timezone: 85` or change the default.
- Update 2026-11-23: owner decided the default is 35 (Eastern Standard Time). Worker,
  installer (`tn_vsphere_windows_timezone: 35`), compose and test-clone.sh (`-tz 23`) now
  default to it. Acceptance on site: `scripts/lab/test-clone.sh srv2022` is accepted, and
  `tzutil /g` in the clone says "Eastern Standard Time".
## 2026-11-23 — depot mirrors and role media (Mac)

- Branch `fix/depot-mirrors-and-role-media` from github/main 5520dbe0 (rc22), draft PR.
- Part A (security/Parrot acng remaps) dropped here: PR #530 carries them; this PR merges after #530.
- B (designed tonight, NOT proven on site): Docker apt repo + Alpine via acng HTTPS-backend
  remaps; Nexus `quay-proxy` on 8084; greyspace-host pulls every pinned image by digest from
  the depot proxies (login as tn-build-read), re-tags on the containerd image store, checks
  each exact reference, seals daemon.json; Dockerfiles get APK_MIRROR; prefetch warms images
  (registry_prefetch.py), Docker Engine debs and apk packages. Tested locally with
  docker:29-dind, registry:2 pull-through and a real apt-cacher-ng with the rendered config.
- C: Microsoft role prerequisites downloaded to ~/.truenorth-work/role-installers (Mac only,
  not in the repo): MANIFEST.tsv + UPLOAD.md. MANUAL: mecm adk.zip, adkwinpe.zip
  (adksetup /layout on Windows), configmgr-prereqs.zip.
- Next (site, 08:30): re-run depot.yml (creates quay-proxy, widens tn-build-read), prefetch.yml,
  upload role installers per UPLOAD.md, build greyspace-host. The site capture shows
  `Offlinemode: 0` on the running depot: check it is meant to be online.

## 2026-11-23 — Mac — #527 Golden images: promote in place, sort and filter

- Branch `feat/527-golden-images-promote-ui` off github/main (rc22). The Golden images tab
  (Infrastructure page) already had builds + Promote since #240, but the builds opened in a
  section below the whole table and the reason was an optional field at its foot, so on a
  long list nothing visible happened. Now: builds open under the image row; Promote opens a
  dialog that requires a reason (≤500, kept in the audit entry); refusals shown under the
  image in plain words; status / promoted filters; default sort puts built-but-unpromoted
  images first with a "Needs promotion" flag. No API change.
- Tests: Karma specs (component + dialog); browser journey
  `test_admin_promotes_a_golden_image_build_with_a_reason` (real API, asserts the audit reason).
- Not verified: why `can_promote` hid the button at the work site, if it did. It is true only
  for an admin of PLATFORM_TENANT_ID (or a single-tenant install); check that on TN-MGMT01.
- Next: owner reviews the draft PR.

## 2026-11-23 — work-site golden-image kit fixes ported (Mac)

- Branch `fix/golden-image-kit-site-2026-11-23` off github/main (v1.2.0-rc22), one draft PR.
  Ports the day's TN-MGMT01 template-kit edits (captured in ~/.truenorth-work/
  site-capture-20261123/, reasons in its template-kit-server-edits.log) with tests:
  cloud-init re-enable/apt update/Kali mirror, Rocky marker, Debian use_mirror + cdrom
  source removal (cleanup.sh), srv2025 NVMe, srv2019 index 2, pfSense lsilogic-sas +
  pkgs.lock + SRV-following resolve, VyOS ethernet-node removal, build record 0644,
  "Host copies/<host>" folders, test-clone no -tz (#526) + IP slots, kali-gui and parrot
  variants, depot remaps (tnubusec, tnparrot), promote stopgap (#527), clear-unstamped
  (hooked into build-phase after a build) and friendly-tags, isos.conf filled.
- Not proven on site: pfSense controller, VyOS clones (still no Tools), parrot, srv2019.
- Not ported (separate decision): the site build dashboard (tn_build_dashboard.py).
- Follow-ups: drop the vsphere-iso.parrot source / build.sh ORDER entry; worker
  timeZone=85 (#526); Promote button (#527).
- Next: CI on the draft PR; adversarial-reviewer (provisioning change); then on site, rebuild
  pfsense and vyos with these files.
- Same day, after adversarial review of PR #530 (second commit): clear-unstamped-copies.sh
  fails closed on any govc read error and refuses (without --force) while
  TN_TEMPLATE_ALLOW_UNPROMOTED lets ranges use the copies; parrot no longer uses
  [trusted=yes]: Parrot's key (B711822346552E4D92DA02DF7A8286AF0E81EE4A, checked on
  parrotsec.org's key-verification page and deb.parrot.sh/parrot/misc/parrotsec.gpg) is
  committed in the script, signed-by only, nothing in trusted.gpg.d. Also: SRV targets limited
  to *.netgate.com/*.pfsense.org:443, explicit test-IP slots for every variant, promote
  script dry-run by default (--yes) and calls out an older promoted build, catalogue parrot
  version "7 (echo, rolling)". Not done: pfSense packagesite signature check.

## 2026-11-23 — work site: rc22 install and golden-image builds (Mac, on site)

- vCenter was down at arrival (esx01 off; owner restarted it). rc22 installed on TN-MGMT01 from rc13.
- Built golden images from a staged rc22 kit on TN-MGMT01; 13 built, tested on esx02-04 and promoted
  (record: docs/lab-checks/audit-2026-11-23.md). Promote UI is missing in rc22 (#527); Windows
  customization timeZone is rejected by vCenter 8.0.3 (#526).
- Server-side kit edits captured to the Mac at ~/.truenorth-work/site-capture-20261123 and being ported
  tonight as draft PRs (kit fixes, #526, #527, installer SO login, depot mirrors + role installers).
- Overnight on the host: srv2019, parrot, vyos -> pfsense, Windows roles, srv2022/win11 rebuilds.
  Dashboard: http://10.30.30.20:8099 (office network).
- Next (on site 2026-11-24 08:30): read overnight results; vyos console session; win10 watched boot;
  upload role installers to the depot; greyspace mirrors; promote what passed; set depot caches offline.

## 2026-11-24 to 11-26 — work site day 2, then the merge run from home (Mac)

- On site 11-24: srv2022, win11-24h2 and pfsense promoted on the owner's word; 16 of 27 images
  built and tested. Record: docs/lab-checks/audit-2026-11-23.md, "Follow-up on site, 2026-11-24".
- Open on site: parrot (fifth run, result UNKNOWN), srv2019 and win10-22h2 (VMware Tools never
  installs in the guest; cause not found), vyos, role installers, greyspace mirrors.
  ubuntu-webstack passed; promotion approved by the owner, not done (lab unreachable from home).
- Merged on the owner's word, each on full CI green against main, squashed: #532, #515, #533,
  #529, #516, #534, #518, #530, #519, #521, #523, #524, #525, #535. Every open site PR appended to this
  file, so the entries of #534, #529 and #530 were moved into this PR (it merges last).
- #530 also carries the 11-24 site fixes (pfSense installer hotkeys; Parrot http sources and
  apt retries). The Parrot part is not proven end to end, and was never run against that
  branch's pinned-key checks.
- #535: the Golden Images promote journey reopened the status filter while its panel was still
  closing; main's journeys lane failed on it three times. Two other intermittent journey
  failures on main are not diagnosed: the helpdesk assignee journey ("CSRF token missing or
  invalid", twice) and the Moodle cmi5 journey (browser closed, once).
- Faster range builds (plan of 2026-11-25): every range VM is a full clone today and nothing
  timed a build. #536 records seconds per build phase; linked clones, class-range reset by
  snapshot revert and pod masters follow, decided on the first timed builds.
- Next on site: one timed build each of medium-enterprise and soc-training (read `timings` in
  the range's provisioner output); the parrot5 result; promote ubuntu-webstack; srv2019/win10
  Tools diagnosis from the guest console; delete the leftover vyos test clones; the depot
  apt-cacher-ng watchdog.

## 2026-11-26 — CSRF fix and rc23 (Mac, from home)

- #538 merged on the owner's word (issue #537): the API issued a new CSRF cookie on every
  read, so a write whose header was read before a read landed got 403. That was the
  intermittent helpdesk journey failure. Now the cookie is issued once and the web app reads
  it at send time. The Moodle cmi5 "browser closed" failure is possibly the same; not shown.
- #539 opened: the CSRF cookie's `Secure` flag is probably not set behind nginx (inferred
  from code). First step is on the work host: read the Set-Cookie header through nginx.
- rc23 cut on the owner's word. Upgrade notes: docs/release-notes/v1.2.0-rc23.md. Before
  upgrading TN-MGMT01 from rc22: put the Security Onion install login (hand-added to
  `.env.production` on 2026-11-23) into the inventory or vault, or the installer replaces it.
- Next on site: install rc23, then the list in the entry above. `journeys` as a required
  check (#197) waits for about five green runs on main and is the owner's setting to change.


## 2026-11-27 — rc23 verified, air-gapped signature check fixed, next-visit page (Mac, from home)

- rc23 release verified from the published assets: signed manifest and SHA256SUMS (online and
  with a trusted-root file), 21 of 21 checksums, 6 of 6 image signatures and SBOM
  attestations, tags match digests, a changed manifest and a wrong identity are refused.
  Done with Homebrew cosign v3.1.3, not the installer's pinned binary.
- #542 merged on the owner's word (issue #541): the installer's air-gapped check no longer
  passes cosign's deprecated `--offline`. It is on main after the rc23 tag, so it ships with
  the next rc. Not done: the installer itself run with `tn_release_trusted_root` on a host
  with no route out.
- Main is green at 7b1c5fc7. No open PRs.
- Staged on the Mac for the visit: the rc23 install checkout and the verified release files
  with `trusted_root.json` (paths in docs/lab-checks/next-visit-rc23.md).
- Waiting on the owner: `approvals.yml` (draft, not in the repo); Qwen names still in the
  write-gate allowlist against the no-Chinese-models rule; `journeys` as a required check (#197).
- Next on site: docs/lab-checks/next-visit-rc23.md, top to bottom.

## 2026-11-28 — closing pass on the docs issues and Taz cards (Mac, from home)

- Re-checked every open docs issue (#387-#416) and Taz card (#433-#478) against main at
  48da4326. Closed 43 with evidence in a comment; left 26 open, each with a note on what
  remains.
- #551 (open, CI green): Taz coverage rounds A to F, 55 test files that were delivered on
  2026-11-08 and never applied. Closes #459-#461, #463-#465.
- #553 (open, CI green): guide gaps found by the re-check, and Taz corrections 437-r2 and
  439-r2. Closes #387, #389, #394, #403, #408, #414, #415.
- New issue #552: the site CA is not trusted in the API and worker containers, so vSphere
  TLS verification must stay off.
- Held for the owner: Taz 453-r2 (corrected UI literal counter) raises the baseline 701 to
  704 (#476). The four PRs opened by other sessions (#544, #545, #547, #548) are untouched.
- Kanzo was unreachable from the Mac all session: the board does not reflect any of this.
  The seven truenorth review cards (#88-#93, #134) are for issues already closed on GitHub.
- Next: owner's word on #551 and #553; then #410 (guide 01, 8 items), #479 (alerting).
