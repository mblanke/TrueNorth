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

1. **Timeline** — A sequence of attack events (injects), each at a `t` timecode
2. **Objectives** — What trainees must detect, respond to, or deliver
3. **Validators** — How each objective is judged (an automated query, or an instructor)
4. **Metadata** — Name, version, the range template the scenario runs against, and optional `{{ variables }}` placeholders

Scenarios are defined in YAML and stored in `content/scenarios/` or uploaded via the API. The Scenario Engine parses the YAML, executes timeline events using pluggable **injectors**, and validates objectives using **validators**.

---

## YAML Schema Reference

### Complete Schema

```yaml
# Required metadata
name: string                  # Display name
version: string               # Version string (e.g., "1.0")
range_template: string        # Range template the scenario runs against (e.g., small-enterprise)

# Optional metadata
variables:                    # Values for {{ name }} placeholders used in objective queries
  key: string | number | boolean

# Attack timeline
timeline:
  - t: "0:00"                 # Offset from exercise start, M:SS or HH:MM timecode
    action: string            # Injector action name (see Injector Types)
    params:                   # Action-specific parameters
      key: value

# Training objectives
objectives:
  - id: string                # Unique objective reference
    type: detection | response | deliverable
    validator: string         # Validator name (see Validators): opensearch_query,
                              # manual_ack, deliverable_check, user_disabled,
                              # greyspace_breadcrumb
    params:                   # Validator-specific parameters
      key: value
    points: integer           # Point value (>= 0)
```

Source: `scenario-engine/schemas/scenario.schema.json`. The API's create/update
(`control-plane/api/app/routers/scenarios.py`) enforces the objective rules of
`scenario_engine.objective_catalogue.objective_errors` but not the full JSON Schema;
`POST /scenarios/validate` enforces the full schema and is the contract for authored content.

### Field Validation Rules

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `name` | string | Yes | Display name |
| `version` | string | Yes | Version string |
| `range_template` | string | Yes | A range template ID (e.g., `small-enterprise`) |
| `variables` | object | No | Keys `^[A-Za-z_][A-Za-z0-9_]*$`, values string/number/boolean |
| `timeline[].t` | string | Yes | `M:SS` or `HH:MM` timecode |
| `timeline[].action` | string | No | Must match a registered injector (else the event fails) |
| `timeline[].params` | object | No | Action-specific parameters |
| `objectives[].id` | string | Yes | Unique within scenario |
| `objectives[].type` | enum | Yes | detection, response, deliverable |
| `objectives[].validator` | string | Yes | Must be a known validator, not a refused one |
| `objectives[].points` | integer | Yes | >= 0 |

Objectives additionally cannot set platform-only params (`approved`, `submission`,
`storage_path`, `window_start`, `window_end`) and cannot name refused validators
(`firewall_rule`, `process_killed`, `network_isolated`) — `scenario_engine/objective_catalogue.py`.

---

## Timeline Format

The timeline is a list of events, each with a timecode `t` (offset from exercise start), an
`action` (the injector to run) and `params`. Events are dispatched in list order; the `t`
offset only positions them in exercise time. Both action dialects are accepted: the engine's
`email_phish` and the authored-content spelling `inject.email_phish`
(`scenario_engine.injectors.canonical_action`).

`{{ variables }}` placeholders do **not** render in timeline `params` — events go to the
injector verbatim. They render only in an objective's `query`/`username` params
(`scenario-engine/runner/run.py:166-168`; the API renders the same objective queries in
`control-plane/api/app/detections/templating.py`). Put literal values in timeline params.

### Sequential Events

```yaml
timeline:
  - t: "0:00"
    action: email_phish
    params:
      subject: "Q4 Financial Report"
      sender: "cfo@corp.example.com"
      recipient: "analyst01@corp.example.com"

  - t: "0:05"
    action: dns_spike
    params:
      domains: ["evil.com", "c2.attacker.net"]
      count: 100

  - t: "0:15"
    action: simulated_execution
    params:
      technique: T1059.001
      process_name: powershell.exe
      target: ws-01
```

An event with a missing or unknown `action`, non-mapping `params` or an unparseable `t` is
recorded as failed, not run (`worker/exercise_run.py` `_parse_event`).

### Unsupported: dependencies and conditions

The engine has no `depends_on`, `condition`, or per-event identifiers: the scenario schema
(`scenario-engine/schemas/scenario.schema.json`) accepts only `t`, `action` and `params`,
and neither the standalone runner nor the worker's run loop evaluates dependencies or
condition expressions. To stage activity, give each event a later `t`.

