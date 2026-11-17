# Acceptance checklist — range template `soc-training`

Derived from source only. Sources: `content/ranges/soc-training/template.yaml` (referenced as
`template:LINE`) and the 22 courses in `content/courses/*.yaml` whose `lab_range.template` is
`soc-training` (referenced as `FILE:LINE`). Base commit d5851f6d.

Labels: **OBSERVED** = read verbatim at the cited line. **INFERRED** = concluded from cited
lines. **UNKNOWN** = not derivable from source; do not guess. **PROPOSED** = suggested fix,
not author-decided.

---

## 1. Nodes

23 nodes. **OBSERVED** `template:11` (`vm_count: 23`, comment points at
`tests/contracts/test_template_vm_count.py`). All rows below **OBSERVED** at the cited line.

| # | id | OS | role | VLAN | IP | Services / notes |
|---|----|----|------|------|----|------------------|
| 1 | c2-server | kali-2024 | c2_server | attacker_infra | 10.60.200.10 | sliver_c2, metasploit, empire (template:65-72) |
| 2 | phish-server | ubuntu-2404 | phishing | attacker_infra | 10.60.200.20 | gophish, nginx, letsencrypt_sim (template:74-81) |
| 3 | staging01 | ubuntu-2404 | staging | attacker_infra | 10.60.200.30 | sftp, http_fileserver, dns_exfil_listener (template:83-90) |
| 4 | redir01 | ubuntu-2404 | redirector | attacker_infra | 10.60.200.40 | socat, iptables_redirect (template:92-99) |
| 5 | v-dc01 | windows-server-2022 | domain_controller | victim_network | 10.60.201.10 | active_directory, dns, dhcp (template:102-109) |
| 6 | v-fs01 | windows-server-2022 | file_server | victim_network | 10.60.201.20 | smb, dfs; pre_staged_data: `\shares\finance\payroll.xlsx`, `\shares\hr\employees.csv`, `\shares\engineering\credentials.kdbx` (template:111-122) |
| 7 | v-web01 | ubuntu-2404 | web_server | victim_network | 10.60.201.30 | nginx, dvwa, juice_shop (template:124-131) |
| 8 | v-ws01 | windows-11 | workstation | victim_network | 10.60.201.100 | user_profile finance_user (template:133-140) |
| 9 | v-ws02 | windows-11 | workstation | victim_network | 10.60.201.101 | user_profile hr_user (template:142-149) |
| 10 | v-ws03 | windows-11 | workstation | victim_network | 10.60.201.102 | user_profile it_admin (template:151-158) |
| 11 | v-ws04 | windows-11 | workstation | victim_network | 10.60.201.103 | user_profile executive (template:160-167) |
| 12 | v-mail01 | ubuntu-2404 | mail_server | victim_network | 10.60.201.40 | postfix, dovecot, roundcube (template:169-176) |
| 13 | siem01 | ubuntu-2404 | siem | soc_tools | 10.60.202.10 | opensearch, opensearch_dashboards, sigma_rules (template:179-186) |
| 14 | velociraptor01 | ubuntu-2404 | edr | soc_tools | 10.60.202.20 | velociraptor_server; note: agents on all victim workstations (template:188-196) |
| 15 | thehive01 | ubuntu-2404 | soar | soc_tools | 10.60.202.30 | thehive, cortex, elasticsearch (template:198-205) |
| 16 | misp01 | ubuntu-2404 | threat_intel | soc_tools | 10.60.202.40 | misp, mysql (template:207-214) |
| 17 | dfir-ws01 | ubuntu-2404 | analyst_workstation | soc_tools | 10.60.202.50 | autopsy, volatility3, plaso, yara (template:216-223) |
| 18 | seconion01 | securityonion | network_monitor | soc_tools | 10.60.202.60 | zeek, suricata, elasticsearch, kibana, strelka; dual-homed (soc_tools mgmt NIC + network_monitoring monitor NIC); `so_allow: [management]` (template:228-240) |
| 19 | zeek01 | ubuntu-2404 | network_sensor | network_monitoring | 10.60.203.20 | zeek, filebeat (template:242-249) |
| 20 | pcap01 | ubuntu-2404 | packet_capture | network_monitoring | 10.60.203.30 | stenographer, arkime (template:251-258) |
| 21 | jump01 | ubuntu-2404 | jump_host | management | 10.60.204.10 | guacamole, ssh (template:261-268) |
| 22 | log-agg01 | ubuntu-2404 | log_collector | management | 10.60.204.20 | rsyslog, logstash, filebeat (template:270-277) |
| 23 | fw01 | pfsense | firewall | management | 10.60.204.1 | gateway interfaces on all five VLANs (template:279-296) |

