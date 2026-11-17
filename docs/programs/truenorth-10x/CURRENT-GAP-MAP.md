# Current-gap closure map

This is the routing map, not evidence that a gap is closed. F0 must revalidate every row
against current source and supported environments. States are `reproduced-current`, `resolved`,
`unproven`, `accepted` or `blocked`; only evidence changes a state.

## Current code-review findings

| Finding | Closure packets | Non-compensating evidence |
|---|---|---|
| R3-01 fragmented/stale release truth | F0 | One current commit/environment evidence record; references do not copy stale counts. |
| R3-02 real infrastructure proof missing | F3, F7, 05 | Named real vCenter/AD/Moodle runs, representative exercise evidence and supervised physical journey. |
| R3-03 incomplete encryption/key boundary | F4 | Approved control allocation, implemented verification, rotation/restore and authority sign-off. |
| R3-04 optional rather than required CI evidence | F2 | Ruleset audit plus deliberately failing PR blocked by every required check. |
| R3-05 clean-room documentation incomplete | F1 | Fresh operator install/upgrade/restore/alert run with no undocumented decisions. |
| R3-06 module green conflated with programme ready | F0 | Separate release, Unclassified IOC and COTE/PSOR scorecards with blocking gates. |
| R3-07 duplicate exercise-template routes | 00 | Duplicate removed; normalized route-uniqueness/deep-link test passes. |

## Published v1.2 limitation routing

| Published limitation or unproven claim | Closure packets | Required outcome |
|---|---|---|
| Injects are synthetic; no host sensor installation | F3, F7, 02, 04 | Keep simulation labelled; implement only approved provider/effect slices and prove them on named hosts. |
| No team exercise scoring/standings/leaderboards | 02 | Accepted team contract and one team exercise projection, or explicitly accepted product non-goal. |
| No global shortcuts/command palette | F6 | Owner decision against critical journeys; implement only if required, otherwise retain as non-goal. |
| English-only UI and unproven accessibility | F6 | Human-approved French and accessibility evidence for all critical role journeys. |
| Partial encryption; no Vault/KMS/HSM/TDE/disk/SSE/internal TLS | F4 | Approved threat/control model plus tested implementation and recovery for the scoped deployment. |
| Alert delivery unproven | F1 | Real approved-channel message and failed-delivery visibility on the clean installation. |
| ARC² backup quiescence limitation | F1, F5 | Reproduced current behavior; safe quiescence/manifest/refusal behavior implemented and restore-tested. |
| vSphere simulator-only and abandon/manual cleanup limits | F3, F5 | Real lifecycle/failure cleanup evidence; remaining manual cleanup explicit, audited and rehearsed. |
| Windows AD DS/AD CS not qualified | F3 | External-mode Windows journeys on named versions/configuration. |
| ARC² output and C105 are test/draft content | F7 | Fixture content withdrawn from learner release; exact approved versions receive human sign-off. |
| cmi5 activity tied to one release | F5 | Reproduce and either implement version-safe behavior or document/test the accepted invariant. |
| LTI launch enrolment behavior | F5 | Accepted enrolment contract with authorization/tenant tests and truthful Student UX. |
| LTI handoff WebSockets unsupported | F5 | Secure expiring handoff support with replay/tenant tests, or documented tested fallback. |
| Pre-v1.0 telemetry-index search limitation | F5 | Upgrade fixture proves compatible read/migration or explicit supported retirement procedure. |
| Multi-tenant `PLATFORM_TENANT_ID` requirement | F5, F4 | Explicit platform-authority model and safe migration/configuration acceptance. |
| LTI requires HTTPS | F1, F4, F5 | Installer preflight and supported TLS configuration; insecure deployment fails closed. |
| Moodle installed-host proof | F1, F3 | Current Ansible Moodle stage plus SSO/publish/Student/result/backup journey on a clean host. |
| Real telemetry/noise/scenario endurance unproven | F3, F7, 07 | Named workload/topology, sustained run, recovery and resource-cleanup evidence. |
| Content accreditation and SME acceptance missing | F7 | Versioned technical/instructional/bilingual/accessibility approval and rehearsal evidence. |
| 70,000-vCPU ambition unproven | 07, F3, F4 | Measured cell limits, federated admission/isolation/recovery and no extrapolation beyond evidence. |

## Closure rule

F0 owns this map. When a row closes, it records the evidence link, commit/environment,
reviewer/authority and date in the current release record. Removing a limitation from release
notes without that evidence is not closure. Adding a future feature is not required when the
owner explicitly accepts a tested non-goal, but the decision and user-visible behavior must be
clear.
