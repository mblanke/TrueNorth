# claude-glm5.md — Handoff brief for GLM‑5.2 (Taz)

**From:** Claude (planning + catalogue layer, off the sensitive path)
**To:** GLM‑5.2 running as Taz (Claude Code harness, GLM backend, on the R7725)
**Re:** Marching orders for CFITES content generation. Read this, then run the kickoff prompt at the bottom.

## Why you're getting this
We are splitting one problem set across a classification boundary. QSP‑derived criteria, DND
network detail, and malware samples must never leave this box — so **you** (on‑box) author every
scenario timeline, inject, validator, rubric, and offensive scaffold. I (off‑box) only produced the
non‑sensitive catalogue + network spec you consume as inputs. Your rules in `CLAUDE.md` still govern
everything; this brief does not override them — where they conflict, `CLAUDE.md` wins.

## Estate note (confirmed)
Target hypervisor is **ESXi / vSphere only** (procured vCenter estate). Terraform uses the vSphere
provider; golden images build to the vCenter Content Library; ISOs live on a vSphere datastore. The
Proxmox and Hyper‑V Packer files in `infra/` are **non‑authoritative** references only — do not target them.

## What I've prepared for you to consume (read‑only inputs, under `content/catalogue/`)
- `vm_iso_catalogue.csv` — reconciled master image list. The `template_id`s match `vm_catalogue.csv`.
  When a scenario needs a host, reference a `template_id` from here — never invent a new golden image.
  It also carries `used_by_ranges` / `used_by_pos` back‑references and a `packer_status` column.
- `range_network_spec.md` — the VLAN/CIDR/firewall conventions (isolated, deny‑egress, per‑PO vSphere
  `distributed_port_group`). Your `range.tf` files must conform to it.
- `iso_acquisition_list.md` — which ISOs exist vs. are pending (informational; you reference images,
  you do not build them).

### Flags you must respect (from the reconciliation)
- `ubuntu-lts` version mismatch: catalogue says 24.04, the shipped Packer files say 22.04.3 — do not
  guess; use whichever a human pins.
- `GAP-vyos`: the `red-vs-blue` range references a `vyos-1.4` router that has **no** golden image.
  Do not fabricate one — flag it if a scenario needs it.
- `c2-server`, `win-xp-sp3` are `enabled=no` — never reference them in a generated scenario.

## Your work, in order (each item is one commit, then STOP for review)
1. **Golden‑image Packer files** — `RUN.md` "Golden images" prompt: for each `vm_catalogue.csv` row
   `enabled=yes`, emit `templates/packer/<template_id>.pkr.hcl` from the template, **vSphere builder**,
   baking the sensor per the `notes` column. Do NOT run `packer build`.
2. **Pilot scenario PO_007** — *already scaffolded* (`scenarios/PO_007/`). Re‑verify it against this
   brief: golden_templates all exist + `enabled=yes` in `vm_iso_catalogue.csv`; one must‑pass validator
   per critical event (scanning; exfiltration; lateral_movement); `terraform validate` + `scripts/validate.sh`
   pass. Report the critical‑event→validator coverage and any gaps. Fix only if it fails; else move on.
3. **Batch** (only after PO_007 is reviewed) — `crosswalk.csv` rows `status=todo, tier in (core,gate)`:
   PO_006, PO_008, PO_010, TEMP67 PO_008, ALRA PO_001. Read + echo each row first; if any field is
   `TODO`, STOP and ask. One commit per PO; append `docs/BUILD_LOG.md`.

## Do NOT (your hard gates — unchanged)
- No `terraform apply` / `forge.py provision`. Files only, for human review.
- `status=offensive_author` rows (Red Analyst PO_001‑004): scaffold range+validators+rubric only,
  leave `AUTHOR-REQUIRED`. `status=cots_gate` rows: draft the gate scenario only.
- Never auto‑score a summative. Never move QSP/DND/malware content off this box or call a cloud API.

## Kickoff prompt (paste this into your session to begin)
> Read `claude-glm5.md`, then `CLAUDE.md`, `RUN.md`, and `content/catalogue/`. If `templates/packer/`
> is empty, start with task 1 (golden‑image vSphere Packer files). Otherwise verify the existing
> `scenarios/PO_007/` scaffold against the catalogue (task 2). Echo the relevant crosswalk/vm_catalogue
> rows back before generating. Do one unit of work, commit, and STOP with a critical‑event→validator
> coverage report and any gaps flagged. Do not provision.