---

## Injector Types and Parameters

Injectors build synthetic records; simulated injectors send no traffic and touch no host
(`scenario_engine/injectors/`, auto-discovered registry). Every inject's telemetry carries
its platform labels under `tn_ground_truth` — stored but not indexed, and refused by the
detection query grammar, so a Student cannot score by querying the labels.

### `email_phish` — Phishing Email Simulation

Builds a synthetic phishing-email record; sends no mail.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sender` | string | No | `attacker@evil.com` | Sender email |
| `recipient` | string | No | `victim@corp.com` | Recipient email |
| `subject` | string | No | `Important Document` | Email subject line |

**Record produced** (`injectors/email_phish.py`):
```json
{
  "injector": "email_phish",
  "sender": "attacker@evil.com",
  "recipient": "victim@corp.com",
  "subject": "Important Document",
  "delivered": false
}
```

---

### `dns_spike` — DNS Query Burst

Builds synthetic records of a burst of DNS queries; sends no queries.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `domains` | [string] | Yes | -- | Domain names to query |
| `count` | integer | Yes | -- | Total queries to generate |

---

### `http_burst` — HTTP Traffic Generation

Builds synthetic records of a burst of HTTP requests; sends no requests.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` (or `target_url`) | string | Yes | -- | Target URL |
| `count` | integer | No | `10` | Number of requests |
| `method` | string | No | `GET` | HTTP method |

---

### `simulated_execution` — Process/Malware Simulation

Builds a synthetic record of a technique executing on a target host.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `technique` | string | No | `T0000` | MITRE ATT&CK technique ID |
| `process_name` | string | No | `unknown.exe` | Process executable name |
| `target` | string | No | `localhost` | Target host |

---

### `identity_new_admin_user` — Identity Manipulation

Builds a synthetic record of a rogue privileged account; creates no account.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `username` | string | No | `rogue_admin` | Account name |
| `target` | string | No | `dc-01` | Domain controller hostname |

---

### Other registered injectors

Also registered and usable as `action` values (`scenario_engine/injectors/__init__.py`,
`list_injectors()`):

| Action | Required params | Description |
|--------|-----------------|-------------|
| `network_scan` | `scan_type`, `target_network` | Synthetic port-scan records |
| `file_operation` | `operation`, `target_host` | Synthetic file-activity records |
| `c2_beacon` | `c2_type`, `target_host` | Synthetic beacon records |
| `ad_attack` | `attack_type`, `target_dc` | Synthetic Active Directory attack records |
| `greyspace_breadcrumb` | `crumbs` (plant) or `ids` (remove) | Plants/removes Greyspace breadcrumbs (ADR 0007) |

---

## Objective Types and Scoring

### Objective Types

| Type | Description | Typical Validators |
|------|-------------|-------------------|
| `detection` | Trainee must detect the attack activity | `opensearch_query` |
| `response` | Trainee must perform a response action | `manual_ack`, `user_disabled` |
| `deliverable` | Trainee must submit an artifact or report | `deliverable_check` |

`user_disabled` and `greyspace_breadcrumb` are also accepted validator names regardless of
type (`scenario-engine/schemas/scenario.schema.json`, `objective_catalogue.SCENARIO_VALIDATORS`).

### Scoring Model

In a team exercise the **instructor judges every objective** through the API (ADR 0020,
`app/exercise_judging.py`); the worker never scores. The scoring engine
(`scenario_engine/scoring/`) is what the CLI runner uses and what backs the instructor's
judgement with evidence:

- `opensearch_query` without `require_observed: true` (the default) is a detection objective:
  the query is the instructor's look-for guidance, and Students' submitted detections are
  recorded as evidence. The engine never credits it on its own — an inject's own synthetic
  records would match the query.
- `opensearch_query` with `require_observed: true` is scored by the engine from observed
  telemetry only (events a range host or sensor produced, inside the exercise window).
  Achieved at `threshold` matches; fewer is **unscored, never failed**.
- `manual_ack` and `deliverable_check` are unscored until an instructor acts.
- An objective that cannot be judged (store down, no window, no query) is reported **unscored**
  with its reason — never as failed, and never silently passed.

