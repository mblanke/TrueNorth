# TrueNorth Content Pack (CCoE / COTE)

Upload this repo to Taz (R7725, Claude Code) and run the loop in `RUN.md`. It generates
CFITES-compliant, Standards-validatable range content from the QSP crosswalk.

## What it produces (per PO)
1. vSphere Terraform range template (isolated, instant-clone)
2. Scenario inject timeline (ATT&CK-tagged) + noise floor (false positives)
3. OpenSearch validators — one must-pass per QSP critical event
4. Deliverable check + manual-ack stub (Standards scores summatives, not the AI)
5. P/F rubric + Variant B (mandated retest)
6. xAPI activity definition (MITE code + NICE/DCWF tag) for the LRS -> MITE roll-up

## Layout
- `CLAUDE.md`      guardrails (auto-loaded by Claude Code)
- `crosswalk.csv`  the contract: one row per PO across the 4 QSPs
- `vm_catalogue.csv` golden image library (build once, clone many)
- `schemas/`       YAML/validator schemas Taz lints against
- `templates/`     scaffolding templates Taz fills in
- `scenarios/PO_009_EXAMPLE/`  fully-worked DEFENSIVE reference — the pattern to copy
- `lms/`           xAPI -> LRS -> MITE mapping conventions
- `scripts/`       validate + scaffold helpers

## Human gates (non-negotiable)
Taz stops at validated files. An instructor confirms the range runs and is fair; Standards
owns the summative rubric and manual-ack. Provisioning (`terraform apply`) is a separate,
explicitly-authorized step — never autonomous.

## Before first run
- Fill `TODO-map` NICE/DCWF Task IDs from the NICE Framework Components v2.1.0 CSV (defensive
  roles) / DCWF (Red Analyst). Confirm blank durations against the QSP Annex D/E assessment plans.
- Rows with `status=needs_spec` require the PO detail pasted in first.
- Confirm Windows XP/7 licensing + EO justification before enabling those templates.

## QSP sources
`qsp_source/` contains the four Qualification Standards (+ Pte QS) behind `crosswalk.csv`. Taz reads them for exact PO/EO detail. CAF documents — keep on-box.

## Start-of-project checklist
See `TODO.md` for the full ordered task list (accreditation, crosswalk completion, images, generation, validation, LMS, provisioning).
