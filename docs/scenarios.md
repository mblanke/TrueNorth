# TrueNorth Range — Scenario Authoring Guide

> Complete guide to creating, testing, and deploying training scenarios including YAML schema reference, injector/validator documentation, and end-to-end walkthroughs.

---

## Table of Contents

- [Overview](#overview)
- [YAML Schema Reference](#yaml-schema-reference)
- [Timeline Format](#timeline-format)
- [Injector Types and Parameters](#injector-types-and-parameters)
- [Objective Types and Scoring](#objective-types-and-scoring)
- [Validator Configuration](#validator-configuration)
- [Detection Rule Authoring](#detection-rule-authoring)
- [Inject Pack Format](#inject-pack-format)
- [Dataset Format](#dataset-format)
- [Complete Walkthrough: Building a Scenario](#complete-walkthrough-building-a-scenario)
- [Content Directory Structure](#content-directory-structure)
- [Testing Scenarios Locally](#testing-scenarios-locally)
- [Advanced Topics](#advanced-topics)

---

## Overview

Scenarios are the core training content in TrueNorth Range. Each scenario defines:

1. **Timeline** — A sequence of attack events (injects) with timing and dependencies
2. **Objectives** — What trainees must detect, respond to, or deliver
3. **Validators** — Automated checks that verify objective completion
4. **Metadata** — Name, difficulty, duration, MITRE ATT&CK mappings

Scenarios are defined in YAML and stored in `content/scenarios/` or uploaded via the API. The Scenario Engine parses the YAML, executes timeline events using pluggable **injectors**, and validates objectives using **validators**.

---

## YAML Schema Reference

### Complete Schema

```yaml
# Required metadata
id: string                    # Unique identifier (kebab-case)
name: string                  # Display name
version: string               # Semantic version (e.g., "1.0.0")
description: string           # Human-readable description

# Optional metadata
difficulty: beginner | intermediate | advanced | expert
duration_minutes: integer     # Expected exercise duration
author: string                # Scenario author
tags: [string]                # Searchable tags
mitre_attack: [string]        # MITRE ATT&CK technique IDs (e.g., T1566.001)

# Prerequisites
prerequisites:
  range_template: string      # Required range template ID
  min_vms: integer            # Minimum VMs in range
  required_sensors: [string]  # Required sensors (sysmon, zeek, suricata)

# Attack timeline
timeline:
  - id: string                # Unique event identifier
    type: string              # Injector type (see Injector Types)
    delay_minutes: number     # Minutes after exercise start (or after depends_on)
    depends_on: string        # Optional: event ID this depends on
    condition: string         # Optional: condition expression
    params:                   # Type-specific parameters
      key: value

# Training objectives
objectives:
  - ref_id: string            # Unique objective reference
    title: string             # Display title
    description: string       # Detailed description
    type: detect | respond | report | contain | deliverable
    points: integer           # Point value
    required: boolean         # Whether required for exercise completion
    validator:
      type: string            # Validator type (see Validators)
      params:                 # Type-specific parameters
        key: value

# Optional: inject pack references
inject_packs:
  - pack_id: string
    offset_minutes: number    # Delay before pack starts

# Optional: dataset references
datasets:
  - dataset_id: string
    load_at_start: boolean    # Load into OpenSearch at exercise start
```

### Field Validation Rules

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `id` | string | Yes | kebab-case, unique, 3-100 chars |
| `name` | string | Yes | 1-255 chars |
| `version` | string | Yes | semver format (X.Y.Z) |
| `difficulty` | enum | No | beginner, intermediate, advanced, expert |
| `duration_minutes` | integer | No | 1-480 (max 8 hours) |
| `timeline[].id` | string | Yes | Unique within scenario |
| `timeline[].type` | string | Yes | Must match registered injector |
| `timeline[].delay_minutes` | number | Yes | Non-negative |
| `objectives[].ref_id` | string | Yes | Unique within scenario |
| `objectives[].points` | integer | Yes | 1-1000 |

---

## Timeline Format

The timeline defines the sequence of attack events. Events are executed in order, respecting `delay_minutes` and `depends_on` constraints.

### Sequential Events

```yaml
timeline:
  - id: recon
    type: dns_spike
    delay_minutes: 0
    params:
      domains: ["evil.com", "c2.attacker.net"]
      count: 100

  - id: initial-access
    type: email_phish
    delay_minutes: 5
    params:
      subject: "Q4 Financial Report"
      body_template: phishing/financial-report.html

  - id: execution
    type: simulated_execution
    delay_minutes: 15
    params:
      technique: T1059.001
      process_name: powershell.exe
      command_line: "powershell -enc SQBFAFgA..."
```

### Dependent Events

Events with `depends_on` execute `delay_minutes` after their dependency completes:

```yaml
timeline:
  - id: initial-access
    type: email_phish
    delay_minutes: 0
    params:
      subject: "Invoice Attached"

  - id: lateral-move
    type: simulated_execution
    delay_minutes: 10        # 10 minutes AFTER initial-access completes
    depends_on: initial-access
    params:
      technique: T1570
      description: "PsExec lateral movement to file server"

  - id: exfiltration
    type: http_burst
    delay_minutes: 5         # 5 minutes AFTER lateral-move completes
    depends_on: lateral-move
    params:
      target_url: "https://exfil.attacker.com/upload"
      method: POST
      count: 50
```

### Conditional Events

```yaml
timeline:
  - id: escalation
    type: identity_new_admin_user
    delay_minutes: 20
    condition: "not objective_achieved('obj-detect-phish')"
    params:
      username: backdoor_admin
      action: create
```

### Parallel Events

Events with the same `delay_minutes` and no dependencies execute in parallel:

```yaml
timeline:
  - id: noise-dns
    type: dns_spike
    delay_minutes: 0
    params:
      count: 500

  - id: noise-http
    type: http_burst
    delay_minutes: 0
    params:
      count: 200

  - id: actual-attack
    type: email_phish
    delay_minutes: 0
    params:
      subject: "Urgent: Password Reset"
```

---

## Injector Types and Parameters

### `email_phish` — Phishing Email Simulation

Generates phishing telemetry events in OpenSearch.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `from_address` | string | No | `attacker@evil.com` | Sender email |
| `to_address` | string | No | `user@corp.local` | Recipient email |
| `subject` | string | Yes | -- | Email subject line |
| `body_template` | string | No | -- | Path to HTML body template |
| `has_attachment` | boolean | No | `true` | Whether email has attachment |
| `attachment_name` | string | No | `document.docx` | Attachment filename |
| `mitre_technique` | string | No | `T1566.001` | ATT&CK technique |

**Generated Events:**
```json
{
  "event.action": "email_received",
  "email.from": "attacker@evil.com",
  "email.to": "user@corp.local",
  "email.subject": "Q4 Financial Report",
  "email.has_attachment": true,
  "email.attachment.name": "document.docx"
}
```

---

### `dns_spike` — DNS Query Burst

Generates a burst of DNS query events.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `target_dns` | string | No | `dns01` | Target DNS server hostname |
| `domains` | [string] | Yes | -- | Domain names to query |
| `count` | integer | No | `100` | Total queries to generate |
| `query_types` | [string] | No | `["A"]` | DNS query types (A, AAAA, TXT, MX) |
| `source_hosts` | [string] | No | `["ws01"]` | Source workstations |

**Generated Events:**
```json
{
  "event.action": "dns_query",
  "dns.question.name": "evil.com",
  "dns.question.type": "A",
  "source.ip": "10.10.100.10",
  "destination.ip": "10.10.100.2"
}
```

---

### `http_burst` — HTTP Traffic Generation

Generates HTTP request/response events.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `target_url` | string | Yes | -- | Target URL |
| `method` | string | No | `GET` | HTTP method |
| `count` | integer | No | `50` | Number of requests |
| `interval_ms` | integer | No | `100` | Delay between requests (ms) |
| `user_agents` | [string] | No | Random | Custom User-Agent strings |
| `source_hosts` | [string] | No | `["ws01"]` | Source workstations |

---

### `simulated_execution` — Process/Malware Simulation

Generates process execution telemetry.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `technique` | string | Yes | -- | MITRE ATT&CK technique ID |
| `description` | string | No | -- | Human description of activity |
| `process_name` | string | No | Varies | Process executable name |
| `command_line` | string | No | Varies | Full command line |
| `parent_process` | string | No | `explorer.exe` | Parent process name |
| `host` | string | No | `ws01` | Target host |
| `user` | string | No | `CORP\user1` | Executing user context |

---

### `identity_new_admin_user` — Identity Manipulation

Generates identity and account creation events.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `username` | string | Yes | -- | Account name |
| `action` | string | No | `create` | Action type: `create`, `elevate`, `modify` |
| `domain` | string | No | `CORP` | Domain name |
| `logon_type` | integer | No | `10` | Windows logon type |
| `elevated_to` | string | No | `Domain Admins` | Group for elevation |

---

## Objective Types and Scoring

### Objective Types

| Type | Description | Typical Validators |
|------|-------------|-------------------|
| `detect` | Trainee must detect the attack activity | `opensearch_query` |
| `respond` | Trainee must perform a response action | `manual_ack`, `opensearch_query` |
| `report` | Trainee must submit a written report | `deliverable_check` |
| `contain` | Trainee must contain the threat | `opensearch_query`, `manual_ack` |
| `deliverable` | Trainee must upload an artifact | `deliverable_check` |

### Scoring Model

Points are assigned per objective. Total score = sum of achieved objective points.

```yaml
objectives:
  - ref_id: obj-1
    title: "Detect phishing email"
    type: detect
    points: 25
    required: true
    validator:
      type: opensearch_query
      params:
        query: "event.action:email_received AND email.subject:*Invoice*"
        min_hits: 1

  - ref_id: obj-2
    title: "Block malicious domain"
    type: contain
    points: 30
    required: false
    validator:
      type: manual_ack

  - ref_id: obj-3
    title: "Submit incident report"
    type: deliverable
    points: 45
    required: true
    validator:
      type: deliverable_check
      params:
        bucket: truenorth-artifacts
        key_prefix: "exercises/{exercise_id}/reports/"
```

### Scoring Tiers

| Score Range | Rating | Description |
|-------------|--------|-------------|
| 90-100% | Excellent | Outstanding detection and response |
| 75-89% | Good | Strong performance with minor gaps |
| 60-74% | Satisfactory | Acceptable but room for improvement |
| 40-59% | Needs Improvement | Significant gaps in detection/response |
| 0-39% | Unsatisfactory | Critical training gaps identified |

---

## Validator Configuration

### `opensearch_query` — Event Detection Validator

Queries OpenSearch for expected events. Passes if the query returns at least `min_hits` results.

```yaml
validator:
  type: opensearch_query
  params:
    query: "event.action:email_received AND email.subject:*Invoice*"
    min_hits: 1
    index_pattern: "tn-range-{range_id}-*"     # Optional: override default
    time_range_minutes: 60                      # Optional: look-back window
```

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | -- | OpenSearch query string |
| `min_hits` | integer | No | 1 | Minimum matching documents |
| `index_pattern` | string | No | Auto | OpenSearch index pattern |
| `time_range_minutes` | integer | No | Unlimited | Time window for query |

### `manual_ack` — Human Acknowledgment Validator

Requires a trainee to manually acknowledge the objective via the API or UI.

```yaml
validator:
  type: manual_ack
  params: {}    # No parameters needed
```

The trainee calls `POST /exercises/{id}/objectives/{ref_id}/ack` with optional evidence.

### `deliverable_check` — Artifact Upload Validator

Checks MinIO for an uploaded artifact (report, screenshot, PCAP, etc.).

```yaml
validator:
  type: deliverable_check
  params:
    bucket: truenorth-artifacts
    key_prefix: "exercises/{exercise_id}/reports/"
    min_size_bytes: 1024               # Optional: minimum file size
    allowed_extensions: [".pdf", ".docx", ".html"]  # Optional: file type filter
```

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `bucket` | string | Yes | -- | MinIO bucket name |
| `key_prefix` | string | Yes | -- | Object key prefix to check |
| `min_size_bytes` | integer | No | 0 | Minimum artifact size |
| `allowed_extensions` | [string] | No | Any | Allowed file extensions |

---

## Detection Rule Authoring

Detection rules in TrueNorth Range use a Sigma-inspired format stored in `content/detections/`.

### Rule Format

```yaml
title: "PowerShell Encoded Command Execution"
id: det-ps-encoded-001
status: production
description: "Detects execution of PowerShell with base64 encoded commands"
author: TrueNorth Range Team
date: 2026/01/15
severity: high
mitre_attack:
  - T1059.001

logsource:
  product: windows
  service: sysmon
  category: process_creation

detection:
  selection:
    process.name: "powershell.exe"
    process.command_line|contains:
      - "-enc"
      - "-EncodedCommand"
      - "-e "
  condition: selection

falsepositives:
  - Legitimate automation scripts
  - System management tools

tags:
  - attack.execution
  - attack.t1059.001

# TrueNorth-specific fields
validator_query: "process.name:powershell.exe AND process.command_line:(*-enc* OR *-EncodedCommand*)"
points: 15
```

### Rule Directory Structure

```
content/detections/
+-- initial-access/
|   +-- det-phishing-attachment.yaml
|   +-- det-phishing-link.yaml
+-- execution/
|   +-- det-powershell-encoded.yaml
|   +-- det-wscript-execution.yaml
+-- persistence/
|   +-- det-new-admin-account.yaml
|   +-- det-scheduled-task.yaml
+-- lateral-movement/
|   +-- det-psexec.yaml
|   +-- det-wmi-remote.yaml
+-- exfiltration/
|   +-- det-large-dns-queries.yaml
|   +-- det-http-exfil.yaml
```

---

## Inject Pack Format

Inject packs are reusable collections of timeline events that can be referenced from multiple scenarios.

### Pack Structure

```yaml
# content/inject-packs/phishing-campaign/pack.yaml
id: phishing-campaign
name: "Phishing Campaign Pack"
version: "1.0.0"
description: "Multi-stage phishing campaign with follow-up exploitation"
tags: ["phishing", "initial-access"]
mitre_attack: ["T1566.001", "T1566.002"]

events:
  - id: phish-recon
    type: dns_spike
    delay_minutes: 0
    params:
      domains: ["corp-login.evil.com"]
      count: 10

  - id: phish-email
    type: email_phish
    delay_minutes: 2
    depends_on: phish-recon
    params:
      subject: "Action Required: Password Expiration"
      from_address: "it-support@corp-login.evil.com"
      body_template: phishing/password-reset.html

  - id: phish-callback
    type: http_burst
    delay_minutes: 5
    depends_on: phish-email
    params:
      target_url: "https://corp-login.evil.com/harvest"
      method: POST
      count: 3
```

### Using Packs in Scenarios

```yaml
inject_packs:
  - pack_id: phishing-campaign
    offset_minutes: 0          # Start immediately

  - pack_id: ransomware-deployment
    offset_minutes: 30         # Start 30 minutes into exercise
```

---

## Dataset Format

Datasets provide pre-built telemetry events that can be loaded into OpenSearch at exercise start. They provide baseline "noise" or ground truth data.

### Dataset Structure

```
content/datasets/corporate-baseline/
+-- dataset.yaml              # Metadata
+-- events.ndjson             # Newline-delimited JSON events
```

**`dataset.yaml`:**

```yaml
id: corporate-baseline
name: "Corporate Network Baseline"
version: "1.0.0"
description: "24 hours of normal corporate network activity"
event_count: 50000
time_range: "24h"
sources:
  - sysmon
  - zeek
  - dns
tags: ["baseline", "normal-activity"]
```

**`events.ndjson`:**

```json
{"timestamp": "2026-01-15T08:00:00Z", "event.action": "process_create", "process.name": "outlook.exe", "host.name": "ws01", "source": "sysmon"}
{"timestamp": "2026-01-15T08:00:01Z", "event.action": "dns_query", "dns.question.name": "mail.corp.local", "source": "dns"}
{"timestamp": "2026-01-15T08:00:02Z", "event.action": "network_connection", "destination.ip": "10.10.100.5", "destination.port": 443, "source": "zeek"}
```

---

## Complete Walkthrough: Building a Scenario

### Step 1: Define the Attack Story

**Scenario:** An APT group targets the organization via spearphishing. After gaining initial access, the attacker performs reconnaissance, escalates privileges, moves laterally, and attempts data exfiltration.

### Step 2: Map to MITRE ATT&CK

| Phase | Technique | ID |
|-------|-----------|-----|
| Initial Access | Spearphishing Attachment | T1566.001 |
| Execution | PowerShell | T1059.001 |
| Persistence | New Admin Account | T1136.001 |
| Lateral Movement | Remote Services | T1021 |
| Exfiltration | HTTP Exfiltration | T1048.002 |

### Step 3: Write the YAML

```yaml
id: apt-full-chain
name: "APT Full Attack Chain"
version: "1.0.0"
description: "Complete APT attack from spearphishing to exfiltration"
difficulty: advanced
duration_minutes: 90
author: "TrueNorth Range Content Team"
tags: ["apt", "full-chain", "advanced"]
mitre_attack:
  - T1566.001
  - T1059.001
  - T1136.001
  - T1021
  - T1048.002

prerequisites:
  range_template: medium-enterprise
  min_vms: 8
  required_sensors: ["sysmon", "zeek"]

timeline:
  # Phase 1: Initial Access
  - id: phish-email
    type: email_phish
    delay_minutes: 0
    params:
      from_address: "cfo@partner-corp.com"
      to_address: "finance@corp.local"
      subject: "Q4 Budget Review - URGENT"
      has_attachment: true
      attachment_name: "Q4_Budget_Review.docx"

  # Phase 2: Execution
  - id: payload-exec
    type: simulated_execution
    delay_minutes: 8
    depends_on: phish-email
    params:
      technique: T1059.001
      process_name: powershell.exe
      command_line: "powershell -WindowStyle Hidden -enc SQBFAFgA..."
      parent_process: WINWORD.EXE
      host: ws03
      user: "CORP\\finance_user"

  # Phase 3: Reconnaissance
  - id: recon-dns
    type: dns_spike
    delay_minutes: 3
    depends_on: payload-exec
    params:
      domains: ["dc01.corp.local", "filesvr.corp.local", "exchange.corp.local"]
      count: 20
      source_hosts: ["ws03"]

  # Phase 4: Privilege Escalation
  - id: priv-esc
    type: identity_new_admin_user
    delay_minutes: 10
    depends_on: recon-dns
    params:
      username: svc_backup
      action: create
      elevated_to: "Domain Admins"
      domain: CORP

  # Phase 5: Lateral Movement
  - id: lateral-move
    type: simulated_execution
    delay_minutes: 5
    depends_on: priv-esc
    params:
      technique: T1021
      process_name: psexec.exe
      command_line: "psexec \\\\filesvr -u CORP\\svc_backup -p ... cmd.exe"
      host: filesvr
      user: "CORP\\svc_backup"

  # Phase 6: Exfiltration
  - id: exfiltration
    type: http_burst
    delay_minutes: 15
    depends_on: lateral-move
    params:
      target_url: "https://storage.attacker-infra.com/upload"
      method: POST
      count: 25
      source_hosts: ["filesvr"]

objectives:
  - ref_id: obj-detect-phish
    title: "Detect spearphishing email"
    description: "Identify the phishing email targeting the finance department"
    type: detect
    points: 15
    required: true
    validator:
      type: opensearch_query
      params:
        query: "event.action:email_received AND email.subject:*Budget*"
        min_hits: 1

  - ref_id: obj-detect-exec
    title: "Detect PowerShell execution"
    description: "Identify the encoded PowerShell command spawned by Word"
    type: detect
    points: 20
    required: true
    validator:
      type: opensearch_query
      params:
        query: "process.name:powershell.exe AND process.parent.name:WINWORD.EXE"
        min_hits: 1

  - ref_id: obj-detect-admin
    title: "Detect new admin account"
    description: "Identify the creation of the svc_backup administrative account"
    type: detect
    points: 20
    required: false
    validator:
      type: opensearch_query
      params:
        query: "event.action:user_created AND user.name:svc_backup"
        min_hits: 1

  - ref_id: obj-contain-lateral
    title: "Contain lateral movement"
    description: "Block or isolate the compromised file server"
    type: contain
    points: 25
    required: false
    validator:
      type: manual_ack

  - ref_id: obj-incident-report
    title: "Submit incident report"
    description: "Upload a completed incident report documenting the full attack chain"
    type: deliverable
    points: 20
    required: true
    validator:
      type: deliverable_check
      params:
        bucket: truenorth-artifacts
        key_prefix: "exercises/{exercise_id}/reports/"
        allowed_extensions: [".pdf", ".docx"]
```

### Step 4: Validate the Scenario

```bash
# Validate YAML syntax and schema
python tools/cli/forge.py template validate content/scenarios/apt-full-chain.yaml

# Dry run (no actual injection)
python scenario-engine/runner/run.py \
  --scenario content/scenarios/apt-full-chain.yaml \
  --dry-run

# Check all injector types are registered
python -c "from scenario_engine.injectors import get_registry; print(get_registry())"
```

### Step 5: Upload and Test

```bash
# Upload via API
curl -X POST http://localhost:8080/scenarios \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"APT Full Chain\",
    \"version\": \"1.0.0\",
    \"yaml\": \"$(cat content/scenarios/apt-full-chain.yaml | jq -Rs .)\",
    \"is_public\": true
  }"

# Create exercise and test
python tools/cli/forge.py exercise create --name "APT Test" --range-id <uuid> --scenario-id <uuid>
python tools/cli/forge.py exercise start <exercise-id>
```

---

## Content Directory Structure

```
content/
+-- ranges/
|   +-- small-enterprise/
|   |   +-- template.yaml        # 3 VMs: DC, workstation, server
|   |   +-- README.md
|   +-- medium-enterprise/
|   |   +-- template.yaml        # 8 VMs: DC, 4 WS, 2 servers, FW
|   |   +-- README.md
|   +-- large-enterprise/
|       +-- template.yaml        # 20+ VMs: full corporate environment
|       +-- README.md
+-- scenarios/
|   +-- apt-breach.yaml
|   +-- insider-threat.yaml
|   +-- supply-chain.yaml
|   +-- ransomware-response.yaml
|   +-- apt-full-chain.yaml
+-- inject-packs/
|   +-- phishing-campaign/
|   |   +-- pack.yaml
|   +-- ransomware-deployment/
|   |   +-- pack.yaml
|   +-- insider-exfil/
|       +-- pack.yaml
+-- datasets/
|   +-- corporate-baseline/
|   |   +-- dataset.yaml
|   |   +-- events.ndjson
|   +-- web-server-logs/
|       +-- dataset.yaml
|       +-- events.ndjson
+-- detections/
    +-- initial-access/
    +-- execution/
    +-- persistence/
    +-- lateral-movement/
    +-- exfiltration/
```

---

## Testing Scenarios Locally

### Dry Run Mode

```bash
# Validates YAML, checks all injectors exist, simulates timeline
python scenario-engine/runner/run.py \
  --scenario content/scenarios/apt-full-chain.yaml \
  --dry-run \
  --verbose

# Output:
# [DRY RUN] Timeline: 6 events
# [00:00] phish-email (email_phish) -- OK
# [00:08] payload-exec (simulated_execution) depends_on:phish-email -- OK
# [00:11] recon-dns (dns_spike) depends_on:payload-exec -- OK
# [00:21] priv-esc (identity_new_admin_user) depends_on:recon-dns -- OK
# [00:26] lateral-move (simulated_execution) depends_on:priv-esc -- OK
# [00:41] exfiltration (http_burst) depends_on:lateral-move -- OK
# Timeline valid. Total duration: ~41 minutes.
# Objectives: 5 (100 points total)
```

### Full Local Test

```bash
# 1. Start infrastructure
cd infra/platform/docker
docker compose -f compose.dev.yml up -d

# 2. Create a range and provision it
python tools/cli/forge.py range create --name "Test Range" --template-id <uuid>
python tools/cli/forge.py range provision <range-id>

# 3. Create exercise
python tools/cli/forge.py exercise create \
  --name "Scenario Test" \
  --range-id <range-id> \
  --scenario-id <scenario-id>

# 4. Start exercise (runs scenario)
python tools/cli/forge.py exercise start <exercise-id>

# 5. Check telemetry
curl "http://localhost:9200/tn-range-<range-id>-*/_search?q=*&size=10" | python -m json.tool

# 6. Query objectives
curl http://localhost:8080/exercises/<exercise-id>/objectives | python -m json.tool
```

### Content Validation Tests

```bash
# Run content validation test suite
pytest tests/scenario_engine/test_content_validation.py -v

# Validates:
# - All YAML files parse correctly
# - All referenced injector types are registered
# - All validator types are registered
# - Objective ref_ids are unique
# - Timeline event IDs are unique
# - Dependency chains have no cycles
```

---

## Advanced Topics

### Custom Condition Expressions

Timeline events support condition expressions that reference objective state:

```yaml
- id: escalation
  type: identity_new_admin_user
  delay_minutes: 30
  condition: "not objective_achieved('obj-detect-phish')"
  params:
    username: escalation_account
```

Available functions:
- `objective_achieved(ref_id)` — True if objective is achieved
- `elapsed_minutes()` — Minutes since exercise start
- `event_completed(event_id)` — True if timeline event completed

### Timeline Compression

For shorter exercises, timelines can be compressed:

```bash
python scenario-engine/runner/run.py \
  --scenario content/scenarios/apt-full-chain.yaml \
  --time-compression 4x   # 4x speed: 90min scenario runs in ~22min
```

### Multi-Team Scenarios

Scenarios can be configured for multi-team exercises where each team gets isolated events:

```yaml
teams:
  mode: isolated          # Each team gets their own event timeline
  max_teams: 4
  per_team_overrides:
    - team_index: 0
      timeline_override:
        - id: phish-email
          params:
            to_address: "team1@corp.local"
```