---
name: tn-curriculum-traceability
description: "Maintain QSP, PO/EO, course, enrollment, and competency mappings; use for learner progress and source-grounded training records."
---

# Curriculum and qualification traceability

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `control-plane/api/app/qsp_progress.py`
- `control-plane/api/app/qsp_ingest.py`
- `control-plane/api/app/enrollment.py`
- `content/catalogue`
- `truenorth-content-pack/truenorth-content/crosswalk.csv`
- `tests/api/test_qsp_crosswalk_integrity.py`

## Workflow

1. Identify the authoritative source, edition, and exact identifier before changing qualification labels, objective wording, NICE/DCWF codes, or claimed completion.
2. Trace PerformanceObjective through CourseModule, Course, Enrollment, and ModuleProgress for progress changes. Separate missing mapping from a learner who has not started.
3. Preserve documented unknowns until evidence resolves them. Never fill an authoritative-looking code or rank with a plausible guess.
4. Separate formative recommendations, assessed objectives, and formally granted qualifications. Automation must not silently promote one into another.
5. Keep learner-owned records and instructor access tenant-scoped. Shared catalogue definitions are distinct from personal achievement records.
6. Batch hierarchy queries and preserve query-count tests where present.

## Verification

Test empty enrollment, incomplete mappings, partial progress, alternate tenants, and source-reference integrity; cite source location/version for changed authority data.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

A traceable mapping or progress fix with source references, unresolved gaps, and regression results.

## Boundary

Keep restricted source documents local unless the user has authorized their transfer; do not imply authority to grant qualifications.