```yaml
objectives:
  - id: detect-phish
    type: detection
    validator: opensearch_query
    params:
      query: "event_type:email AND subject:*Invoice*"
      min_hits: 1
    points: 25

  - id: contain-host
    type: response
    validator: manual_ack
    points: 30

  - id: disable-account
    type: response
    validator: user_disabled
    params:
      username: svc_backup
    points: 30

  - id: incident-report
    type: deliverable
    validator: deliverable_check
    params:
      deliverable_type: incident_report
    points: 45
```

### Grading

Letter grades come from `GradingCalculator.letter_grade`
(`scenario_engine/scoring/grading.py`); thresholds are configurable, defaults below.

| Score | Grade |
|-------|-------|
| >= 90% | A |
| >= 80% | B |
| >= 70% | C |
| >= 60% | D |
| < 60% | F |

---

## Validator Configuration

The validator is a **string** on the objective (`validator: opensearch_query`), with the
type-specific parameters under `params`. Refused validators (`firewall_rule`,
`process_killed`, `network_isolated`) fail scenario validation outright.

### `opensearch_query` — Event Detection Validator

Queries the event store for expected events. In an exercise this objective is judged by the
instructor (Students' submitted detections are its evidence); with `require_observed: true`
the scoring engine judges it from observed telemetry only.

```yaml
- id: detect-phish
  type: detection
  validator: opensearch_query
  params:
    query: "event_type:email AND subject:*Invoice*"   # Lucene string or query DSL dict
    min_hits: 1                                        # or threshold; at least 1
    require_observed: true                             # engine-scored: observed events only
    index: "truenorth-*"                               # Optional: index pattern
```

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string \| dict | Yes | -- | Lucene query string or query DSL; a missing query is unscored, never match-all |
| `min_hits` (or `threshold`) | integer | No | 1 | Minimum matching documents (clamped to >= 1) |
| `require_observed` | boolean | No | `false` | Must be `true` for the engine to score it |
| `index` | string | No | `truenorth-*` (scoring engine) / `truenorth-events-*` (runner validator) | Index pattern |

The exercise window (`window_start`/`window_end`) is set by the platform from the exercise,
not by content — only events ingested inside it count (`scoring/validators.py`).

### `manual_ack` — Instructor Acknowledgment Validator

Unscored until an instructor acknowledges the objective. Content cannot pre-approve it
(`params.approved` is a platform-only param and fails scenario validation).

```yaml
- id: contain-host
  type: response
  validator: manual_ack
  params:
    prompt: "Isolate host WS-02 (document steps)"
```

An instructor calls `POST /exercises/{id}/objectives/{ref_id}/ack` (permission
`objective:ack` — instructors and admins, never Students) with an optional note
(`evidence`) and the detection submissions the judgement relies on (`cites`)
(`control-plane/api/app/routers/exercises.py`).

### `deliverable_check` — Deliverable Submission Validator

Judged from the Student's submission record the platform hands to the engine; scenario
content cannot name a file (`params.storage_path` and `params.submission` are platform-only).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `deliverable_type` | string | No | `unknown` | e.g. `incident_report`, `ioc_list` |
| `min_size_bytes` | integer | No | 100 | Minimum file size |
| `required_fields` | [string] | No | `[]` | Fields that must be present (JSON files) |

With no submission record the objective is unscored, and an instructor acknowledges it
(`scoring/validators.py` `validate_deliverable`).

### `user_disabled` — Account Disabled Validator

Achieved when observed Windows Security **4725** (account disabled) for `username` is the
latest such event inside the exercise window, with no later **4722** re-enable
(`scoring/validators.py` `validate_user_disabled`).

```yaml
- id: disable-backdoor
  type: response
  validator: user_disabled
  params:
    username: svc_backup    # a plain account name; no query syntax
```

### `greyspace_breadcrumb` — Greyspace Token Validator

Achieved when the answer a team submitted carries the exercise's breadcrumb token (ADR 0007).
Requires `params.crumb_id`; the token is never put in the evidence.

---

## Detection Rule Authoring

Detection rules in TrueNorth Range use a Sigma-inspired format stored in `content/detections/`.

### Rule Format

```yaml
title: Suspicious PowerShell Download Cradle
id: tn-detect-001
status: stable
description: Detects PowerShell commands commonly used for downloading and executing payloads
author: TrueNorth Range
date: 2024/03/15
mitre_attack:
  - T1059.001
  - T1105
logsource:
  category: process_creation
  product: windows
detection:
  selection:
    Image|endswith: "\\powershell.exe"
    CommandLine|contains:
      - "Invoke-WebRequest"
      - "IWR"
  condition: selection
level: high
falsepositives:
  - Legitimate software installations
  - Admin scripts
```

The severity field is `level` (`low`/`medium`/`high`/`critical`), not `severity`. There are
no `validator_query` or `points` fields — linking a rule to scoring is done by the
scenario's objectives, not by the rule. See `content/detections/powershell-download.yaml`
for a complete shipped example.

### Rule Directory Structure

Detection rules are flat YAML files directly under `content/detections/` (no
subdirectories):

```
content/detections/
+-- credential-dumping-lsass.yaml
+-- data-exfil-dns-tunnel.yaml
+-- defense-evasion-timestomp.yaml
+-- lateral-movement-psexec.yaml
+-- persistence-scheduled-task.yaml
+-- powershell-download.yaml
+-- rdp-brute-force.yaml
+-- suspicious-dns-dga.yaml
+-- vss-deletion.yaml
```

Rules are loaded into the API by `scripts/load_content.py` (step 5, Sigma).

---

## Inject Pack Format

Inject packs are reusable collections of timeline events that can be referenced from multiple scenarios.

### Pack Structure

Inject packs are flat YAML files directly under `content/inject-packs/`, each with `id`,
`name`, `version`, `description`, `mitre_attack` and an `injects:` list. Each inject has
`id`, `type` (an injector name), optional `delay_minutes` and `depends_on`, and `params`
— the same parameter tables as the timeline above.

```yaml
# content/inject-packs/phishing-campaign.yaml
id: inject-phishing-campaign
name: Phishing Campaign Inject Pack
version: "1.0"
description: |
  Simulates a targeted spear-phishing campaign against an organization.
mitre_attack:
  - T1566.001

injects:
  - id: phish-email-delivery
    type: email_phish
    delay_minutes: 0
    params:
      subject: "URGENT: Updated Wire Transfer Instructions"
      # ...

  - id: credential-harvest
    type: simulated_execution
    delay_minutes: 5
    depends_on: phish-email-delivery
    params:
      technique: T1078
```

### Using Packs in Scenarios

**UNKNOWN — not wired up.** The scenario schema has no `inject_packs` field
(`scenario-engine/schemas/scenario.schema.json`), and `scripts/load_content.py:19-21`
states that `content/inject-packs/` and `content/datasets/` are not loaded: nothing in
the API or the worker reads them yet (the worker reads a scenario's `inject_packs` list
and discards it). Pack files are validated as YAML with `id`/`injects`/`mitre_attack` by
`tests/scenario_engine/test_content_validation.py`, and are otherwise reference material.

---

## Dataset Format

Datasets are pre-built telemetry events intended as baseline "noise" or ground-truth
reference material. **UNKNOWN — not wired up:** like inject packs, they are not loaded
into OpenSearch at exercise start or at any other time — nothing in the API or the worker
reads them (`scripts/load_content.py:19-21`; no replay loader exists in the repo).

### Dataset Structure

Datasets are single flat YAML files under `content/datasets/` with an `events:` list. Each
event has `timestamp`, `event_id`, `source`, `description` and a `fields` mapping.

```yaml
# content/datasets/windows-security-events.yaml
# Sample Windows Security Event Logs
# These can be replayed into OpenSearch for training exercises
events:
  - timestamp: "2024-03-15T14:23:01Z"
    event_id: 4624
    source: Security
    description: "Successful logon"
    fields:
      LogonType: 10
      TargetUserName: admin
      TargetDomainName: CORP

  - timestamp: "2024-03-15T14:25:00Z"
    event_id: 1
    source: Microsoft-Windows-Sysmon
    description: "Process Create"
    fields:
      Image: "C:\\Windows\\System32\\cmd.exe"
      CommandLine: "cmd.exe /c whoami /all"
```

Shipped datasets: `active-directory-events.yaml`, `linux-audit-events.yaml`,
`network-traffic-samples.yaml`, `windows-security-events.yaml`. Like inject packs,
datasets are validated by `tests/scenario_engine/test_content_validation.py` but not
loaded by the API or worker (`scripts/load_content.py:19-21`).

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
# content/scenarios/apt-full-chain/scenario.yaml
name: apt-full-chain
version: "1.0"
range_template: medium-enterprise

timeline:
  # Phase 1: Initial Access
  - t: "0:00"
    action: email_phish
    params:
      sender: "cfo@partner-corp.com"
      recipient: "finance@corp.local"
      subject: "Q4 Budget Review - URGENT"

  # Phase 2: Execution
  - t: "0:08"
    action: simulated_execution
    params:
      technique: T1059.001
      process_name: powershell.exe
      target: ws03

  # Phase 3: Reconnaissance
  - t: "0:11"
    action: dns_spike
    params:
      domains: ["dc01.corp.local", "filesvr.corp.local", "exchange.corp.local"]
      count: 20

  # Phase 4: Privilege Escalation
  - t: "0:21"
    action: identity_new_admin_user
    params:
      username: svc_backup
      target: dc-01

  # Phase 5: Exfiltration
  - t: "0:26"
    action: http_burst
    params:
      # Timeline params are NOT template-rendered: a literal value here.
      target_url: "https://storage.attacker-infra.com/upload"
      method: POST
      count: 25

objectives:
  - id: detect-phish
    type: detection
    validator: opensearch_query
    params:
      query: "subject:*Budget*"
    points: 20

  - id: detect-admin
    type: detection
    validator: opensearch_query
    params:
      query: "username:svc_backup"
    points: 20

  - id: contain-lateral
    type: response
    validator: manual_ack
    params:
      prompt: "Isolate filesvr and disable svc_backup (document steps)"
    points: 30

  - id: disable-backdoor
    type: response
    validator: user_disabled
    params:
      username: svc_backup
    points: 10

  - id: incident-report
    type: deliverable
    validator: deliverable_check
    params:
      deliverable_type: incident_report
      min_size_bytes: 500
    points: 20
```

### Step 4: Validate the Scenario

```bash
# Dry run (no actual injection): prints a JSON results summary
python scenario-engine/runner/run.py \
  content/scenarios/apt-full-chain/scenario.yaml \
  --dry-run

# Check all injector types are registered
python -c "import sys; sys.path.insert(0, 'scenario-engine'); \
  from scenario_engine.injectors import list_injectors; print(list_injectors())"
```

`forge template validate <path>` validates a range **template** (against
`scenario-engine/schemas/template.schema.json`), not a scenario. Scenario schema
validation runs when the YAML is uploaded (`POST /scenarios/validate` enforces the full
schema; create/update enforce objective errors) — see the YAML Schema Reference above.

### Step 5: Upload and Test

The API requires authentication, and in dev it is reached directly on `127.0.0.1:8081` or
through nginx on `:4200` (`/api/`) — not `:8080`.

```bash
# Log in and store a token for the CLI
# (TRUENORTH_API_URL defaults to http://localhost:8080 — set it to http://127.0.0.1:8081 for dev)
TRUENORTH_API_URL=http://127.0.0.1:8081 python -m tools.cli.forge login

# Upload via the API (auth required)
curl -X POST http://127.0.0.1:8081/scenarios \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TRUENORTH_API_TOKEN" \
  -d "{
    \"name\": \"apt-full-chain\",
    \"version\": \"1.0\",
    # jq -Rs . already emits a quoted JSON string; quotes around it would be invalid JSON
    \"yaml\": $(cat content/scenarios/apt-full-chain/scenario.yaml | jq -Rs .),
    \"is_public\": true
  }"