**Known discrepancy.** ADR 0004 (`docs/adr/0004-scheduler-module.md:354-360`) records that
this template once declared `vm_count: 25` while building 23; the field was corrected to
`vm_count: 23` (template:11) and `tests/contracts/test_template_vm_count.py` checks that
declared `vm_count` against what the worker renders. A residual stale field remains:
`resource_totals.total_vms: 25` (template:299), which nothing in the repo reads or
validates (`grep -rn resource_totals --include='*.py' control-plane/ scripts/ tests/`
returns no hits). Nothing reads `vm_count` to size bookings; demand is derived from
nodes. Acceptance: accept the 23-node build, do not "fix" either count here. **OBSERVED**

**Gap flagged.** `radare2` is required on dfir-ws01 by C527 module 4
(`up-dfi-03-static-analysis-beyond-the-basics.yaml:316-317`, AUTHOR-REQUIRED note at :309)
but
is not in the template's dfir-ws01 services list (template:223). Acceptance depends on the
author decision this note requests. **OBSERVED** (both halves) / unresolved.

## 2. Networks and addressing

Five VLANs. **OBSERVED** template:13-34.

| VLAN id | name | CIDR | description |
|---------|------|------|-------------|
| 200 | attacker_infra | 10.60.200.0/24 | attacker C2, phishing, staging |
| 201 | victim_network | 10.60.201.0/24 | simulated victim corporate network |
| 202 | soc_tools | 10.60.202.0/24 | SOC analyst platforms |
| 203 | network_monitoring | 10.60.203.0/24 | network visibility / packet analysis |
| 204 | management | 10.60.204.0/24 | management, jump host, range control |

Firewall rules (template:36-61). **OBSERVED**: attacker-to-victim allow ("attacks must be
visible to monitoring"); victim-to-attacker allow on ports [80, 443, 53, 8080] ("C2 callback
channels"); monitoring-mirror action `mirror` from `*` ("full packet capture via SPAN");
soc-to-victim allow; soc-to-monitoring allow.

Addressing invariants to accept:
- fw01 holds the .1 gateway of every VLAN (template:286-296). **OBSERVED**
- seconion01 is the only dual-homed node: management NIC on soc_tools, monitor NIC on
  network_monitoring receiving the mirror; `so_allow: [management]` restricts who reaches
  its console (template:234-238). **OBSERVED**
- The courses' implicit routing (jump01 reaching every VLAN's consoles, dfir-ws01 reaching
  all victim hosts) works through the soc-to-victim and management rules above. **INFERRED**

## 3. Accounts the courses assume

Template accounts block, template:304-355 (cites ADR 0012 §12). **OBSERVED**:

- Passwords generated per range, shown sealed on the Credentials panel.
- Windows hosts: built-in `Administrator` (Sysprep); also first domain admin once v-dc01 is
  promoted (template:319-320).
- Linux admin: `ubuntu` (`kali` on c2-server).
- Student-visible `analyst` (role: user): siem01, velociraptor01, thehive01, misp01,
  dfir-ws01, jump01 (template:330-354). jump01's is "the way in: SSH or Guacamole"
  (template:354).
