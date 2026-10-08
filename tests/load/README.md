# TrueNorth Range — k6 Load Testing Suite

Comprehensive load, stress, spike, soak, and WebSocket testing for the TrueNorth Range API.
Designed for **1,200 concurrent users** and **70,000 VMs**.

---

## Prerequisites

### Install k6

```powershell
# Windows (winget)
winget install k6 --source winget

# Windows (Chocolatey)
choco install k6

# macOS
brew install k6

# Docker
docker pull grafana/k6
```

Verify installation:

```bash
k6 version
```

### API Server

The scripts call the published contract (`docs/interfaces/openapi.json`) under its base
path `/api/v1`. The default target is the dev stack's API:

```bash
http://localhost:8081
```

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `BASE_URL` | `http://localhost:8081` | The API (itest stack: `http://127.0.0.1:18081`; behind the web nginx: `http://host:4200/api`) |
| `WS_URL` | `BASE_URL` with `ws` | For `/api/v1/ws/{channel}` |
| `API_PREFIX` | `/api/v1` | Set to empty for the unversioned aliases |
| `AUTH_TOKEN` | none | A bearer token, when the target authenticates. A dev or itest stack runs `AUTH_DISABLED` and acts as its dev admin |
| `PROFILE` | `full` | `smoke`: smoke thresholds and rate-limit pacing (the CI run) |
| `RATE_SHARE` | `1/3` under `smoke`, else `0` | Share of the API's per-client rate limits one run may use; `0` turns pacing off |
| `READY_TIMEOUT_S` | `60` | How long a range may take to reach `ready` / `destroyed` |
| `BATCH_SIZE` | `50` (`3` under `smoke`) | Ranges per batch in `batch-provision` |

### Rate limits

The API limits each client address per 60 s sliding window
(`control-plane/api/app/middleware.py`): 200 GETs, 30 `POST /ranges...` (a range's
provision and destroy included), 5 batch provisions, 100 other requests; `/health` is
unlimited. A capacity run from one host is therefore throttled, not measured: run it
against a target with `RATE_LIMIT_ENABLED=false`. Under `PROFILE=smoke` each VU paces
itself (`helpers/api.js`) to `RATE_SHARE` of every limit, so a 429 never happens and
three scenarios run back to back stay inside one window.

---

## Directory Structure

```
tests/load/
├── config.js                   # Environment, metrics, thresholds (full and smoke)
├── helpers/
│   ├── api.js                  # Tagged, checked API calls; rate-limit pacing
│   ├── data.js                 # Request bodies (templates, scenarios, ranges, exercises)
│   ├── flows.js                # Journeys: browse, range lifecycle, exercise, batch
│   └── ws.js                   # Event socket session (/ws/{channel})
├── scenarios/
│   ├── baseline.js             # Light constant load, strict thresholds
│   ├── ci-gate.js              # < 30 s read-only gate
│   ├── smoke.js                # Smoke test — 10 VUs, 1 min
│   ├── load.js                 # Standard load — 200 VUs, 10 min
│   ├── stress.js               # Stress test — 1,200 VUs, 15 min
│   ├── spike.js                # Spike test — 100 → 1,200 → 100
│   ├── soak.js                 # Soak test — 500 VUs, 2 hours
│   ├── websocket.js            # WebSocket — 500 connections
│   └── batch-provision.js      # Batch provision — 500+ provisions
├── results/                    # Generated reports (git-ignored)
├── run-all.ps1                 # PowerShell runner script
└── README.md                   # This file
```

---

## CI smoke (`load-smoke` job in `.github/workflows/ci.yml`)

