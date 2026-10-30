# ADR 0019: leadership and technical streams toward DP 3–5

**Status:** Accepted for testing (owner, 2026-10-30: "add a leadership and non leadership stream to the lms for dp3-5"; approach chosen: learning paths from existing courses)
**Date:** 2026-10-30

## Context

`docs/dp3-5-curriculum-proposal.md` argues for two routes past DP 2: a leadership route
(`progression`) and a technical route (`specialty`, depth without command). DP 3–5 are still
undefined, so there is no qualification to hang them on (`DP_LADDER` leaves those rungs
blank on purpose). The owner wants both streams visible in the LMS for each of DP 3, 4 and 5 now,
for testing.

## Decision

1. **Learning paths, not qualifications.** The spine and `qsp_paths.DP_PROGRESSION` are
   untouched. A stream is a placement of existing catalogue courses.
2. **Data:** an optional `streams` column in `content/catalogue/specialist_pathways.csv`
   (`leadership:3;technical:4`). `parse_programme` reads it, the course keeps it in
   `course_meta["streams"]`, and `catalogue_tags` adds `stream:leadership` / `stream:technical`.
   The column is optional, so the DP1/DP2 programme CSV is unchanged.
3. **Paths:** `generate_programme_paths` also builds `Stream — Leadership DP3` … `Stream — Technical DP5`
   (six, unpublished, "carries no qualification claim"), ordered by course code like the
   other programme paths. A course is on one stream per DP.
4. **Placement (a test fixture; reassign freely):**

   | | DP 3 | DP 4 | DP 5 |
   |---|---|---|---|
   | Leadership | C501, C531, C534, C535 | C502, C504, C532, C536, C558 | C505, C506, C551, C554, C560 |
   | Technical | C513, C519, C522, C525, C555 | C521, C526, C527, C528, C539, C541 | C511, C518, C530, C542, C553 |

5. Unlike the proposal (one route at DP 3), both streams exist at every period, as the owner asked.

## Consequences

- The streams are drawn from the 60 upskilling courses only; the proposal's own DP 3–5 courses
  (`PROP-DP3-01` …) are not authored and are not in the catalogue.
- When Standards defines DP 3–5, the qualifications (`PROP-DP4-T` etc.) can replace these
  paths; the `streams` column then goes away or becomes the course-to-qualification input.
