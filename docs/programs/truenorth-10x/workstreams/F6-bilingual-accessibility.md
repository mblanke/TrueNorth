# Foundation F6 — bilingual and accessibility acceptance

Status: proposed after F0. Suggested branch: `claude/foundation-f6-bilingual-a11y`.

## Closure outcome

Critical Student, instructor, commander, operator and administrator journeys are usable in
English and French and pass the approved WCAG/accessibility target with automated and human
evidence. Translation completeness is a release gate, not an optional polish count.

## First contract/inventory slice

1. Ratify language scope, official terminology authority, WCAG target and supported browser/
   assistive-technology matrix.
2. Inventory route titles, navigation, forms, validation/errors, status text, emails,
   reports/PDF/CSV, Keycloak theme, Moodle touchpoints and operational messages.
   Decide whether a global command palette is required for accessibility/operator efficiency;
   do not mislabel its absence as a defect if the approved journeys do not require it.
3. Introduce or validate one message catalogue and locale negotiation/persistence strategy;
   prohibit new user-facing literals outside approved exceptions.
4. Convert one complete Student journey and one instructor journey end to end, including
   backend error codes/messages and generated documents—not only the shell.
5. Add pseudo-localization, missing-key, layout-overflow, keyboard, focus, landmark/name and
   contrast gates to CI.

## Critical journey sequence

Student: sign in → enrol/prepare → join class/exercise → complete/submit → view result and
remediation. Instructor: schedule → approve/manage attendance → operate exercise → adjudicate
→ close out/AAR. Add commander readiness, range operation and administration after the shared
shell/contracts are stable.

## Acceptance

- A qualified translator/terminology owner approves French strings in scoped journeys.
- Locale survives refresh/deep links and does not alter authorization or record identity.
- Keyboard-only and named assistive-technology runs complete without traps/lost context.
- Automated axe/lint checks, zoom/reflow, focus order and error association pass.
- PDF/export/email/accessibility behavior is included or explicitly scoped with an owner.
- Missing translation fails the release gate for critical journeys.

## Recovery

Ship by route/role behind an explicit locale readiness manifest. English remains available if
a French slice is incomplete, but an incomplete critical journey cannot be labelled bilingual.

## Skills

Primary: `tn-angular-delivery`; add `tn-navigation-design`, `tn-api-contracts`,
`tn-release-verification` and `tn-docs-handoff`. Human linguistic and accessibility testing is
mandatory; models may find issues and generate fixtures but cannot self-certify acceptance.
