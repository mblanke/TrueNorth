---
name: tn-navigation-design
description: "Redesign TrueNorth menus, route hierarchy, and learner/instructor/admin flows; use when users struggle to find tasks or lose context."
---

# Navigation and user journeys

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `control-plane/web/src/app/app.component.ts`
- `control-plane/web/src/app/app.routes.ts`
- `control-plane/web/src/app/shared/hub-shell.component.ts`
- `control-plane/web/src/app/core/guards/auth.guard.ts`

## Workflow

1. Inventory visible menus, redirects, permission guards, page actions, and back links. Record a concrete confusing journey rather than judging labels in isolation.
2. Separate global destinations from sections of the selected exercise, course, or range. Preserve entity IDs, filters, and return location through navigation.
3. For the requested redesign, evaluate learner, instructor, and administrator experiences. A workspace switch is presentation for roles the account already has, never a role grant.
4. Give each destination a clear canonical home. Use role-appropriate navigation, stable active states, meaningful breadcrumbs, and explicit next actions.
5. Treat Prepare / Run / Review inside an exercise as a candidate design, not a shipped capability. Prototype the route map and adapt it to user feedback before broad route changes.
6. Preserve old deep links with deliberate redirects. Check that redirects do not activate the wrong sidebar item or discard query parameters.

## Verification

Walk each role from entry to task completion, plus browser back, direct URL, refresh, keyboard navigation, narrow layouts, empty data, and forbidden routes.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

Before/after journey map, canonical navigation tree, route migration mapping, and a preview or implemented slice matching the request.

## Boundary

A navigation mockup must label sample content and must not imply backend operations ran.

