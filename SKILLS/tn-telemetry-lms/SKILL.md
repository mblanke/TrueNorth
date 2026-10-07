---
name: tn-telemetry-lms
description: "Implement or diagnose TrueNorth telemetry ingestion, OpenSearch queries, xAPI/LRS delivery, and external learning-system adapters."
---

# Telemetry and learning-record integration

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `control-plane/api/app/telemetry_mitre.py`, `control-plane/worker/worker/telemetry_tasks.py`
- `control-plane/api/app/xapi.py`
- `control-plane/api/app/lms`
- `control-plane/api/app/search_backends`
- `tests/integration/test_telemetry_flow.py`
- `tests/api/test_xapi.py`

## Workflow

1. Follow one event from producer to transformation, tenant/range destination, consumer query, and resulting evidence or learning record.
2. Version event contracts and preserve correlation IDs, timestamps, actor/activity identity, and tenant/range ownership through retries.
3. Test unavailable backends, malformed events, duplicates, out-of-order delivery, and backpressure. Avoid silent loss presented as successful ingestion.
4. Keep xAPI emission distinct from LRS acceptance and downstream qualification recording. Validate the configured adapter instead of assuming all backends support every operation.
5. Check index mappings and embedding dimensions when retrieval changes. A text-only fallback should be visible as degraded capability.
6. Use synthetic records and isolated test indices/buckets; read the relevant interoperability source when modifying a protocol contract.

## Verification

Prove the event is queryable or accepted at the intended test destination and that unrelated tenant data cannot appear. Record any untested external hop.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

An integration change or diagnosis with event examples, delivery guarantees, and reproducible checks.

## Boundary

Do not publish real learner records or source documents to an external system during a local code test.