- seconion01: `tnadmin` (OS admin via its per-range so-setup, ADR 0012 §8) and
  `analyst@range.local` — the Security Onion Console web login, which must be an e-mail
  address (template:345-348, per worker/provisioners/vsphere_securityonion.py).
- Not listed: the pfSense firewall's appliance config login (template:308).
- "OpenSearch Dashboards logins are scenario content" (template:331).

What the courses assume (all **OBSERVED** at cited lines):

- An `analyst` OS account on jump01 for SSH logon is step 1 of nearly every course —
  c204:57, c208:60, c209:60, c301 implicit (:59-60), up-cti-04:52, up-dfi-02:52,
  up-dfi-04:48, up-dfi-06:48, up-ir-03:51, up-net-03:49-51, up-soc-01:48,
  up-soc-02:51, up-soc-05:49.
- An `analyst` account on dfir-ws01: rmc-c209:56 ("over SSH as the analyst account"),
  up-dfi-02:140 ("log on to dfir-ws01 with the analyst account").
- An `analyst` account on siem01: up-idm-05:53 ("log in to siem01 with the analyst
  account"), c206:60.
- An `analyst` web login on thehive01: c206:62-63; on misp01: up-cti-02:48.
- seconion01 console: `analyst@range.local` at `https://10.60.202.60` (template:348).
- A "standard user your instructor assigned" for Windows sign-in on v-ws01 (c204:63), and
  per-profile users `finance_user` on v-ws01 (up-idm-05:144,157) and `hr_user` on v-ws02
  (up-idm-05:239,252). The template's accounts block lists only `Administrator` on Windows
  hosts, all `student_visible: false` — **how a student obtains a non-admin Windows sign-in
  is UNKNOWN** (instructor-side provisioning is the only plausible route; do not assume a
  password source).

## 4. Services the course steps name

Per node, only what course steps actually use. All **OBSERVED** at cited lines.

- **jump01** — SSH logon (courses above); Guacamole as an alternative or browser start
  point (c207:59 "over SSH or Guacamole", up-soc-05:49); browser to siem01 Dashboards
  (up-soc-01:50-52, up-soc-02:51), velociraptor01 console (up-soc-04:50-51), thehive01
  (c207:59-62, up-soc-05:51-52), misp01 (up-cti-02:48, up-cti-04:52), seconion01 console
  (up-dfi-04:55-56), dfir-ws01 console (:58); onward SSH/shell to zeek01 (up-net-01:45-46,
  up-net-03:49-51), dfir-ws01 (c209:62-63, up-dfi-03:50), v-dc01 authentication entries
  (up-ir-03:506), v-ws01 clock/security log (up-ir-03:53,145).
- **siem01** — OpenSearch Dashboards, index pattern, Discover searches, alert view, rule
  editor, saved detections (up-soc-01:50-54, up-soc-02:53-57, up-idm-05:53,152,246,351,
  up-dfi-06:426-434, c204:60-61, up-soc-03:57-59). Sigma rule work on siem01 is module 2
  (up-soc-03:112-148 — write, convert and run the rule; refs `- sigma` at :157); c209:509
  writes a Sigma rule on siem01. `sigma_rules` appears as a siem01 service only in the
  template (template:186).
- **velociraptor01** — console from jump01, client check-ins, per-client collections
  (process listing, command-line, hunts), memory acquisition of v-ws01, export to
  dfir-ws01 (up-soc-04:50-57, up-dfi-02:55-61, up-dfi-06:243-249,432, up-ir-03:232,
  up-idm-05:62,360). Agent presence on victim workstations assumed (template:196).
- **thehive01** — web interface from jump01, cases, case tasks, observables, severity
  (c206:62-63, c207:59-64, up-soc-05:51-55, up-dfi-04:523-525, up-dfi-06:336-345,
  up-idm-05:547-556).
- **misp01** — web interface/events index from jump01 or dfir-ws01, indicators, decay
  windows, hash searches, event creation (up-cti-02:47-53, up-cti-04:52-58,
  up-dfi-03:499, up-dfi-04:528, up-dfi-06:63,249, c209, C556 per extraction).
- **dfir-ws01** — SSH/console from jump01, analysis tooling: volatility3 (up-dfi-02:143-152,
  up-dfi-04:436), YARA (up-dfi-02:319, up-dfi-03:493-496, up-dfi-04:442), radare2
  (up-dfi-03:316 — see §1 gap), hashing (up-dfi-06:53), disk image (c208:326) and memory
  image (c208:417) opened on dfir-ws01.
- **seconion01** — SOC console from jump01, sensor services on the monitor interface, rule
  management page with custom-rule storage, Suricata rules including editing a local rules
  file, Zeek connection logs (up-dfi-04:55-58,334-340, c209, up-net-03:355-357, C556).
- **zeek01** — shell from jump01, conn.log and other protocol logs (conn/dns/smb/ssl), log
  viewer, monitor interface state and packet counters, authentication-traffic records
  (up-net-01:45-52, up-net-03:48-55, up-idm-05:59,255, up-dfi-04:244-247, up-ir-03:234,
  c209).
- **pcap01** — capture browsing for destinations/ports (up-dfi-04:250, C513 per
  extraction).
- **log-agg01** — inbound feeds and forwarded event types (c204:58-59); clock readings for
  time-sync checks (up-ir-03:58).
- **v-dc01** — local security policy audit settings, authentication/ticket events,
  DHCP address leases (up-idm-05:56,152-154,351,450-456, up-ir-03:506, c204:130,144). The
  dhcp service exists on the node (template:109).
- **v-fs01** — share/service reach per account (up-idm-05:453), share access entries
  (up-ir-03:240), pre-staged share files exist (template:119-122).
- **v-web01** — HTTP page requests as traffic source (up-net-03:56, C515 per extraction).
- **v-ws01..v-ws04** — Windows workstations: security log/policy access (c208:62-65,
  up-soc-03:54-56), sign-in activity (c204:63, up-idm-05:157,252,357), detonation/hunt
  target (up-dfi-04:151-154, up-dfi-06:243), velociraptor agents (template:196).
- **fw01** — rule list review/export (up-dfi-04:51, c209:66, c2-server rule connectivity).
- **v-mail01** — named as a victim host that must answer (rmc-c209:57) and mail routing
  context (C515 per extraction); no step logs into it. **INFERRED** (low dependency)

Ports named anywhere in the 22 courses: none for management UIs; only 80/443/53/8080 inside
the firewall rule (template:45) and quiz options (up-dfi-04:101,290). Any listener-port
check therefore has **UNKNOWN** port values except where the URL itself carries the answer
(seconion01 `https://10.60.202.60`, template:348).

## 5. Post-build checks

Acceptance checks a builder can run. Only source-citable expectations are listed; everything
not citable is **UNKNOWN** and must not be guessed into the runbook.

**OBSERVED**-grounded checks:

| Check | Expected | Source |
|---|---|---|
| Host answers | All nine victim-network hosts (v-dc01, v-fs01, v-web01, v-mail01, v-ws01..04) answer from dfir-ws01 | rmc-c209:57-59 (verbatim step) |
| Host answers | SOC platforms siem01, misp01, thehive01, velociraptor01 reachable from dfir-ws01 | rmc-c209:60-61; template IPs 10.60.202.x |
| Service listens | SSH on jump01 accepts the `analyst` account | template:268,354; step-1 of ~15 courses (§3) |
| Service listens | Guacamole on jump01 opens a browser session | template:268; c207:59, up-soc-05:49 |
| Service listens | OpenSearch Dashboards on siem01 opens from jump01 and Discover returns counts | up-soc-01:50-54, up-soc-02:51-57 |
| Service listens | Velociraptor console on velociraptor01 shows client check-ins, including v-ws01..04 | template:196; up-soc-04:50-52; up-idm-05:62 |
| Service listens | Security Onion Console web login at `https://10.60.202.60` with `analyst@range.local` | template:347-348 |
| Service listens | TheHive interface on thehive01 opens from jump01; prepared case present | c206:62-63; up-soc-05:51-52 |
| Service listens | MISP web interface on misp01 opens from jump01/dfir-ws01; events index lists entries | up-cti-02:47-51; up-cti-04:52-56 |
| Service listens | zeek01 reachable from jump01; conn.log files exist for the lab window | up-net-01:45-47 |
| Service listens | seconion01 sensor services (zeek, suricata, …) listening on the monitor interface | up-dfi-04:55-56; template:240 |
| Account logs in | `analyst` SSH on jump01, dfir-ws01; web/OS `analyst` on siem01, thehive01, misp01 | §3 citations |
| Account logs in | `analyst@range.local` into seconion01 console | template:348 |
| Data present | Pre-staged share files on v-fs01 (`payroll.xlsx`, `employees.csv`, `credentials.kdbx`) | template:119-122 |
| Data present | Velociraptor agents deployed on all victim workstations | template:196; up-soc-04:53 |
| Network | Mirror traffic reaches the monitor NIC (seconion01 sensor services see victim traffic) | template:48-52,234-237; up-net-03:52-55 |

**UNKNOWN** (cannot be derived from source — do not assert):

- Any TCP port for SSH, Dashboards, Velociraptor, TheHive, MISP, Guacamole beyond the
  default assumption; no course or template line states one. Port checks must be filled in
  by the author or measured on a live range (stop condition: needs a live range).
- How students receive the Windows "standard user" credentials (c204:63) — the template
  account list has no student-visible Windows account.
- Web-application logins inside OpenSearch Dashboards, Velociraptor, TheHive, MISP
  (template:331 says scenario content; per-course values are instructor content).
- NTP server identity: up-ir-03:39 teaches NTP synchronisation and up-ir-03:51-58 has
  students compare clocks across v-ws01/siem01/log-agg01, but no node in the template
  provides NTP. **PROPOSED**: ask the author whether v-dc01 or fw01 is the intended time
  source before writing a time-sync check.
- radare2 on dfir-ws01 (up-dfi-03:309) — pending the author-required decision (§1).

## 6. Courses using this template

22 courses declare `lab_range.template: soc-training`; each uses it in all 6 modules
(first-module `lab_range` line cited). "First lab step that would fail" names the earliest
step with a concrete template dependency. All **OBSERVED** at cited lines.

| Course file | Code / title | Module 1 lab | First lab step that would fail if the template were wrong |
|---|---|---|---|
| c204-security-monitoring-siem.yaml | C204 Security Monitoring & SIEM | :53 | :56-57 SSH logon to jump01 as the analyst account; :58-59 log-agg01 feed listing |
| c206-incident-response-foundations.yaml | C206 Incident Response Foundations | :54 | :57-58 jump01 terminal; then :59-61 siem01 analyst alerts, :62-64 thehive01 analyst cases |
| c207-incident-response-foundations.yaml | C207 Incident Response Foundations | :54 | :58-59 jump01 "over SSH or Guacamole" + TheHive console from jump01 |
| c208-windows-forensics.yaml | C208 Windows Forensics | :56 | :59-60 jump01 SSH analyst; :61-62 v-ws01 registry hive paths + Security event log |
| c209-malware-analysis-101.yaml | C209 Malware Analysis 101 | :56 | :59-61 jump01 SSH analyst; :62-64 dfir-ws01 interface addresses; :65-67 fw01 rule export |
| c301-advanced-threat-hunting.yaml | C301 Advanced Threat Hunting | :56 | :59-60 jump01; :61-63 siem01 telemetry indices; :64-66 velociraptor01 clients |
| rmc-c209-cyber-threat-intelligence.yaml | RMC C209 Cyber Threat Intelligence | :52 | :55-56 dfir-ws01 SSH analyst; :57-59 all nine victim hosts answer from dfir-ws01 |
| up-cti-02-ioc-management-pivoting.yaml | C556 IOC Management & Pivoting | :44 | :47-48 jump01, open the MISP web interface on misp01 |
| up-cti-04-intel-led-detection-hunt-operations.yaml | C558 Intel-Led Detection & Hunt Operations | :48 | :51-53 jump01 SSH, browser to the threat intelligence platform on misp01 |
| up-dfi-02-memory-forensics-with-volatility.yaml | C526 Memory Forensics with Volatility | :49 | :52 jump01 SSH analyst; :55 velociraptor01 memory acquisition of v-ws01 |
| up-dfi-03-static-analysis-beyond-the-basics.yaml | C527 Static Analysis Beyond the Basics | :44 | :47 jump01 analyst; :50 jump01→dfir-ws01 connect; module 4 :316-317 radare2 (unresolved, §1) |
| up-dfi-04-dynamic-malware-detonation.yaml | C528 Dynamic Malware Detonation | :45 | :48 jump01 SSH analyst; :51 fw01 rule list; :55 seconion01 console sensor services |
| up-dfi-06-malware-capability-assessment-capstone.yaml | C530 Malware Capability Assessment Capstone | :45 | :48 jump01 SSH analyst; :50-51 dfir-ws01 triage folder; :243 v-ws02 collection |
| up-idm-05-identity-detection-engineering.yaml | C511 Identity Detection Engineering | :50 | :53 siem01 analyst login + Dashboards indices; :56 v-dc01 local security policy |
| up-ir-03-incident-timeline-reconstruction.yaml | C533 Incident Timeline Reconstruction | :48 | :51 jump01 SSH; :53 v-ws01 clock from jump01; :58 log-agg01 clock (NTP unknown, §5) |
| up-net-01-packet-analysis-deep-dive.yaml | C513 Packet Analysis Deep Dive | :42 | :45-47 jump01 shell on zeek01, list conn.log files |
| up-net-03-network-monitoring-with-zeek-suricata.yaml | C515 Network Monitoring with Zeek & Suricata | :45 | :48-51 jump01 SSH, onward to zeek01; :52-55 monitor interface state/counters |
| up-soc-01-siem-query-craft.yaml | C519 SIEM Query Craft | :44 | :47-49 jump01 SSH analyst + browser; :50-52 OpenSearch Dashboards on siem01, index pattern |
| up-soc-02-alert-triage-under-pressure.yaml | C520 Alert Triage Under Pressure | :47 | :50-52 jump01 SSH, browser to siem01 Dashboards; :53-55 alert view by rule |
| up-soc-03-detection-engineering-with-sigma-att-ck.yaml | C521 Detection Engineering with Sigma & ATT&CK | :48 | :51-53 jump01; :54-56 v-ws01 local security policy; :57-59 siem01 indices |
| up-soc-04-endpoint-telemetry-edr-analysis.yaml | C522 Endpoint Telemetry & EDR Analysis | :47 | :50-52 jump01 SSH → velociraptor01 console, client check-in count |
| up-soc-05-soar-automation-for-analysts.yaml | C523 SOAR & Automation for Analysts | :45 | :48-50 jump01 over SSH or Guacamole with the analyst account; :51-53 thehive01 case |

Cross-course summary. **INFERRED** from the table above: the template is load-bearing
first through jump01 (SSH/Guacamole + `analyst`), then siem01 Dashboards, velociraptor01,
thehive01, misp01, dfir-ws01, zeek01 and seconion01; no course logs into c2-server,
phish-server, staging01, redir01 or v-mail01. The closest touches are a connectivity test,
not a sign-in: c209:68-70 has dfir-ws01 attempt one connection to c2-server in the
attacker network and record whether it succeeds, and rmc-c209:57-59 requires all nine
victim-network hosts (including v-mail01) to answer from dfir-ws01; phish-server,
staging01 and redir01 have no step touching them at all. **OBSERVED** (both steps
verbatim; no course file names phish-server, staging01 or redir01).
