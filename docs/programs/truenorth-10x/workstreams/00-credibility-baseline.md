# Workstream 00 — credibility baseline

Status: ready. Suggested branch: `claude/10x-00-credibility-baseline`.

## Outcome

Remove known navigation duplication and establish a small automated baseline that makes later
program claims inspectable. This is deliberately small; it proves the branch/review loop
before schema-heavy work begins.

## Current evidence

`control-plane/web/src/app/app.routes.ts` defines the `exercises/templates` list and `new`
routes twice. Current CI at the inspected commit is green, so the test suite does not detect
this class of route duplication.

## First PR

1. Remove the duplicate route pair without changing route order, guards, lazy loading or deep
   links.
2. Add a route uniqueness test that walks nested route definitions and fails on duplicate
   canonical full paths, while allowing deliberate parameter variants only when documented.
3. Add a focused test proving `exercises/templates` and `exercises/templates/new` resolve to
   the existing destinations and retain their authorization metadata.
4. Update `codereview1.md` so R3-07 points to the merged evidence, not an open defect.

Likely files: `control-plane/web/src/app/app.routes.ts`, a nearby route spec under
`control-plane/web/src/app/`, and `codereview1.md`. Confirm the test location from current
conventions before editing.

## Acceptance

- Every canonical route path is unique under its parent.
- Existing deep links and guards are unchanged.
- Focused web tests and `scripts/dod.sh` pass at the branch head, or an environmental blocker
  is reported exactly.
- An independent `tn-change-review` finds no route/authorization regression.

## Non-goals

No navigation redesign, new readiness schema, new feature flag, dependency upgrade or broad
route reformatting.

## Recovery

The PR is code-only and reversible. Revert the route/test commit if route matching changes.

## Skills

Primary: `tn-change-review`. Verification: `tn-release-verification`. Use Taz only after the
qualification handshake; this is a good first disposable evaluation ticket.
