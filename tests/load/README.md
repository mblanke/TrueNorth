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

Ensure the TrueNorth Range API is running:

```bash
# Default target
http://localhost:8080
```

Override via environment variable:

```bash
export BASE_URL=http://your-server:8080
```

---

## Directory Structure

```
tests/load/
├── config.js                   # Shared configuration, metrics, thresholds
├── helpers/
│   ├── data.js                 # Test data generators (ranges, templates, etc.)
│   └── checks.js               # Reusable check/assertion functions
├── scenarios/
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
```

---

## Scenarios

| Scenario | VUs | Duration | Purpose |
|---|---|---|---|
| **smoke** | 10 | 1 min | Validate all endpoints respond correctly |
| **load** | 200 | 10 min | Standard mixed workload (read/write/exercise) |
| **stress** | 1,200 | 15 min | Max capacity — find breaking points |
| **spike** | 100→1,200→100 | ~7 min | Sudden traffic surge + recovery |
| **soak** | 500 | 2 hours | Endurance — detect memory leaks |
| **websocket** | 500 connections | 7 min | WebSocket concurrency + latency |
| **batch-provision** | 10 | 10 min | Batch operations — 500+ provisions |

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

### Stress / Spike (relaxed for peak load)

| Metric | Threshold |
|---|---|
| `http_req_duration` p95 | < 1,000 ms |
| `http_req_duration` p99 | < 3,000 ms |
| `http_req_failed` | < 2% |

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
| `range_creation_duration` | Trend | Time to create a range via POST /ranges |
| `provision_duration` | Trend | Time to provision a range |
| `exercise_complete_duration` | Trend | Time to complete an exercise |
| `batch_provision_duration` | Trend | Time for batch provision requests |
| `ws_message_latency` | Trend | WebSocket round-trip message latency |
| `ws_connect_duration` | Trend | WebSocket connection establishment time |
| `http_errors` | Rate | Fraction of requests returning errors |
| `provisions_enqueued` | Counter | Total provisions submitted |

---

## Interpreting Results

### Console Output

k6 prints a summary table after each run:

```
     ✓ status is 200
     ✓ response time < 500ms

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