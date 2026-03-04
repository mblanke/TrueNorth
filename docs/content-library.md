# TrueNorth Range — Content Library

> Comprehensive catalog of range templates, attack scenarios, detection rules, inject packs, and datasets available in TrueNorth Range.

---

## Table of Contents

- [Overview](#overview)
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

The TrueNorth Range Content Library provides pre-built, tested, and versioned training content. All content is stored in the `content/` directory and can be uploaded via the API or CLI.

### Content Types

| Type | Location | Format | Description |
|------|----------|--------|-------------|
| Range Templates | `content/ranges/` | YAML + Packer/Terraform | VM blueprints and network topologies |
| Scenarios | `content/scenarios/` | YAML | Attack timelines with injectors and objectives |
| Detection Rules | `content/detections/` | YAML (Sigma-inspired) | Rules for validating trainee detections |
| Inject Packs | `content/inject-packs/` | YAML | Reusable collections of timeline events |
| Datasets | `content/datasets/` | YAML + NDJSON | Pre-built telemetry for background noise |

---

## Range Templates

### Template Catalog

#### Small Enterprise (`small-enterprise`)

| Property | Value |
|----------|-------|
| **VMs** | 3 |
| **Description** | Minimal corporate network |
| **Provisioning Time** | 3-5 minutes |
| **Host Requirements** | 8 vCPU, 16 GB RAM, 100 GB disk |

**VM Inventory:**

| Hostname | OS | Role | IP | Installed Software |
|----------|-----|------|-----|-------------------|
| `dc01` | Windows Server 2022 | Domain Controller | 10.10.100.1 | AD DS, DNS, DHCP |
| `ws01` | Windows 11 | Workstation | 10.10.100.10 | Office 365, Sysmon |
| `svr01` | Ubuntu 22.04 | Web/File Server | 10.10.100.20 | Apache, Samba, auditd |

**Network Topology:**
```
[dc01] ─── VLAN 100 ─── [ws01]
              │
           [svr01]
```

---

#### Medium Enterprise (`medium-enterprise`)

| Property | Value |
|----------|-------|
| **VMs** | 8 |
| **Description** | Mid-size corporate with segmented network |
| **Provisioning Time** | 8-12 minutes |
| **Host Requirements** | 16 vCPU, 64 GB RAM, 500 GB disk |

**VM Inventory:**

| Hostname | OS | Role | IP | Installed Software |
|----------|-----|------|-----|-------------------|
| `dc01` | Windows Server 2022 | Domain Controller | 10.10.100.1 | AD DS, DNS, DHCP, GPO |
| `ws01` | Windows 11 | Finance Workstation | 10.10.100.10 | Office 365, Sysmon |
| `ws02` | Windows 11 | IT Admin Workstation | 10.10.100.11 | RSAT, Sysmon, PuTTY |
| `ws03` | Windows 11 | HR Workstation | 10.10.100.12 | Office 365, Sysmon |
| `ws04` | Windows 11 | General Workstation | 10.10.100.13 | Office 365, Sysmon |
| `filesvr` | Windows Server 2022 | File Server | 10.10.100.20 | File shares, Sysmon |
| `websvr` | Ubuntu 22.04 | Web Server | 10.10.200.10 | Nginx, PHP, auditd |
| `fw01` | pfSense 2.7 | Firewall | 10.10.100.254/10.10.200.254 | Suricata IDS |

**Network Topology:**
```
          [Internet]
              │
           [fw01]
           /     \
    VLAN 100     VLAN 200
    (Corp LAN)   (DMZ)
    ┌─────────┐   │
    │  [dc01] │  [websvr]
    │  [ws01] │
    │  [ws02] │
    │  [ws03] │
    │  [ws04] │
    │[filesvr]│
    └─────────┘
```

---

#### Large Enterprise (`large-enterprise`)

| Property | Value |
|----------|-------|
| **VMs** | 20+ |
| **Description** | Full corporate environment with SIEM |
| **Provisioning Time** | 15-25 minutes |
| **Host Requirements** | 32 vCPU, 128 GB RAM, 1 TB disk |

**VM Inventory:**

| Hostname | OS | Role | Subnet |
|----------|-----|------|--------|
| `dc01` | Win Server 2022 | Primary DC | 10.10.100.0/24 |
| `dc02` | Win Server 2022 | Secondary DC | 10.10.100.0/24 |
| `ws01-ws08` | Windows 11 | Workstations (8) | 10.10.100.0/24 |
| `filesvr` | Win Server 2022 | File Server | 10.10.100.0/24 |
| `exchange` | Win Server 2022 | Email Server | 10.10.100.0/24 |
| `websvr01` | Ubuntu 22.04 | Public Web | 10.10.200.0/24 |
| `websvr02` | Ubuntu 22.04 | Internal Web | 10.10.200.0/24 |
| `dbsvr` | Ubuntu 22.04 | Database | 10.10.300.0/24 |
| `siem` | Ubuntu 22.04 | SIEM (ELK) | 10.10.400.0/24 |
| `fw01` | pfSense 2.7 | Edge Firewall | All subnets |
| `fw02` | pfSense 2.7 | Internal Firewall | Internal only |

---

#### SOC Analyst Lab (`soc-analyst-lab`)

| Property | Value |
|----------|-------|
| **VMs** | 5 |
| **Description** | SIEM-focused lab for SOC analyst training |
| **Provisioning Time** | 5-8 minutes |
| **Host Requirements** | 12 vCPU, 32 GB RAM, 200 GB disk |

**VM Inventory:**

| Hostname | OS | Role | IP |
|----------|-----|------|-----|
| `siem` | Ubuntu 22.04 | ELK Stack (Elasticsearch + Kibana) | 10.10.100.5 |
| `zeek` | Ubuntu 22.04 | Zeek Network Monitor | 10.10.100.6 |
| `ws01` | Windows 11 | Endpoint (Sysmon) | 10.10.100.10 |
| `svr01` | Ubuntu 22.04 | Target Server | 10.10.100.20 |
| `attacker` | Kali Linux | Attack Platform | 10.10.200.100 |

---

#### Incident Response Lab (`incident-response`)

| Property | Value |
|----------|-------|
| **VMs** | 10 |
| **Description** | Pre-compromised network for IR exercises |
| **Provisioning Time** | 10-15 minutes |
| **Host Requirements** | 20 vCPU, 64 GB RAM, 500 GB disk |

Includes forensic workstation with:
- Autopsy, Volatility, YARA
- Wireshark, NetworkMiner
- Timeline Explorer
- Pre-loaded disk images and memory dumps

---

## Attack Scenarios

### Scenario Catalog

#### APT Breach (`apt-breach`)

| Property | Value |
|----------|-------|
| **Difficulty** | Advanced |
| **Duration** | 90 minutes |
| **MITRE Techniques** | T1566.001, T1059.001, T1136.001, T1021, T1048.002 |
| **Objectives** | 5 (100 points) |
| **Required Template** | medium-enterprise |

**Attack Narrative:** A sophisticated threat actor targets the organization through spearphishing. After gaining initial access via a malicious document, the attacker executes PowerShell payloads, creates a backdoor admin account, moves laterally to the file server using PsExec, and exfiltrates sensitive data via HTTPS.

**Objectives:**
1. Detect spearphishing email (15 pts)
2. Detect PowerShell execution from Word (20 pts)
3. Detect new admin account creation (20 pts)
4. Contain lateral movement (25 pts)
5. Submit incident report (20 pts)

---

#### Insider Threat (`insider-threat`)

| Property | Value |
|----------|-------|
| **Difficulty** | Intermediate |
| **Duration** | 60 minutes |
| **MITRE Techniques** | T1078, T1083, T1005, T1048.003 |
| **Objectives** | 4 (80 points) |
| **Required Template** | medium-enterprise |

**Attack Narrative:** A disgruntled employee with legitimate credentials begins accessing files outside their normal scope. They enumerate sensitive directories, collect confidential documents, and attempt to exfiltrate data through DNS tunneling.

**Objectives:**
1. Detect unusual file access patterns (20 pts)
2. Detect directory enumeration (20 pts)
3. Detect DNS tunneling (25 pts)
4. Submit incident report (15 pts)

---

#### Supply Chain Compromise (`supply-chain`)

| Property | Value |
|----------|-------|
| **Difficulty** | Expert |
| **Duration** | 120 minutes |
| **MITRE Techniques** | T1195.002, T1059.001, T1543.003, T1071.001, T1486 |
| **Objectives** | 6 (150 points) |
| **Required Template** | large-enterprise |

**Attack Narrative:** A trusted software vendor's update mechanism is compromised. A malicious update installs a backdoor that establishes persistence via a Windows service, communicates with C2 infrastructure over HTTPS, and ultimately deploys ransomware across the network.

**Objectives:**
1. Detect suspicious software update (20 pts)
2. Detect backdoor installation (25 pts)
3. Detect new service creation (20 pts)
4. Detect C2 communication (25 pts)
5. Contain ransomware spread (35 pts)
6. Submit full IR report with IOCs (25 pts)

---

#### Ransomware Response (`ransomware-response`)

| Property | Value |
|----------|-------|
| **Difficulty** | Intermediate |
| **Duration** | 75 minutes |
| **MITRE Techniques** | T1566.001, T1059.001, T1486, T1490 |
| **Objectives** | 5 (100 points) |
| **Required Template** | medium-enterprise |

**Attack Narrative:** Ransomware arrives via phishing email. After execution, it encrypts files on the local workstation, attempts to delete shadow copies, and spreads to network shares.

**Objectives:**
1. Detect phishing email (15 pts)
2. Detect ransomware execution (20 pts)
3. Detect shadow copy deletion (20 pts)
4. Isolate infected host (25 pts)
5. Submit incident report (20 pts)

---

#### Data Exfiltration via Cloud Services (`cloud-exfil`)

| Property | Value |
|----------|-------|
| **Difficulty** | Beginner |
| **Duration** | 45 minutes |
| **MITRE Techniques** | T1567.002, T1083, T1005 |
| **Objectives** | 3 (60 points) |
| **Required Template** | small-enterprise |

**Attack Narrative:** An attacker with compromised credentials accesses the network, browses file shares for sensitive data, and uploads files to an external cloud storage service.

**Objectives:**
1. Detect unauthorized file share access (20 pts)
2. Detect cloud upload activity (25 pts)
3. Submit incident summary (15 pts)

---

## Detection Rules

### Rule Catalog by MITRE Tactic

#### Initial Access

| Rule ID | Title | Technique | Severity |
|---------|-------|-----------|----------|
| `det-phish-attach-001` | Phishing Email with Attachment | T1566.001 | High |
| `det-phish-link-001` | Phishing Email with Link | T1566.002 | High |
| `det-ext-service-001` | Brute Force on External Service | T1110 | Medium |

#### Execution

| Rule ID | Title | Technique | Severity |
|---------|-------|-----------|----------|
| `det-ps-encoded-001` | PowerShell Encoded Command | T1059.001 | High |
| `det-ps-download-001` | PowerShell Download Cradle | T1059.001 | High |
| `det-wscript-001` | WScript/CScript Execution | T1059.005 | Medium |
| `det-mshta-001` | MSHTA Execution | T1218.005 | Medium |

#### Persistence

| Rule ID | Title | Technique | Severity |
|---------|-------|-----------|----------|
| `det-new-admin-001` | New Admin Account Created | T1136.001 | High |
| `det-schtask-001` | Scheduled Task Creation | T1053.005 | Medium |
| `det-service-001` | New Service Installed | T1543.003 | Medium |
| `det-runkey-001` | Registry Run Key Modified | T1547.001 | Medium |

#### Discovery

| Rule ID | Title | Technique | Severity |
|---------|-------|-----------|----------|
| `det-net-enum-001` | Network Share Enumeration | T1135 | Low |
| `det-dir-enum-001` | Directory Enumeration | T1083 | Low |
| `det-whoami-001` | Whoami Execution | T1033 | Low |

#### Lateral Movement

| Rule ID | Title | Technique | Severity |
|---------|-------|-----------|----------|
| `det-psexec-001` | PsExec Lateral Movement | T1570 | High |
| `det-wmi-remote-001` | WMI Remote Execution | T1047 | High |
| `det-rdp-lateral-001` | RDP Lateral Movement | T1021.001 | Medium |

#### Exfiltration

| Rule ID | Title | Technique | Severity |
|---------|-------|-----------|----------|
| `det-dns-tunnel-001` | DNS Tunneling | T1048.001 | High |
| `det-http-exfil-001` | HTTP Data Exfiltration | T1048.002 | High |
| `det-large-upload-001` | Unusually Large Upload | T1567 | Medium |
| `det-cloud-upload-001` | Cloud Storage Upload | T1567.002 | Medium |

#### Impact

| Rule ID | Title | Technique | Severity |
|---------|-------|-----------|----------|
| `det-ransomware-001` | File Encryption Activity | T1486 | Critical |
| `det-shadow-del-001` | Shadow Copy Deletion | T1490 | Critical |
| `det-service-stop-001` | Security Service Stopped | T1489 | High |

---

## Inject Packs

### Pack Catalog

#### Phishing Campaign Pack (`phishing-campaign`)

| Property | Value |
|----------|-------|
| **Events** | 3 |
| **Duration** | ~7 minutes |
| **Techniques** | T1566.001, T1566.002 |

**Events:**
1. DNS reconnaissance of phishing domain (0 min)
2. Phishing email delivery (2 min)
3. HTTP callback to phishing infrastructure (5 min)

---

#### Ransomware Deployment Pack (`ransomware-deployment`)

| Property | Value |
|----------|-------|
| **Events** | 4 |
| **Duration** | ~15 minutes |
| **Techniques** | T1486, T1490, T1489 |

**Events:**
1. Ransomware binary execution (0 min)
2. Shadow copy deletion (2 min)
3. Security service termination (3 min)
4. File encryption activity (5-15 min, continuous)

---

#### Insider Data Collection Pack (`insider-exfil`)

| Property | Value |
|----------|-------|
| **Events** | 3 |
| **Duration** | ~20 minutes |
| **Techniques** | T1083, T1005, T1567.002 |

**Events:**
1. Directory enumeration (0 min)
2. File collection from sensitive shares (5 min)
3. Upload to external cloud storage (15 min)

---

#### C2 Beacon Pack (`c2-beacon`)

| Property | Value |
|----------|-------|
| **Events** | 2 |
| **Duration** | Continuous |
| **Techniques** | T1071.001, T1573 |

**Events:**
1. Initial C2 DNS check-in (0 min)
2. Periodic HTTPS callbacks (every 5 min)

---

#### Credential Harvesting Pack (`credential-harvest`)

| Property | Value |
|----------|-------|
| **Events** | 3 |
| **Duration** | ~10 minutes |
| **Techniques** | T1003.001, T1558.003, T1110 |

**Events:**
1. LSASS memory dump (0 min)
2. Kerberoasting request (3 min)
3. Password spray attempt (7 min)

---

## Datasets

### Dataset Catalog

#### Corporate Baseline (`corporate-baseline`)

| Property | Value |
|----------|-------|
| **Events** | 50,000 |
| **Time Range** | 24 hours |
| **Sources** | Sysmon, Zeek, DNS |
| **Size** | ~25 MB |

Normal corporate activity including: email, web browsing, file access, print jobs, application launches, DNS queries, network connections.

---

#### Web Server Logs (`web-server-logs`)

| Property | Value |
|----------|-------|
| **Events** | 100,000 |
| **Time Range** | 7 days |
| **Sources** | Apache/Nginx access logs |
| **Size** | ~50 MB |

Normal web server traffic including GET/POST requests, status codes, user agents, referrers. Includes some 404 scanning noise.

---

#### SOC Alert Baseline (`soc-alerts-baseline`)

| Property | Value |
|----------|-------|
| **Events** | 5,000 |
| **Time Range** | 30 days |
| **Sources** | Suricata, SIEM alerts |
| **Size** | ~5 MB |

Mix of true positives and false positives for SOC triage training. Includes benign scan alerts, failed logon noise, and legitimate application alerts.

---

## MITRE ATT&CK Coverage Matrix

### Techniques Covered by Scenarios

| Technique | ID | apt-breach | insider-threat | supply-chain | ransomware | cloud-exfil |
|-----------|----|:----------:|:--------------:|:------------:|:----------:|:-----------:|
| Spearphishing Attachment | T1566.001 | X | | | X | |
| Supply Chain Compromise | T1195.002 | | | X | | |
| Valid Accounts | T1078 | | X | | | |
| PowerShell | T1059.001 | X | | X | X | |
| New Admin Account | T1136.001 | X | | | | |
| New Service | T1543.003 | | | X | | |
| File and Directory Discovery | T1083 | | X | | | X |
| Data from Local System | T1005 | | X | | | X |
| Remote Services (PsExec) | T1021 | X | | | | |
| Application Layer Protocol | T1071.001 | | | X | | |
| DNS Tunneling | T1048.001 | | X | | | |
| HTTP Exfiltration | T1048.002 | X | | | | |
| Cloud Storage Upload | T1567.002 | | | | | X |
| Data Encrypted for Impact | T1486 | | | X | X | |
| Inhibit System Recovery | T1490 | | | | X | |

### Coverage Summary

| MITRE Tactic | Techniques Covered | Total Available | Coverage |
|-------------|--------------------|----------------|---------|
| Initial Access | 3 | 9 | 33% |
| Execution | 3 | 12 | 25% |
| Persistence | 3 | 19 | 16% |
| Privilege Escalation | 1 | 13 | 8% |
| Defense Evasion | 1 | 42 | 2% |
| Credential Access | 3 | 17 | 18% |
| Discovery | 4 | 31 | 13% |
| Lateral Movement | 3 | 9 | 33% |
| Collection | 2 | 17 | 12% |
| Exfiltration | 4 | 9 | 44% |
| Impact | 3 | 14 | 21% |

---

## Content Versioning

### Version Scheme

All content follows semantic versioning (`MAJOR.MINOR.PATCH`):

| Change Type | Version Bump | Example |
|------------|-------------|---------|
| Breaking changes (restructured objectives, removed events) | Major | 1.0.0 → 2.0.0 |
| New features (added events, new objectives) | Minor | 1.0.0 → 1.1.0 |
| Bug fixes (typos, timing tweaks) | Patch | 1.0.0 → 1.0.1 |

### Compatibility

- Scenarios specify their minimum API version
- Templates specify their minimum Terraform provider version
- Detection rules specify their minimum OpenSearch version
- Breaking changes are noted in `CHANGELOG.md`

---

## Contributing Content

### Content Submission Process

1. **Fork** the content repository
2. **Create** your content following the schemas in the [Scenario Authoring Guide](scenarios.md)
3. **Validate** using the CLI tools:
   ```bash
   python tools/cli/forge.py template validate content/scenarios/my-scenario.yaml
   python scenario-engine/runner/run.py --scenario content/scenarios/my-scenario.yaml --dry-run
   ```
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