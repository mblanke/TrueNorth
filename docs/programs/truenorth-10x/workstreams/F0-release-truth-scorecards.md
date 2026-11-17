# Foundation F0 — release truth and separate scorecards

Status: ready. Suggested branch: `claude/foundation-f0-release-truth`.

## Closure outcome

A reviewer can answer, from one maintained record: which commit and images were assessed,
which checks passed, which installed environment ran, what was skipped or unavailable, and
which limitations remain. Software-release, Unclassified IOC and COTE/PSOR readiness are
separate scorecards with non-compensating gates.

## Current evidence and problem

Current `main` is `eec5390677b58bfece641698ae84d57d2d941126`; v1.2.0-rc18 remains two
commits behind. `RESUME.md`, `docs/current-state.md`, release notes and module/plan documents
contain observations from several release candidates. Some release-note limitations have
since changed—for example, current source includes an optional Ansible Moodle stage—so an old
page must not generate new implementation work until reproduced.

## First evidence PR

1. Reconcile current `main`, tag, image digests, workflow runs, artifacts, installed-host
   version and repository status. Do not rename tag evidence as evidence for a later commit.
2. Create one dated release-evidence record with exact job names/results, permitted skips,
   artifact/SBOM/signature references, staging acceptance and explicit unavailable checks.
3. Make `RESUME.md`, `docs/current-state.md`, release notes and module matrix link to the
   record instead of copying counts.
4. Create separate tables for software release, Unclassified IOC and COTE/PSOR readiness.
   A green module cannot compensate for a missing accreditation, language, hardware or
   content-approval gate.
5. Reclassify every known limitation as `reproduced-current`, `resolved`, `unproven`,
   `accepted`, or `blocked`, with source/evidence and owner.

Likely files: `docs/review/`, release notes, `RESUME.md`, `docs/current-state.md`, module
matrix, COTE plan and a small evidence-generation script if existing artifacts cannot produce
the record deterministically.

## Acceptance

- Every pass has a commit, environment, command/run and artifact reference.
- Current main, rc18 and staging evidence remain distinguishable.
- No stale test count or “unreleased” page is presented as current proof.
- Every limitation has owner, state, closure condition and destination packet.
- The three scorecards cannot roll up to green when a non-compensating gate is open.
- Links and claim-check tooling pass; no private endpoint, credential or learner data appears.

## Recovery

This is evidence-only unless a generator is added. Preserve old records as superseded history;
do not rewrite them to look current. Revert references if a record is later found invalid.

## Skills

Primary: `tn-docs-handoff`; verification: `tn-release-verification` and
`tn-change-review`. Taz may reconcile source and artifacts under the worker protocol, but
Claude owns conclusions and unsupported-claim review.
