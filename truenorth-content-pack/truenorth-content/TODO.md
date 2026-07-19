# TODO — TrueNorth Content Pack

Ordered. [H]=human/Standards, [T]=Taz can do, [P]=procurement/policy, [ACC]=accreditation.
Nothing provisions VMs or grants a qualification without an explicit human gate.

## 0. Before anything runs
- [ ] [ACC] Confirm ATO covers an agent with shell access provisioning ranges + touching malware
          samples on this host. Guardrails in CLAUDE.md support it but do not replace the ATO.
- [ ] [P]   Handling review: QSPs in qsp_source/ are CAF docs — confirm they may sit on Taz. Keep on-box.
- [ ] [H]   Confirm Taz backend/build (GLM version) so grounding instructions are tuned.
- [ ] [H]   Confirm the real STEADY-STATE concurrent operator count (150 / 250 / 350) — drives hardware,
          not the 1,200 FVEY surge. Decide surge model: cloud-burst vs mobile-kit vs FVEY partner-pool.

## 1. Finish the contract (crosswalk.csv) — do before batch generation
- [ ] [H] Fill all `nice_dcwf_task` = TODO-map from NICE Components v2.1.0 CSV (defensive roles)
          and DCWF (Red Analyst PO_001-004). Version-pin stays v2.1.0.
- [ ] [H] Fill blank `duration_min` (Red Analyst PO_001-003; ALRA cots_gate rows) from QSP Annex D/E.
- [ ] [H] Replace `PO_TODO` (TEMP67, status=needs_spec) with the real remaining Cpl POs from
          QS_EN_Temp_67 (Chapter 2). Add rows + scenario/build estimates.
- [ ] [H] Sanity-check tier (core vs enrichment) and scenario_count per row against the ~550 core /
          ~900 total design.

## 2. Golden images (run once, before scenarios)
- [ ] [P] Confirm Windows XP + Win7 licensing/entitlement for training. Keep win-xp-sp3 DISABLED
          unless a specific EO needs XP-era behaviour; default legacy -> win7.
- [ ] [T] Generate Packer build files for every vm_catalogue row where enabled=yes (bake sensor per notes).
- [ ] [H] Review Packer files; then [H-gate] run packer build to the vCenter Content Library.
- [ ] [H] Author c2-server + offensive images (enabled=no) — Standards, not Taz.

## 3. Scenario generation (per PO — CLAUDE.md workflow)
- [ ] [T] Pilot: PO_007 (shares PO_009's 3 critical events). Review output end-to-end.
- [ ] [T] Then batch status=todo, tier in (core,gate): PO_006, PO_008, PO_010, ALJQ FKSA, ALRA PO_001,
          TEMP67 PO_008. One commit per PO; append docs/BUILD_LOG.md.
- [ ] [H] Author offensive scenarios (status=offensive_author): Red Analyst PO_001-004 — Standards.
          Taz scaffolds range+validators+rubric ONLY; humans add tradecraft/MSEL/adversary package.
- [ ] [H] COTS gates (status=cots_gate): build EC/PC gates around SANS FOR610/FOR526 + RMC MASC (ALRA
          PO_002-004). Taz can draft the gate scenario; instruction stays COTS.
- [ ] [H] Build 2nd/3rd variants per scenario for retest + skill-fade rotation (QSP 1-yr expiry).

## 4. Validation (Standards)
- [ ] [H] Dry-run each scenario; confirm the range runs, injects fire, noise floor is plausible.
- [ ] [H] Confirm every QSP critical event -> a must-pass validator (Taz flags gaps; Standards signs).
- [ ] [H] Formal CFITES validation serial (MPGTG J7-7 / J3 Stds) + program eval (CFITES Vol 11).

## 5. LMS wiring (lms/xapi_mapping.md)
- [ ] [H] Stand up / confirm LRS. Verify xAPI activity-ID scheme carries the MITE course code.
- [ ] [T] Emit xapi.json per validated scenario with real MITE code + NICE/DCWF task + version.
- [ ] [H] Build + test LRS -> MITE export (qualification grant + MPRR). MITE = record of authority.
- [ ] [H] Confirm EO->PO->Module->NQual roll-up lights up correctly for one test candidate.

## 6. Provisioning (explicit human authorization each time — never autonomous)
- [ ] [H-gate] terraform apply / forge.py provision on the fixed COTE platform.
- [ ] [H-gate] Mobile-kit surge builds for FVEY exercises (per the surge model chosen in section 0).

## Parking lot / decisions still open
- [ ] Fixed-platform hardware sizing to the confirmed concurrent number (denser EPYC 9005 baseline).
- [ ] VVF (steady-state, VLAN+NGFW isolation) vs VCF (only if NSX microseg needed at surge scale).
- [ ] Escalate all costs to YOE (FY2032-2037) via StatCan Non-Res BCPI before entering the CIPPR.
- [ ] Wire crosswalk.csv into the cost-model workbook as the single shared spec.
