# Agent work log

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
