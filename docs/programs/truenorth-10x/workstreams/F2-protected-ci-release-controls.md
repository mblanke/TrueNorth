# Foundation F2 — protected CI and release controls

Status: proposed after F0. Suggested branch: `claude/foundation-f2-protected-ci`.

## Closure outcome

The evidence required for release cannot be bypassed by merging to `main`; a deliberately
failing test PR proves each required check blocks merge, and release evidence is generated
from actual run metadata instead of copied counts.

## First audit slice

1. Inventory current workflow jobs, aggregate checks, branch protection/rulesets and release
   workflow triggers through GitHub read APIs.
2. Map required outcomes to stable check names: aggregate Python, PostgreSQL/integration,
   real-auth journeys, Angular, contracts/MOSA, installer lint and supply-chain gates.
3. Remove accidental ambiguity between matrix children and aggregate checks. Preserve shard
   completeness and PostgreSQL-skip guards.
4. Add or repair a release-evidence generator that records run/commit, job conclusions,
   artifacts, allowed skips, image digests and signature/SBOM results.
5. With explicit repository-owner authority, update the GitHub ruleset and open a disposable
   failing PR that demonstrates each required check blocks merge; then close it without merging.

## Acceptance

- Read-only audit distinguishes configured protection from a green optional job.
- All required check names are produced on relevant PRs without path-filter deadlocks.
- Cancellation, skipped matrix cells and missing aggregate jobs cannot report success.
- Failing proof PR is visibly unmergeable; repaired proof PR becomes mergeable.
- Release workflow consumes an exact green commit and emits verifiable evidence.
- Ruleset/export evidence is retained without exposing tokens or repository secrets.

## External-action boundary

Code changes and read-only audit may proceed normally. Updating GitHub protection and opening
the proof PR are external state changes requiring explicit owner authorization and suitable
repository permissions. If unavailable, mark the gate `blocked-by-repository-admin`; do not
claim it is configured.

## Recovery

Export the existing ruleset first. Revert rule changes if required workflows deadlock. The
proof PR contains a deliberate, isolated failure and is never merged.

## Skills

Primary: `tn-release-verification`; review with `tn-change-review`. GitHub MCP or `gh` may
collect/modify protection only within explicit authority. Taz may inspect workflows and build
the evidence generator but cannot change repository policy or merge.
