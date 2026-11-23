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