# CLI: list scenarios, start an existing exercise
python -m tools.cli.forge scenario list
python -m tools.cli.forge exercise list
python -m tools.cli.forge exercise start <exercise-id>
```

There is no `forge exercise create` — an exercise is created with
`POST /exercises` (range, scenario and team configuration). Ranges are provisioned with
`forge range batch-provision` (there is no `forge range provision`).

---

## Content Directory Structure

```
content/
+-- ranges/                        # one template.yaml per directory
|   +-- small-enterprise/          #   vm_count: 6
|   +-- medium-enterprise/         #   Medium Enterprise Network
|   +-- large-enterprise/          #   vm_count: 50
|   +-- cloud-security/            #   vm_count: 16
|   +-- greyspace-internet/        #   Small Office with Greyspace Internet
|   +-- red-team/                  #   vm_count: 28
|   +-- red-vs-blue/               #   vm_count: 40
|   +-- soc-training/              #   vm_count: 23
+-- scenarios/                     # one scenario.yaml per directory
|   +-- apt-nation-state/
|   +-- cloud-breach/
|   +-- ics-attack/
|   +-- incident-response-drill/
|   +-- insider-threat-advanced/
|   +-- ransomware-lite/
+-- inject-packs/                  # flat YAML files (not loaded by the API/worker)
|   +-- cloud-misconfiguration.yaml
|   +-- insider-threat.yaml
|   +-- phishing-campaign.yaml
|   +-- ransomware-simulation.yaml
|   +-- supply-chain-attack.yaml
+-- datasets/                      # flat YAML files (not loaded by the API/worker)
|   +-- active-directory-events.yaml
|   +-- linux-audit-events.yaml
|   +-- network-traffic-samples.yaml
|   +-- windows-security-events.yaml
+-- detections/                    # flat Sigma-inspired YAML files
    +-- credential-dumping-lsass.yaml
    +-- data-exfil-dns-tunnel.yaml
    +-- defense-evasion-timestomp.yaml
    +-- lateral-movement-psexec.yaml
    +-- persistence-scheduled-task.yaml
    +-- powershell-download.yaml
    +-- rdp-brute-force.yaml
    +-- suspicious-dns-dga.yaml
    +-- vss-deletion.yaml
