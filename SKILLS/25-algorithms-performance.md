# Algorithms & Performance
- Profile before optimizing.
- Prefer async I/O for network-bound operations (FastAPI async endpoints, httpx).
- Use database indexes for query-heavy fields (range_id, tenant_id, state).
- Pagination on all list endpoints (limit/offset or cursor-based).
- Batch telemetry writes to OpenSearch.