Every scenario in `scenarios/` runs at **1 VU for 30 s** (`--vus 1 --duration 30s`, which
replaces each script's own `scenarios`/`stages`) against the itest stack: the API on
`http://127.0.0.1:18081`, mock provisioner, `AUTH_DISABLED`, schema from
`alembic upgrade head` (`scripts/itest.sh up`), with `PROFILE=smoke`. k6 is
`grafana/k6` v1.8.1, pinned by digest. Each run has a 240 s wall-clock limit because
`setup()` is not bounded by `--duration`.

It shows that the scripts still run against the current API: every call is to a route
in `docs/interfaces/openapi.json`, with the body its schema requires, and is checked for
the status the contract gives. **It is not a capacity test.** One VU for 30 s says nothing
about 1,200 users.

**Blocking.** A scenario that exits non-zero is an `::error::` and fails the job.
`build/k6/summary.txt` (k6 exit code per scenario; 99 means thresholds crossed) and
`<scenario>.json` (`--summary-export`) are uploaded as the `k6-smoke` artifact.

Under `PROFILE=smoke` every scenario applies the smoke thresholds instead of its own:

| Metric | Smoke threshold |
|---|---|
| `http_req_failed` | < 1% |
| `http_req_duration{kind:read}` p95 | < 1,000 ms |
| `http_req_duration{kind:write}` p95 | < 2,000 ms |
| `checks` | > 99% |
| `batch-provision` also | `batch_provision_duration` p95 < 2,000 ms; `provisions_enqueued` >= 1 |
| `websocket` also | `ws_connect_duration` p95 < 2,000 ms; `ws_message_latency` p95 < 500 ms; `ws_errors` < 1% |

A full run (no `PROFILE`) applies each scenario's own thresholds:

| Scenario | Thresholds |
|---|---|
| `baseline` | `http_req_duration` p50<200, p95<400, p99<800 ms; `http_req_failed` <0.5%; `checks` >99% |
| `ci-gate` | `http_req_duration` p95<400, p99<1000 ms; `http_req_failed` <1%; `checks` >99% |
| `smoke`, `load` | default (below) |
| `soak` | default, with `http_req_duration` p95<600, p99<2000 ms |
| `spike` | default, with `http_req_duration` p95<1500, p99<3000 ms |
| `stress` | stress (below) |
| `batch-provision` | stress, plus `batch_provision_duration` p95<10 s and `provisions_enqueued` count>=500 |
| `websocket` | `ws_connect_duration` p95<2000 ms; `ws_message_latency` p95<500 ms; `ws_errors` <5% |

Locally, against your own itest stack (`ITEST_PROJECT` keeps it apart from any other):

```bash
ITEST_PROJECT=my-itest bash scripts/itest.sh up
docker run --rm --network host -v "$PWD/tests/load:/load:ro" grafana/k6:1.8.1 \
  run --vus 1 --duration 30s -e PROFILE=smoke -e BASE_URL=http://127.0.0.1:18081 \
  /load/scenarios/ci-gate.js
ITEST_PROJECT=my-itest bash scripts/itest.sh down
```

(`--network host` needs Linux, or Docker Desktop with host networking enabled.)

---

## Running Tests

### Run All (default pipeline: smoke → load → stress → spike)

```powershell
.\run-all.ps1
```

### Run a Single Scenario

```powershell
.\run-all.ps1 -Scenario smoke
.\run-all.ps1 -Scenario load
.\run-all.ps1 -Scenario stress
.\run-all.ps1 -Scenario spike
.\run-all.ps1 -Scenario soak
.\run-all.ps1 -Scenario websocket
.\run-all.ps1 -Scenario batch-provision
```

### Override Base URL

```powershell
.\run-all.ps1 -Scenario load -BaseUrl http://staging-api:8080
```

### Continue After Failures

```powershell
.\run-all.ps1 -Force
```

### Run Directly with k6

```bash
k6 run scenarios/smoke.js

# With environment overrides
k6 run -e BASE_URL=http://staging:8080 -e AUTH_TOKEN=mytoken scenarios/load.js

# With custom VU count
k6 run --vus 50 --duration 5m scenarios/smoke.js

# As CI runs it: smoke thresholds, paced under the rate limits
k6 run --vus 1 --duration 30s -e PROFILE=smoke scenarios/smoke.js
```

---

## Scenarios

| Scenario | VUs | Duration | Purpose |
|---|---|---|---|
| **baseline** | 10 | 1 min | Reads, one range created and deleted per iteration; strict latency |
| **ci-gate** | 5→10→0 | 25 s | Read-only: health and the lists every page opens |
| **smoke** | 10 | 1 min | Every journey once: lists, range provisioned and torn down, exercise to its AAR |
| **load** | 200 | 10 min | Standard mixed workload (read/range/exercise/stats) |
| **stress** | 1,200 | 15 min | Max capacity — range lifecycles, paged lists, stats, socket bursts |
| **spike** | 100→1,200→100 | ~7 min | Sudden traffic surge + recovery |
| **soak** | 500 | 2 hours | Endurance — detect memory leaks; periodic batch provision |
| **websocket** | 500 connections | 7 min | Event sockets on the tenant channel: connect, round-trip latency, reconnect |
| **batch-provision** | 10 | one batch each | 10 batches of 50 ranges (500 provisions), each provisioned, torn down, deleted |

A range lifecycle is: `POST /ranges`, `POST /ranges/{id}/provision` (202), poll
`GET /ranges/{id}` until `ready`, `POST /ranges/{id}/destroy` (202), poll until
`destroyed`, `DELETE /ranges/{id}` (204). Exercises run on one range made in `setup()`
(a range with exercises on record is kept for their history). Everything a run creates
is named `k6-...`.

---

## Thresholds

### Default (smoke, load, soak)

| Metric | Threshold |
|---|---|
| `http_req_duration` p95 | < 500 ms |
| `http_req_duration` p99 | < 1,500 ms |
| `http_req_failed` | < 1% |
| `range_creation_duration` p95 | < 800 ms |
| `provision_duration` p95 | < 2,000 ms |
| `exercise_complete_duration` p95 | < 1,500 ms |
| `checks` | > 99% |

### Stress / Spike (relaxed for peak load)

| Metric | Threshold |
|---|---|
| `http_req_duration` p95 | < 1,000 ms |
| `http_req_duration` p99 | < 3,000 ms |
| `http_req_failed` | < 2% |
| `checks` | > 98% |

### WebSocket

| Metric | Threshold |
|---|---|
| `ws_connect_duration` p95 | < 2,000 ms |
| `ws_message_latency` p95 | < 500 ms |
| `ws_errors` | < 5% |

---

## Custom Metrics

| Metric | Type | Description |
|---|---|---|
| `range_creation_duration` | Trend | `POST /ranges` response time |
| `provision_duration` | Trend | `POST /ranges/{id}/provision` response time (accepting it) |
| `range_ready_duration` | Trend | From the provision's acceptance until the range is `ready` |
| `exercise_complete_duration` | Trend | `POST /exercises/{id}/complete` response time |
| `batch_provision_duration` | Trend | `POST /ranges/batch-provision` response time |
| `ws_message_latency` | Trend | Socket round trip of a timed frame (its ack) |
| `ws_connect_duration` | Trend | Socket connection establishment time |
| `ws_errors` | Rate | Sockets that did not upgrade or got no ack |
| `provisions_enqueued` | Counter | Provisions accepted (single and batched) |

Every request is tagged `kind:read` (GET) or `kind:write`, and `name` with its route
template (`GET /ranges/{id}`), so per-route figures stay readable.

---

## Interpreting Results

### Console Output

k6 prints a summary table after each run:

```
     ✓ GET /ranges 200
     ✓ POST /ranges/{id}/provision 202

     http_req_duration..........: avg=45ms  min=12ms  p(95)=120ms  p(99)=350ms
     http_req_failed............: 0.12%  ✓ 15  ✗ 12485
     range_creation_duration....: avg=89ms  p(95)=210ms
```

- **✓** = check passed, **✗** = check failed
- Green thresholds = within limits; **red = breached**

### JSON Reports

Raw JSON summaries are written to `results/`:

```
results/smoke_20260225-143000.json
results/load_20260225-143100_raw.json
```

### Key Indicators

| Indicator | What to Look For |
|---|---|
| **p95 rising** | Possible resource saturation |
| **Error rate climbing** | Backend overload or timeout |
| **Soak p95 drift** | Memory leak or connection pool exhaustion |
| **WS errors > 5%** | WebSocket handler bottleneck |

### Grafana / InfluxDB Integration

For real-time dashboards, pipe k6 output to InfluxDB:

```bash
k6 run --out influxdb=http://localhost:8086/k6 scenarios/load.js
```

Then import the [k6 Grafana dashboard](https://grafana.com/grafana/dashboards/2587).

---

## Capacity Targets

| Metric | Target |
|---|---|
| Concurrent users | 1,200 |
| Total VMs managed | 70,000 |
| Range creation p95 | < 800 ms |
| Provision p95 | < 2,000 ms |
| Batch provision (50 ranges) | < 10,000 ms |
| WebSocket connections | 500 concurrent |
| Soak duration without degradation | 2 hours |

---

## Troubleshooting

### "Too many open files" on Linux/macOS

```bash
ulimit -n 65536
```

### k6 running out of memory

Reduce VUs or use the `--no-connection-reuse` flag.

### High error rate during stress test

This is expected near breaking points. Check:
1. API server logs for 5xx errors
2. Database connection pool limits
3. Worker queue depth via `/ranges/stats`

### WebSocket connections failing

Ensure the server supports the expected number of concurrent WebSocket connections.
Check reverse proxy (nginx/traefik) `proxy_read_timeout` and `worker_connections`.