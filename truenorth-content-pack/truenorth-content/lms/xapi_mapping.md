# xAPI -> LRS -> MITE mapping

## Emit (per validated PC/EC)
xAPI statement: actor=candidate; verb=passed|completed; object=PO (EOs as sub-activities).
Activity ID scheme: https://ccoe.forces.gc.ca/xapi/mite/<MITE_CODE>/po/<PO_ID>
Context extensions: nice_dcwf_task, component_version (pin: v2.1.0), cfites_assessment.

## Roll-up (in the LRS)
EO passed -> PO complete -> Module complete -> NQual (ALJQ / ALRA / etc.).
NICE/DCWF Task IDs ride on every statement -> allied workforce-readiness reporting is automatic.

## Grant (LRS -> MITE)
On NQual roll-up complete, emit an export record keyed by MITE course code for the qualification
grant + MPRR update. Because activity IDs already carry the MITE code, this is a lookup, not a match.
MITE REMAINS RECORD OF AUTHORITY. The LMS/LRS never marks a qualification "granted" itself.

## Do not
- Do not auto-grant in the LRS. Do not drop the component_version (mappings drift on NICE updates).
