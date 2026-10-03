---
name: tn-angular-delivery
description: "Implement TrueNorth Angular components, forms, and UI state using the existing Material stack; use for frontend behavior and accessibility changes."
---

# Angular feature delivery

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `control-plane/web/package.json`
- `control-plane/web/angular.json`
- `control-plane/web/src/app/core/services/api.service.ts`
- `control-plane/web/src/app/shared`

## Workflow

1. Read the current component and direct callers before editing. Preserve standalone imports, lazy routes, theme tokens, and the existing Angular/Material versions.
2. Separate container orchestration from reusable display components when that makes the current change easier to verify; avoid unrelated framework replacement.
3. Represent loading, empty, error, stale, and success states explicitly. Never turn failed requests into plausible zero values.
4. Use server aggregates for totals, not a paginated list's length. Keep destructive actions labeled and asynchronous operations visible until their outcome is known.
5. Provide accessible names, form labels, error associations, focus handling, and reduced-motion support. Keep actions available on keyboard and touch.
6. Use the existing typed service boundary and guard behavior. Avoid putting privileged work behind UI hiding alone.

## Verification

Run targeted component checks and production build/lint when available. Inspect relevant states in a browser; distinguish mocked API verification from a real backend journey.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

A focused frontend change with route/state impact, browser evidence, executed checks, and any remaining visual or runtime limits.

## Boundary

Do not fabricate successful provisioning, scoring, or permission changes in UI state.

