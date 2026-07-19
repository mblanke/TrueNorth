# Run instructions for Taz

## One PO (recommended first run — use the example as reference)
> Read `scenarios/PO_009_EXAMPLE/` to learn the pattern. Then read row `PO_007` from
> `crosswalk.csv` and `vm_catalogue.csv`. Echo the PO_007 criteria back. Scaffold
> `scenarios/PO_007/` following the example: vSphere Terraform range referencing only existing
> golden templates, an ATT&CK-tagged timeline with a noise floor, one OpenSearch validator per
> critical event (scanning, exfiltration, lateral_movement — all must-pass), a deliverable check,
> a manual_ack stub, a P/F rubric, Variant B, and xapi.json. Run `terraform validate` and lint
> the timeline. Report critical-event -> validator coverage and flag gaps. Commit and STOP.

## Batch (after the first few are reviewed and the pattern is trusted)
> For each row in `crosswalk.csv` where `status=todo` and `tier` in (core,gate): perform the
> per-PO workflow in CLAUDE.md, one commit per PO. Skip rows where `status` in
> (offensive_author, needs_spec, cots_gate) — those need human authoring first. After each PO,
> append a line to `docs/BUILD_LOG.md`: PO_ID, scenarios created, critical events covered, gaps.

## Golden images (run once, before scenarios)
> For each row in `vm_catalogue.csv` where `enabled=yes`: generate a Packer build file under
> `templates/packer/<template_id>.pkr.hcl` from `templates/packer.pkr.hcl.tmpl`, baking in the
> sensor/collector per the notes column. Do NOT run `packer build` — leave files for review.

## NEVER
- Never `terraform apply` / `forge.py provision`.
- Never generate offensive payloads/exploits/C2 profiles (status=offensive_author -> scaffold only).
- Never call a cloud API or move data off-box.
