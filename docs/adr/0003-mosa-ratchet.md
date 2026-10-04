# ADR 0003 — MOSA debt ratchet, licensing and data rights

- Status: accepted
- Date: 2026-10-03
- MOSA pillar: enabling environment, conformance

## Context
A MOSA review (2026-10-03) found the runtime well partitioned but the code coupled
across module boundaries. Rules that live only in documents or in an agent's memory
drift between sessions. Rules the Definition of Done enforces do not.

## Decision: the ratchet
`scripts/mosa_check.py` runs in `scripts/dod.sh` and CI. It counts the following and
records them in `.dod-mosa-baseline`. A count above baseline fails, and a count below it
lowers the baseline automatically. CI also fails if the lowered baseline isn't committed.

| Metric | Why it is debt |
|---|---|
| `worker_raw_sql` | The worker writes API-owned tables with raw SQL and no shared model, so a column rename breaks it silently |
| `vendor_sdk_outside_adapters` | A vendor SDK in a router means swapping the vendor requires editing business logic (ADR 0001) |
| `hardcoded_backend_branches` | `if hypervisor_type == "x"` dispatch instead of the registry |
| `web_features_direct_httpclient` | Feature components calling HTTP directly bypass the contract-generated client (ADR 0002) |
| `lines_api_models_py`, `lines_api_schemas_py`, `lines_worker_tasks_py` | God-files every section depends on; new code goes into per-section modules |

Raise a baseline only with a new ADR explaining why.

## Decision: supply-chain evidence
- CI job `supply-chain` produces a CycloneDX SBOM (`sbom.cdx.json`) and pip-audit and
  npm audit reports as build artifacts.
- The audits are **report-only** for now. The 2026-10-03 baseline was 81 Python
  advisories in 10 packages (notably `python-jose`, `starlette`/FastAPI,
  `python-multipart`, `pypdf`, `urllib3`) and 14 npm advisories. Make the job blocking
  once those upgrades land.
- Third-party container images are pinned to a tag. Never use `:latest`.

## Decision: licensing and data rights
- The repository is **proprietary, all rights reserved** (`LICENSE`). This can be
  relaxed later by the copyright holder. Open-sourcing cannot be undone, so it is not
  the default.
- Government data rights (e.g. DFARS 252.227-7013/-7014 in the US, or the applicable
  Canadian contract terms for CAF/DND) are set **by contract**, not by this file.
  Deliverables must carry the restrictive markings the contract allows; unmarked
  deliverables can default to unlimited rights.
- Third-party components keep their own licences. Note Elastic-licensed images in
  `compose.helk.yml` before redistributing that overlay.