```

`scripts/load_content.py` loads ranges, scenarios and detections into a running API;
inject packs and datasets are not loaded (`scripts/load_content.py:19-21`).

---

## Testing Scenarios Locally

### Dry Run Mode

```bash
# Validates against the JSON schema, logs each timeline event, no injection
python scenario-engine/runner/run.py \
  content/scenarios/apt-full-chain/scenario.yaml \
  --dry-run \
  --event-store null
```

The runner takes the scenario path positionally, with `--range-id`, `--dry-run`,
`--output <file>` and `--event-store opensearch|null` flags (`runner/run.py` `main()`).
It logs one line per timeline event, then prints a JSON summary:

```json
{
  "scenario": "apt-full-chain",
  "range_id": "standalone-001",
  "timeline": [{"t": "0:00", "action": "email_phish", "status": "dry_run"}],
  "objectives": [],
  "score": {"earned": 0, "max": 0, "unscored": 0}
}
```

`--event-store null` scores nothing, for a dry run without a store; dev time compression
is fixed at 10x (`offset * 0.1`, `runner/run.py:119`).

### Full Local Test

```bash
# 1. Start infrastructure (OpenSearch 127.0.0.1:9200, API 127.0.0.1:8081, web :4200)
cd infra/platform/docker
docker compose -f compose.dev.yml up -d

