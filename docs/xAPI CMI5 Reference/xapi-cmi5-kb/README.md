# xAPI and cmi5 knowledge pack (compiled 2026-09-23)

A self-contained reference and lab kit for xAPI (1.0.3 and IEEE 9274.1.1-2023 "2.0"), cmi5 (Quartz) and xAPI Profiles, built for reading and for local LLM / RAG ingestion.

## Contents

| Path | What it is |
|---|---|
| `xapi-cmi5-reference.md` | The consolidated reference (about 14,000 words). Start here. Every section has a `Tag:` line; Section 19 lists confidence and gaps. |
| `corpus/xapi-cmi5-chunks.jsonl` | The reference and the example files pre-split into retrieval chunks (one JSON object per line: `id`, `section`, `subsection`, `tag`, `kind`, `text`, `approx_tokens`, ...). Each chunk starts with a breadcrumb so it stands alone. |
| `examples/` | Validated xAPI statements (1.0.3, 2.0, voiding), a full cmi5 session, LMS.LaunchData, learner preferences, `cmi5.xml` (validates against the official XSD), and an example xAPI Profile. See `examples/README.md`. |
| `lab/` | Docker Compose for SQL LRS on PostgreSQL, localhost-only, secrets in `.env` (copy `.env.example`). |
| `scripts/lrs-smoke-test.sh` | 14 functional checks of any LRS (about, statements, idempotency, 409, State, ETag concurrency and 412, queries). `XAPI_VERSION=2.0.0` for 2.0. |
| `scripts/cmi5-session-sim.sh` | Plays LMS and AU to push one complete cmi5 session through an LRS; `DRY_RUN=1` prints every request; `LMS_ONLY=1` stops after launch for a real AU. |
| `scripts/fetch-sources.sh` | Clones the official spec repositories and builds a flat corpus with provenance (`MANIFEST.tsv`). |
| `scripts/build-chunks.py` | Regenerates the JSONL chunks after you edit the reference. |
| `au/` | Minimal cmi5 Assignable Unit (`cmi5-au.js` + `index.html`, no dependencies) and a Node end-to-end test (`test-au-node.mjs`). |

## Quick start (about 5 minutes; Linux host with Docker, curl, jq)

```bash
cd lab && cp .env.example .env && chmod 600 .env   # then set the placeholder values (runbook Step 1)
docker compose up -d
set -a; . ./.env; set +a; export LRS_ENDPOINT=http://127.0.0.1:8080/xapi
../scripts/lrs-smoke-test.sh && ../scripts/cmi5-session-sim.sh
```
Rollback: `docker compose down` (keep data) or `docker compose down -v` (delete data).

## Test status

Tested on 2026-09-23 against SQL LRS v0.9.8: smoke test 14/14 for both xAPI 1.0.3 and 2.0.0, cmi5 simulator end to end, AU harness 9/9, all example statements accepted with the right version header. Details and untested items: reference Section 19.

## Ingestion notes

- Use `corpus/xapi-cmi5-chunks.jsonl` directly (embed `text`; keep `tag`, `section`, `kind` as metadata filters), or re-chunk the Markdown with your own splitter on `##`/`###` headings.
- Add the primary specs with `scripts/fetch-sources.sh`; they are Apache-2.0 licensed and the script keeps each repository's LICENSE beside the copied files.
- Status facts (IEEE project states, product capabilities) date from 2026-09-23. Re-check before citing them.
