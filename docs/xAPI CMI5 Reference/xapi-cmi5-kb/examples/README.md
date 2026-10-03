# Examples

All JSON files are syntax-checked; `cmi5.xml` validates against the official cmi5 `CourseStructure.xsd` (Quartz).

| File | What it shows |
|---|---|
| `xapi-1.0.3-answered.json` | A complete 1.0.3-compatible statement: account IFI, bilingual language maps, `cmi.interaction` choice question, result, context activities. |
| `xapi-2.0-context-agents.json` | xAPI 2.0 statement using `contextAgents` (assessor) and `contextGroups` (team) for a range-exercise inject. Send only with `X-Experience-API-Version: 2.0.0` to a 2.0-capable LRS. |
| `xapi-voiding.json` | Voiding statement (verb `voided`, `StatementRef` object). |
| `cmi5.xml` | Course structure: course, objective, block, two AUs (one `CompletedAndPassed` with mastery score and launch parameters, one `Completed` in `OwnWindow`). |
| `cmi5-LMS.LaunchData.json` | The State document the LMS writes before launch (`stateId=LMS.LaunchData`). |
| `cmi5-learner-preferences.json` | Agent Profile document `cmi5LearnerPreferences`. |
| `cmi5-session-statements.json` | One full Normal-mode session for module 1: `launched` (LMS), `initialized`, `progressed` (cmi5-allowed), `completed`, `passed`, `terminated` (AU), then `satisfied` for the block and the course (LMS). |
| `cmi5-lms-waived-abandoned.json` | LMS-issued `waived` (module 2, reason "Tested Out", own session ID) and `abandoned` (a later session that never terminated). |

Scenario behind the cmi5 examples: module 2 was waived on 2026-09-22, so when module 1 is completed and passed on 2026-09-23 every AU in the block and course has met its moveOn, and the LMS issues `satisfied` for both. Runtime activity IDs (`https://lms.example.org/xapi/activities/...`) are LMS-generated and deliberately differ from the publisher IDs in `cmi5.xml`, which appear in `contextActivities.grouping`.

`xapi-profile-range-exercise.jsonld` is a minimal xAPI Profile 1.0 document (one custom verb, two activity types, one result extension with an inline JSON Schema, three statement templates, a primary `sequence` pattern with a nested `zeroOrMore`). `xapi-2.0-context-agents.json` is written to conform to its "Inject detected" template. Profiles 1.0 has no concept type for xAPI 2.0 `relevantTypes`, so the relevant-type IRIs used in that statement are documentation-only.