# 2. Create a range from a template
python -m tools.cli.forge range create --name "Test Range" --template-id <uuid>

# 3. Provision it (batch API; there is no single-range provision command)
python -m tools.cli.forge range batch-provision <range-id>

# 4. Start an exercise (created via POST /exercises; no `forge exercise create`)
python -m tools.cli.forge exercise start <exercise-id>

# 5. Check telemetry (dev binds OpenSearch to loopback)
curl "http://127.0.0.1:9200/range-<range-id>*/_search?q=*&size=10" | python -m json.tool

# 6. Query objectives
curl -H "Authorization: Bearer $TRUENORTH_API_TOKEN" \
  http://127.0.0.1:8081/exercises/<exercise-id>/objectives | python -m json.tool
```

### Content Validation Tests

```bash
# Run content validation test suite
pytest tests/scenario_engine/test_content_validation.py -v

# Checks:
# - inject packs exist, parse, and have id, injects and mitre_attack
# - detections exist and have title, detection and level
# - range templates exist and have assets, nodes or network
# - datasets exist and have events
```

Schema conformance of every shipped scenario and range template against
`scenario-engine/schemas/*.schema.json` is a separate corpus test:
`pytest tests/scenario_engine/test_content_schema_conformance.py -v`.

---

## Advanced Topics

### Unsupported: condition expressions

Timeline events do not support condition expressions. There is no `condition` key, and no
functions like `objective_achieved(ref_id)`, `elapsed_minutes()` or `event_completed()`
exist anywhere in the engine — grep for them and you find only this document's history.
A timeline runs sequentially from its `t` timecodes; see "Unsupported: dependencies and
conditions" under Timeline Format.

### Timeline Compression

There is no `--time-compression` flag. Compression is fixed:

- **Runner (standalone):** 10x — each timeline minute waits 6 seconds
  (`offset * 0.1`, `scenario-engine/runner/run.py:119`).
- **Worker (exercises):** each timeline minute waits at most 1 second
  (`min((offset or 0) * 0.01, 1)`, `control-plane/worker/worker/exercise_run.py:123`).

Exercise duration is set when the exercise is created, not by the scenario YAML (there is
no `duration_minutes` field in the schema).

### Multi-Team Scenarios

The scenario YAML has no `teams` key and no `per_team_overrides` — teams are configured on
the exercise, not in scenario content. ADR 0020: in team exercises every objective is
judged by an instructor, and the worker never scores.