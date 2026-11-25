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
