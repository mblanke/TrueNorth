# Build log (Taz appends one line per PO)
| date | po_id | scenarios | critical events covered | gaps flagged |
|------|-------|-----------|-------------------------|--------------|
| 2026-07-17 | PO_007 | scenarios/PO_007 (range.tf, timeline.yaml, 5 validators, rubric.md, variant_B, xapi.json) | scanning (crit_scanning), exfiltration (crit_exfil), lateral_movement (crit_lateral) | nice_dcwf_task=TODO-map (human-gate per TODO.md §1); templates/packer/*.pkr.hcl generated for 20 enabled rows (review before build); packer not installed in gen env (HCL brace-balanced, not fmt-validated) |
