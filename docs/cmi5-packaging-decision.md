# cmi5: packaging only until stage 5

> **Superseded (2026-10-09).** TrueNorth now runs cmi5: an AU runtime in the SPA for every
> module of an accepted release, a served `cmi5.xml` per release, and its own LMS side
> (launch, fetch, the AU's checked xAPI endpoint, moveOn and `satisfied`). See
> [`docs/cmi5.md`](cmi5.md). The record below is kept as the decision of its day.

**Decision (2026-10-07, stage-4 programme):** cmi5 in TrueNorth stays a **packaging
format**. ARC² builds a cmi5 package per course (`build/arc2/<slug>/07-bundle/cmi5/`:
`cmi5.xml` plus the AU files) so a course can be handed to an external cmi5 LMS such as
PCTE. TrueNorth itself does **not** run cmi5 at stage 4:

- no `GET /courses/{id}/cmi5.xml` endpoint, no cmi5 launch, no AU runtime in the SPA,
  no `launched` / `satisfied` / `abandoned` / `waived` handling, no cmi5 session state;
- no Moodle cmi5 plugin in the farm image (`mod_cmi5` is noted in `docs/moodle-primer.md`
  as the candidate for later, untested on Moodle 5.2);
- results inside TrueNorth's own delivery keep flowing as they do now: Moodle over LTI 1.3
  with AGS grade pass-back, and TrueNorth's xAPI statements (`app/xapi.py`) to the LRS.
  Those statements are cmi5-*shaped* where that was free, not cmi5-conformant.

**Why:** the stage-4 bar is "every LMS path tested and reachable". The delivery path that
is built and tested end to end is TrueNorth → Moodle (release publishing, SSO, LTI labs,
AGS). A cmi5 runtime would be a second delivery path with its own session model,
conformance suite and LMS plugin, for no Student-facing gain in this cut.

**What this rules out for stage 4:** "Phase 2: cmi5" in `docs/moodle-integration.md`
(2a–2e) is design, not scope. Work that assumes TrueNorth launches or tracks cmi5 AUs
waits for stage 5.

**Stage 5 entry questions:** which LMS launches the AUs (PCTE, Moodle with `mod_cmi5`, or
both); whether TrueNorth range labs become AUs or stay LTI links inside a cmi5 course;
and the conformance target (ADL cmi5 test suite) and LRS of record.
