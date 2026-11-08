# TrueNorth Range — Content Library

> Comprehensive catalog of range templates, attack scenarios, detection rules, inject packs, and datasets available in TrueNorth Range.

---

## Table of Contents

- [Overview](#overview)
- [Courses](#courses)
- [Range Templates](#range-templates)
- [Attack Scenarios](#attack-scenarios)
- [Detection Rules](#detection-rules)
- [Inject Packs](#inject-packs)
- [Datasets](#datasets)
- [MITRE ATT&CK Coverage Matrix](#mitre-attck-coverage-matrix)
- [Content Versioning](#content-versioning)
- [Contributing Content](#contributing-content)

---

## Overview

The TrueNorth Range Content Library provides versioned training content. All content is stored in the `content/` directory and is loaded through the API's own endpoints by `scripts/load_content.py` (dev, over HTTP) or `scripts/load_content_inprocess.py` (installer, in-process).

### Content Types

| Type | Location | Format | Description |
|------|----------|--------|-------------|
| Range Templates | `content/ranges/<name>/template.yaml` | YAML | VM blueprints and network topologies (`render.py` renders the infrastructure) |
| Scenarios | `content/scenarios/<name>/scenario.yaml` | YAML | Attack timelines with injectors and objectives |
| Detection Rules | `content/detections/*.yaml` | YAML (Sigma-inspired) | Rules for validating trainee detections |
| Inject Packs | `content/inject-packs/*.yaml` | YAML | Collections of timeline injects — **not loaded yet** (below) |
| Datasets | `content/datasets/*.yaml` | YAML | Small hand-authored sample event lists — **not loaded yet** (below) |
| Courses | `content/courses/` + `content/catalogue/` | YAML + CSV | Catalogue courses (drafts) and the programme they sit in |
| Course releases | `content/releases/` | `.tar.gz` (ARC² release bundle) | Test fixtures only (below) |

---

## Courses

`content/courses/` holds 104 course YAML files (plus `content/courses/instructor/` with
the staff-only instructor packs, which the loader deliberately skips). The programme they
sit in is `content/catalogue/cyber_operator_programme.csv` (44 course rows, tracked in
`content/catalogue/production_register.csv`). Every course file is `status: draft`:
generated or unsourced courseware that Standards has not validated. Imports are always
unpublished.

**Staging (`tn-staging`, v1.2.0-rc3) lists 45 courses** (live host; recorded here from
[release-notes/v1.2.0.md](release-notes/v1.2.0.md), not verified against the host for this
document). Staging loads the shipped content with
`tn_publish_shipped_content` on, and the ARC² batch driver
(`scripts/arc2_batch_inprocess.py`) has run the catalogue through the Course Studio there.
Those releases are **TEST CONTENT**: the staging runner accepts the outline and preview
gates by itself (`ARC2_AUTO_ACCEPT_GATES`, recorded as `accepted_by: auto (test host)`,
badged "TEST CONTENT" in the Studio) and the batch accepts each release's open actions with
the note "auto-accepted test content (staging)". Nobody reviewed them. Never release them
to Students or copy them to another host ([arc2-course-studio.md](arc2-course-studio.md)).

### C105 release: a test fixture, the course stays a draft

`content/releases/c105-foundations-d0e83b9f6c1f.tar.gz` is the ARC² release of C105 that
the programme owner accepted for the pilot. It is committed as a **fixture**: the API tests
(`tests/api/test_c105_release.py`, `test_cmi5_lms.py`, `test_cmi5_lti.py`) and the CI
`cmi5` lane, where ADL CATAPULT imports the `cmi5.xml` TrueNorth serves for it. It is not a
course for Students: C105 is unpublished pending Standards review
(`content/courses/c105-foundations-of-cybersecurity.yaml`: `status: draft`).

Nothing publishes it automatically. The installer's shipped-content load
(`tn_load_shipped_content`, `scripts/load_content_inprocess.py`) never uploads a release, and
with `tn_publish_shipped_content` it publishes every course, quiz and learning path
**except** the courses in its `KEEP_DRAFT` list (C105) and their quizzes
(`tests/contracts/test_installer_shipped_content.py`). The ARC² batch driver on a test
host skips C105 the same way, whatever `--courses` says
(`tests/scripts/test_arc2_batch_inprocess.py`). A host that loaded the content or ran the
batch before this rule keeps whatever it published then: withdraw C105 there as below.

#### Retiring C105 on a host installed before #147

`POST /admin/fixture-courses/C105/withdraw` (platform administrator only;
`app/course_publishing/withdraw.py`) puts the course and its quizzes back to draft and hides
the Moodle course on every Moodle its release was published to (`set_visible(false)` through
the Moodle backend). Nothing is deleted: enrolments, module progress, quiz attempts, final
grades and Moodle result records stay as they were, and the Moodle course is hidden, not
removed. `dry_run=true` is the default and only reports. Each change is audit-logged
(`withdraw_fixture_course`, one row for TrueNorth and one for Moodle, in the course's
tenant). Running it again changes nothing and says so. It refuses (409) while a publication
of the course is running, and stops a requested or stalled one (marked `failed`) so the
startup resume cannot show the course again. Only the codes in `FIXTURE_CODES` (C105) are
accepted. The API image must include this route
(`control-plane/api/app/routers/course_publications.py`, `POST /admin/fixture-courses/{code}/withdraw`;
which released image first carried it is not recorded here).

On the host, as root, first the dry run and then the change (installer default paths):

```bash
cd /srv/truenorth/app/infra/platform/docker
sudo docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production \
  run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v /srv/truenorth/app:/srcapp:ro \
  --entrypoint python api /srcapp/scripts/withdraw_fixture_course.py --admin <platform-admin upn>
# read the report, then the same command with --apply
```

The dry run prints the course, each quiz with its attempts, enrolment, progress and
Moodle-result counts, each Moodle with the course's visibility there, and the lines it would
change. `--apply` prints the same report after re-checking both systems (`APPLIED`, then
`nothing to change` on a second run). Exit status 0 means done or nothing to do; 1 means the
report lists errors (for example a Moodle that could not be reached: TrueNorth is already
done, so re-run once it is back); 2 means refused. `--tenant <id>` names another tenant;
`--json` prints the API's report.

---

## Range Templates

### Template Catalog

All eight shipped templates live in `content/ranges/<name>/template.yaml` and validate
against `scenario-engine/schemas/template.schema.json`
(`python tools/cli/forge.py template validate <template.yaml>`). A template declares
VLANs, nodes (each node's image, IP and NICs), firewall rules and accounts; `vm_count`
is the VM count the nodes build (contract-tested in
`tests/contracts/test_template_vm_count.py`). Templates carry no provisioning-time or
host-requirement data.

#### Small Enterprise (`small-enterprise`) — v1.0, 6 VMs

| VLAN | Subnet | Purpose |
|------|--------|---------|
| mgmt | 10.0.1.0/24 | Management |
| srv | 10.0.2.0/24 | Servers |
| user | 10.0.3.0/24 | User LAN |

| Node | Image | IP |
|------|-------|----|
| `jump-box` | ubuntu-2404 | 10.0.1.10 |
| `dc-01` | windows-server-2022 | 10.0.2.10 |
| `ws-01`…`ws-03` | windows-11 | 10.0.3.10–.12 |
| `fw-01` | pfsense | 10.0.1.1 (one NIC per VLAN) |

#### Medium Enterprise (`medium-enterprise`) — v1.0, 8 VMs

| VLAN | Subnet | Purpose |
|------|--------|---------|
| corporate | 10.10.100.0/24 | User LAN |
| servers | 10.10.200.0/24 | Servers |
| dmz | 10.10.30.0/24 | DMZ |
| management | 10.10.40.0/24 | Management |

| Node | Image | IP |
|------|-------|----|
| `dc01` | windows-server-2022 | 10.10.200.10 |
| `fs01` | windows-server-2022 | 10.10.200.20 |
| `web01` | ubuntu-2404 | 10.10.30.10 |
| `mail01` | ubuntu-2404 | 10.10.30.20 |
| `ws01`, `ws02` | windows-11 | 10.10.100.10, .11 |
| `siem01` | ubuntu-2404 | 10.10.40.10 |
| `fw01` | pfsense | 10.10.40.1 |

#### Large Enterprise (`large-enterprise`) — v1.0, 50 VMs

Full corporate environment: the largest shipped template by VM count. See
`content/ranges/large-enterprise/template.yaml` for the node list.

#### Cloud Security Training Environment (`cloud-security`) — v1.0, 16 VMs

Cloud-flavoured training environment (used by the `cloud-breach` scenario). See
`content/ranges/cloud-security/template.yaml` for the node list.

#### Red Team Operations Environment (`red-team`) — v1.0, 28 VMs

Red-team operations range. See `content/ranges/red-team/template.yaml`.

#### Red vs Blue Cyber Exercise (`red-vs-blue`) — v2.0, 40 VMs

Red-vs-blue exercise range. See `content/ranges/red-vs-blue/template.yaml`.

#### SOC Analyst Training Environment (`soc-training`) — v1.0, 23 VMs

SIEM-focused SOC analyst training environment (used by the
`incident-response-drill` scenario). See `content/ranges/soc-training/template.yaml`.

#### Small Office with Greyspace Internet (`greyspace-internet`) — v1.0

Small office with a greyspace internet segment; 4 nodes and no `vm_count` (the node
list is the inventory). See `content/ranges/greyspace-internet/template.yaml`.

---

## Attack Scenarios

### Scenario Catalog

Six shipped scenarios live in `content/scenarios/<name>/scenario.yaml` and validate
against `scenario-engine/schemas/scenario.schema.json`. Run one with
`python scenario-engine/runner/run.py <scenario.yaml> --dry-run` (positional scenario
path; `--range-id`, `--output` and `--event-store` are optional).

#### apt-nation-state — v1.0

| Property | Value |
|----------|-------|
| **Difficulty** | hard |
| **Duration** | 90 minutes |
| **Required Template** | large-enterprise |
| **Phases** | 12 |
| **Objectives** | 8 (100 points, passing 70) |
| **MITRE (enterprise)** | T1566.001, T1059.005, T1053.005, T1570, T1560.001, T1048.002, T1003.001, T1087.002, T1018, T1021.002, T1071.001, T1027 |

Advanced persistent threat simulation modeled after nation-state adversary tradecraft:
a full kill chain from initial spearphishing to data exfiltration over HTTPS, across
multiple network segments. Emphasizes detection engineering and threat hunting.

---

#### cloud-breach — v1.0

| Property | Value |
|----------|-------|
| **Difficulty** | hard |
| **Duration** | 75 minutes |
| **Required Template** | cloud-security |
| **Phases** | 10 |
| **Objectives** | 7 (100 points, passing 70) |
| **MITRE (enterprise)** | T1530, T1552.005, T1078.004, T1098.003, T1584.007, T1562.008, T1087.004, T1580, T1537, T1190 |

Cloud infrastructure compromise: an attacker discovers an exposed S3 bucket, steals AWS
credentials, escalates privileges through IAM misconfiguration, deploys a Lambda
backdoor, and manipulates CloudTrail to cover tracks. Requires understanding of
cloud-native attack patterns and detection mechanisms. Note: its `T1562.008` (Disable or
Modify Cloud Logs) is **revoked** in the shipped MITRE catalogue
(`content/mitre/enterprise-attack-techniques.json`, revoked by T1685.002) — the scenario
YAML predates that.

---

#### ics-attack — v1.0

| Property | Value |
|----------|-------|
| **Difficulty** | expert |
| **Duration** | 60 minutes |
| **Required Template** | large-enterprise |
| **Phases** | 8 |
| **Objectives** | 6 (100 points, passing 75) |
| **MITRE (ICS)** | T0886, T0843, T0821, T0842, T0856, T0831, T0880, T0826 |

Industrial control system (ICS) attack targeting an OT/SCADA environment: the adversary
moves from the IT network into the OT segment, modifies PLC programs, manipulates HMI
displays, bypasses safety systems, and disrupts physical processes. Based on real-world
ICS attack frameworks (TRITON/TRISIS, Industroyer). The scenario maps its phases to the
Purdue model and uses `mitre_ics` techniques (not the enterprise ATT&CK matrix).

---

#### incident-response-drill — v1.0

| Property | Value |
|----------|-------|
| **Difficulty** | medium |
| **Duration** | 60 minutes |
| **Required Template** | soc-training |
| **Phases** | 6 |
| **Objectives** | 7 (100 points, passing 65) |
| **MITRE (enterprise)** | T1566.001, T1059.001, T1053.005, T1003.001, T1021.002, T1486, T1070.001 |

A blue team incident response exercise with a **pre-staged compromised environment**:
indicators of compromise are scattered across multiple log sources. Participants collect
evidence, reconstruct timelines, and execute containment/eradication/recovery phases
under time pressure. Scoring rewards thoroughness, accuracy, and speed.

---

#### insider-threat-advanced — v1.0

| Property | Value |
|----------|-------|
| **Difficulty** | medium |
| **Duration** | 60 minutes |
| **Required Template** | medium-enterprise |
| **Phases** | 8 |
| **Objectives** | 6 (100 points, passing 60) |
| **MITRE (enterprise)** | T1078, T1078.002, T1083, T1005, T1039, T1052.001, T1567.002, T1070.001, T1070.004, T1098 |

An advanced insider threat: a disgruntled employee escalates privileges, accesses
sensitive data, exfiltrates via USB and cloud storage, and attempts anti-forensic
techniques to cover their tracks. Focuses on behavioral analytics, DLP monitoring, and
insider threat detection patterns.

---

#### ransomware-lite — v0.1

| Property | Value |
|----------|-------|
| **Required Template** | small-enterprise |
| **Objectives** | 2 (25 + 25 points) |
| **MITRE (enterprise)** | T1204 (timeline execution only; no `mitre_attack` list) |

A short introductory ransomware drill: phishing email, simulated dropper execution
(T1204, User Execution) and a DNS spike. No `difficulty`, `duration_minutes` or
`total_points` keys — the earliest and smallest shipped scenario.

---

## Detection Rules

### Rule Catalog

Nine shipped rules in `content/detections/*.yaml`, IDs `tn-detect-001`…`tn-detect-009`
(Sigma-inspired: older rules carry `mitre_attack`/`level` keys, newer ones
`mitre_technique`/`severity`/`status`). The loader reads either key form
(`scripts/load_content.py`, `load_detections`).

| Rule ID | File | Title | Technique(s) | Severity | Status |
|---------|------|-------|--------------|----------|--------|
| `tn-detect-001` | `powershell-download.yaml` | Suspicious PowerShell Download Cradle | T1059.001, T1105 | high | stable |
| `tn-detect-002` | `vss-deletion.yaml` | Volume Shadow Copy Deletion | T1490 | critical | stable |
| `tn-detect-003` | `suspicious-dns-dga.yaml` | Suspicious DNS Queries to Known DGA Patterns | T1568.002, T1071.004 | medium | experimental |
| `tn-detect-004` | `rdp-brute-force.yaml` | RDP Brute Force Attempt | T1110.001, T1021.001 | high | stable |
| `tn-detect-005` | `lateral-movement-psexec.yaml` | Lateral Movement via PsExec | T1570, T1021.002, T1569.002 | high | stable |
| `tn-detect-006` | `credential-dumping-lsass.yaml` | Credential Dumping via LSASS Memory Access | T1003.001 | critical | stable |
| `tn-detect-007` | `data-exfil-dns-tunnel.yaml` | Data Exfiltration via DNS Tunneling | T1048.003, T1071.004 | high | stable |
| `tn-detect-008` | `persistence-scheduled-task.yaml` | Persistence via Scheduled Task Creation | T1053.005 | high | stable |
| `tn-detect-009` | `defense-evasion-timestomp.yaml` | Defense Evasion via File Timestomping | T1070.006 | medium | stable |

---

## Inject Packs

### Pack Catalog

**These packs are not loaded yet.** Per `scripts/load_content.py` (docstring): nothing in
the API or the worker reads them — the worker reads a scenario's `inject_packs` list and
discards it — so there is nowhere for them to appear.

| Pack ID | File | Injects | Delay minutes | MITRE techniques |
|---------|------|---------|---------------|------------------|
| `inject-phishing-campaign` | `phishing-campaign.yaml` | 4 | 0, 5, 15, 20 | T1566.001, T1566.002, T1078, T1021.001 |
| `inject-ransomware-simulation` | `ransomware-simulation.yaml` | 5 | 0, 2, 5, 8, 10 | T1566.001, T1059.001, T1486, T1490 |
| `inject-insider-threat` | `insider-threat.yaml` | 5 | 0, 5, 15, 25, 30 | T1078, T1083, T1005, T1567.002 |
| `inject-cloud-misconfiguration` | `cloud-misconfiguration.yaml` | 5 | 0, 10, 20, 30, 40 | T1530, T1078.004, T1610, T1098.003, T1580 |
| `inject-supply-chain-attack` | `supply-chain-attack.yaml` | 5 | 0, 10, 20, 30, 40 | T1195.001, T1195.002, T1189, T1059.007, T1105 |

All five packs are version 1.0.

---

## Datasets

### Dataset Catalog

**These datasets are not loaded yet** — same as the inject packs: nothing in the API or
the worker reads `content/datasets/` (`scripts/load_content.py`, docstring).

Four small, hand-authored YAML files of sample event lists, not bulk telemetry. They
carry no event-count, size or time-range metadata; each is an `events:` list with a
handful of representative entries (timestamps in early 2024 or 2026).

| File | Contents |
|------|----------|
| `active-directory-events.yaml` | Kerberoasting, Golden Ticket, DCSync and GPO-modification indicators (Windows Security event IDs such as 4769) |
| `linux-audit-events.yaml` | SSH brute force, privilege escalation, file integrity, process anomalies, cron manipulation (~23 `event_type` entries) |
| `network-traffic-samples.yaml` | DNS and flow samples (4 `event_type` entries) |
| `windows-security-events.yaml` | Windows Security logon events (e.g. 4624 with logon type 10), replayable into OpenSearch |

---

## MITRE ATT&CK Coverage Matrix

### Techniques Exercised by Shipped Scenarios

Enterprise techniques from the five scenarios' `mitre_attack` lists, plus `ransomware-lite`'s
timeline technique. `ics-attack` uses `mitre_ics` (T08xx) and sits outside this matrix.

| ID | Technique | Tactic(s) | apt-nation-state | cloud-breach | incident-response-drill | insider-threat-advanced | ransomware-lite |
|----|-----------|-----------|:---:|:---:|:---:|:---:|:---:|
| T1003.001 | LSASS Memory | Credential Access | X | | X | | |
| T1005 | Data from Local System | Collection | | | | X | |
| T1018 | Remote System Discovery | Discovery | X | | | | |
| T1021.002 | SMB/Windows Admin Shares | Lateral Movement | X | | X | | |
| T1027 | Obfuscated Files or Information | Stealth | X | | | | |
| T1039 | Data from Network Shared Drive | Collection | | | | X | |
| T1048.002 | Exfiltration Over Asymmetric Encrypted Non-C2 Protocol | Exfiltration | X | | | | |
| T1052.001 | Exfiltration Over USB | Exfiltration | | | | X | |
| T1053.005 | Scheduled Task | Execution, Persistence, Priv. Esc. | X | | X | | |
| T1059.001 | PowerShell | Execution | | | X | | |
| T1059.005 | Visual Basic | Execution | X | | | | |
| T1070.001 | Clear Windows Event Logs | Stealth | | | X | X | |
| T1070.004 | File Deletion | Stealth | | | | X | |
| T1071.001 | Web Protocols | Command and Control | X | | | | |
| T1078 | Valid Accounts | Init. Access, Persistence, Priv. Esc., Stealth | | | | X | |
| T1078.002 | Valid Accounts: Domain Accounts | Init. Access, Persistence, Priv. Esc., Stealth | | | | X | |
| T1078.004 | Valid Accounts: Cloud Accounts | Init. Access, Persistence, Priv. Esc., Stealth | | X | | | |
| T1083 | File and Directory Discovery | Discovery | | | | X | |
| T1087.002 | Account Discovery: Domain Account | Discovery | X | | | | |
| T1087.004 | Account Discovery: Cloud Account | Discovery | | X | | | |
| T1098 | Account Manipulation | Persistence, Priv. Esc. | | | | X | |
| T1098.003 | Account Manipulation: Additional Cloud Roles | Persistence, Priv. Esc. | | X | | | |
| T1190 | Exploit Public-Facing Application | Initial Access | | X | | | |
| T1204 | User Execution | Execution | | | | | X |
| T1486 | Data Encrypted for Impact | Impact | | | X | | |
| T1530 | Data from Cloud Storage | Collection | | X | | | |
| T1537 | Transfer Data to Cloud Account | Exfiltration | | X | | | |
| T1552.005 | Cloud Instance Metadata API | Credential Access | | X | | | |
| T1560.001 | Archive via Utility | Collection | X | | | | |
| T1566.001 | Spearphishing Attachment | Initial Access | X | | X | | |
| T1562.008 | Disable or Modify Cloud Logs | Stealth | | X | | | |
| T1567.002 | Exfiltration to Cloud Storage | Exfiltration | | | | X | |
| T1570 | Lateral Tool Transfer | Lateral Movement | X | | | | |
| T1580 | Cloud Infrastructure Discovery | Discovery | | X | | | |
| T1584.007 | Compromise Infrastructure: Serverless | Resource Development | | X | | |

Two scenario techniques are **revoked** in the shipped catalogue
(`content/mitre/enterprise-attack-techniques.json`): `T1070.001` (revoked by T1685.005)
and `T1562.008` (revoked by T1685.002). The scenario YAML predates those revocations.

### Coverage Summary

Counts read from the shipped catalogue (816 technique entries, 149 revoked, tactic named
per its `"tactics"` list; a technique counts in every tactic it maps to). The catalogue
names TA0005 **"Stealth"** (not "Defense Evasion") and TA0112 "Defense Impairment".

| Tactic | Catalogue ID | Catalogue active techniques | Exercised by shipped scenarios |
|--------|--------------|----------------------------|--------------------------------|
| Initial Access | TA0001 | 22 | T1190, T1566.001, T1078.004 |
| Execution | TA0002 | 69 | T1059.001, T1059.005, T1053.005, T1204 |
| Persistence | TA0003 | 117 | T1078, T1078.002, T1078.004, T1053.005, T1098, T1098.003 |
| Privilege Escalation | TA0004 | 98 | T1078, T1078.002, T1078.004, T1053.005, T1098, T1098.003 |
| Stealth | TA0005 | 151 | T1027, T1070.001, T1070.004, T1562.008, T1078, T1078.002, T1078.004 |
| Credential Access | TA0006 | 67 | T1003.001, T1552.005 |
| Discovery | TA0007 | 49 | T1018, T1083, T1087.002, T1087.004, T1580 |
| Lateral Movement | TA0008 | 25 | T1021.002, T1570 |
| Collection | TA0009 | 41 | T1005, T1039, T1530, T1560.001 |
| Exfiltration | TA0010 | 19 | T1048.002, T1052.001, T1537, T1567.002 |
| Command and Control | TA0011 | 47 | T1071.001 |
| Impact | TA0040 | 33 | T1486 |
| Resource Development | TA0042 | 50 | T1584.007 |
| Reconnaissance | TA0043 | 46 | — |
| Defense Impairment | TA0112 | 56 | — |

35 unique enterprise techniques are exercised (plus 8 ICS techniques in `ics-attack`).

---

## Content Versioning

### Version Field

Templates, scenarios and inject packs carry a free-form `version:` string — shipped values
include `"1.0"`, `"2.0"` (`red-vs-blue` template) and `"0.1"` (`ransomware-lite`
scenario). This is not strict `MAJOR.MINOR.PATCH` semantic versioning, and no shipped
schema or loader enforces a version scheme; the loader passes the string through as-is
(`scripts/load_content.py`, `load_yaml_items`, default `"1.0"`).

### Compatibility

- Scenarios and templates are validated against `scenario-engine/schemas/scenario.schema.json`
  and `template.schema.json` on load; neither schema carries minimum-API, Terraform-provider
  or OpenSearch version fields.
- Repository-wide changes are noted in the root `CHANGELOG.md`.

---

## Contributing Content

### Content Submission Process

1. **Branch** this repository
2. **Create** your content in its section directory — `content/ranges/<name>/template.yaml`,
   `content/scenarios/<name>/scenario.yaml`, `content/detections/<name>.yaml` — following
   the [Scenario Authoring Guide](scenarios.md)
3. **Validate** using the CLI tools:
   ```bash
   python tools/cli/forge.py template validate content/ranges/my-range/template.yaml
   python scenario-engine/runner/run.py content/scenarios/my-scenario/scenario.yaml --dry-run
   ```
   `forge template validate` checks a **template** against
   `scenario-engine/schemas/template.schema.json`; the scenario runner validates the
   scenario against `scenario.schema.json` when it loads it.
4. **Test** on a local instance with the Docker Compose dev stack
5. **Submit** a pull request with:
   - Content files
   - README describing the scenario/template
   - Test results from dry run
   - MITRE ATT&CK mapping

### Content Review Criteria

| Criterion | Description |
|-----------|-------------|
| **Schema Compliance** | YAML passes schema validation |
| **Dry Run Success** | All injectors and validators pass dry run |
| **Realism** | Attack chain is realistic and educational |
| **Documentation** | Clear description, objectives, and ATT&CK mapping |
| **Testing** | Tested on at least one template size |
| **Originality** | Does not duplicate existing content |
| **Versioned** | Follows semantic versioning |