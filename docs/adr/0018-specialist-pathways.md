# ADR 0018: specialist pathways toward DP 3–5

**Status:** Accepted for testing (owner, 2026-10-30: "go with option 1 ... a specialist path which leads to the non existent DP3-5 ... this is for testing purposes and can be further developed later")
**Date:** 2026-10-30

## Context

The 60 upskilling courses (C501–C560, drafted by TAZ) are outside the DP1/DP2 programme, but
the importer only attaches content to a catalogue course (`import_course_content` refuses
a course with no catalogue row), so they could not be loaded. DP 3–5 are not defined yet
(`docs/dp3-5-curriculum-proposal.md`), and the owner wants the upskilling courses to be the
first material of specialist routes that will lead there.

## Decision

1. **A second catalogue, `content/catalogue/specialist_pathways.csv`**, in the programme
   catalogue's columns, imported by the same `import_programme` (the loader runs it after the
   programme CSV). No new tables, no importer change.
2. **One pathway per row group.** `programme = specialist-<slug>`, `term_code = SPEC-<SLUG>`,
   and `dp_order` is the developmental period the pathway **leads to** (3, 4 or 5), not one it is
   taught in. `generate_programme_paths` therefore makes one path per pathway, named
   `Programme — specialist-<slug> DP<n>`. Terms have no dates.
3. **Unbound and unpublished.** `qsp_code` is `QSP-TODO` (no qualification claim) and every
   row is `unsourced` / `proposed`, so every course stays unpublished until a source exists.
4. **Eleven pathways, as a test fixture** (grouping follows each course family; reassign freely):

   | Leads to | Pathway | Courses |
   |---|---|---|
   | DP 3 | Digital Forensics | C525, C526, C529 |
   | DP 3 | Detection & SOC Operations | C519–C524 |
   | DP 3 | Network Defence | C513–C518 |
   | DP 3 | Incident Response | C531–C536 |
   | DP 3 | Threat Intelligence & OSINT | C555–C560 |
   | DP 4 | Malware Analysis | C527, C528, C530 |
   | DP 4 | Offensive Operations | C537–C542 |
   | DP 4 | Identity Security | C507–C512 |
   | DP 4 | Cloud Security | C543–C548 |
   | DP 5 | OT & ICS Security | C549–C554 |
   | DP 5 | Governance, Risk & Assurance | C501–C506 |

   Planned, no courses yet: **Reverse Engineering** (follows Malware Analysis, toward DP 4) and
   a dedicated **OSINT collection** pathway (toward DP 3). Add their rows when the courses exist.
5. **Relation to the DP 3–5 proposal.** Depth pathways are the material for the proposal's
   technical stream (`PROP-DP4-T`, `PROP-DP5-T`), not new capability ladders; that proposal's
   rule that breadth stays mandatory for every operator is unchanged. Nothing here defines
   DP 3–5 or any qualification.

## Consequences

- C500–C599 are the upskilling range everywhere (QA, contract tests, parity test).
- ARC² QA's catalogue-identity check accepts a code from either catalogue.
- The career map shows these courses in the DP 3–5 columns once those columns exist; until
  then they appear only through their programme paths.
- Tests that counted "the catalogue" now count the DP1/DP2 programme plus the specialist set.
