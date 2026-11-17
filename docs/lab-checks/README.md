# Lab checks — which range template does each course use?

Companion index for the per-template checklists in this directory (`<template>.md`,
landing in sibling commits of card #478). This README answers, from the course corpus
alone: which of the eight range templates each course's labs run on, and where a lab
session starts.

## Method (how this table was derived)

OBSERVED — every fact below was derived by reading the course and template YAML in this
repository (a read-only `yaml.safe_load` sweep over `content/courses/*.yaml`, plus
`grep -n 'template:'` for the line citations). No range was provisioned and no lab was
run; nothing here is a claim about runtime behaviour. Facts that cannot be derived from
the YAML are marked UNKNOWN, not guessed.

OBSERVED — a course declares its range in each module: `modules[].lab_range.template`
names a directory under `content/ranges/`, and `modules[].lab_range.steps[].hosts` names
node ids from that template's `template.yaml` (see `content/ranges/soc-training/template.yaml:11`
for `vm_count`, and the `nodes:` list in each template). The contract test
`tests/contracts/test_lab_check_docs.py` asserts that every host named in every step of
every course exists as a node id in that course's template — it passes at the commit this
README documents (104 passed, 8 xfailed: the 8 xfail is the checklist-files check, which
waits on the sibling commits).

## Summary

All 104 course files in `content/courses/` (the count the schema test asserts,
`tests/contracts/test_course_content_schema.py:84`) parse as courses. 97 of them carry
`lab_range` on all 6 modules; every one of those 97 uses exactly one template throughout
(no mixed-template course). 7 courses carry no `lab_range` at all — the held set.

| Template | Courses | Lab modules (courses × 6) |
|---|---|---|
| soc-training | 22 | 132 |
| small-enterprise | 20 | 120 |
| medium-enterprise | 18 | 108 |
| large-enterprise | 13 | 78 |
| cloud-security | 10 | 60 |
| red-team | 5 | 30 |
| greyspace-internet | 5 | 30 |
| red-vs-blue | 4 | 24 |
| **Total** | **97** | **582** |

Held — 7 courses, not listed under any template because their drafts contain no
`lab_range` (OBSERVED by grep of the seven files for `lab_range`/`template:`, which
returns nothing): **C302, C305, C306, RMC C202, RMC C205, RMC C207, RMC C211**
(`scripts/check_course.py:86-95` holds these codes from drafting). Which templates they
will use once authored is UNKNOWN.

## Per-template course list

For each course: the module-1 lab step that first touches the template's hosts — the
step's `hosts` and a gist of its `action` (truncated at ~90 characters; the full text is
in the course file). Every course's first lab module is ordinal 1 (OBSERVED in the sweep),
so "module 1" is the first lab step everywhere. The `file:line` citation points at the
module-1 `template:` declaration; all 6 modules of the file declare the same template
(OBSERVED in the sweep: each file's six `template:` lines are identical, 6 per file).

### soc-training — 22 courses / 132 lab modules

| Course | Declared at | Module 1 hosts | First step action (gist) |
|---|---|---|---|
| C204 | c204-security-monitoring-siem.yaml:54 | jump01 | Log on to jump01 over SSH as the analyst account, open a terminal and record the start tim |
| C206 | c206-incident-response-foundations.yaml:55 | jump01 | Log on to jump01, open a terminal and record the start time in the lab notes. |
| C207 | c207-incident-response-foundations.yaml:55 | jump01 | Log on to jump01 over SSH or Guacamole, open the TheHive console and note the start time. |
| C208 | c208-windows-forensics.yaml:57 | jump01 | Log on to jump01 over SSH with the analyst account, open a terminal and note the start tim |
| C209 | c209-malware-analysis-101.yaml:57 | jump01 | Log on to jump01 over SSH with the analyst account your instructor assigned, open a termin |
| C301 | c301-advanced-threat-hunting.yaml:57 | jump01 | Log on to jump01, open a terminal and record the start time in the lab notes. |
| RMC C209 | rmc-c209-cyber-threat-intelligence.yaml:53 | dfir-ws01 | Log on to dfir-ws01 over SSH as the analyst account the instructor assigned and open a ter |
| C519 | up-soc-01-siem-query-craft.yaml:45 | jump01 | Log on to jump01 over SSH with the instructor-assigned analyst account and open a browser |
| C520 | up-soc-02-alert-triage-under-pressure.yaml:48 | jump01 | Log in to jump01 over SSH, open a browser session to the OpenSearch Dashboards on siem01 a |
| C521 | up-soc-03-detection-engineering-with-sigma-att-ck.yaml:49 | jump01 | Log on to jump01, open the lab notes and write down one credential-access technique from A |
| C522 | up-soc-04-endpoint-telemetry-edr-analysis.yaml:48 | jump01, velociraptor01 | Log on to jump01 over SSH, open the Velociraptor console on velociraptor01 and record how |
| C523 | up-soc-05-soar-automation-for-analysts.yaml:46 | jump01 | Log on to jump01 over SSH or Guacamole with the analyst account, open a browser and note t |
| C526 | up-dfi-02-memory-forensics-with-volatility.yaml:50 | jump01 | Log on to jump01 over SSH with the analyst account the instructor assigned, open a termina |
| C527 | up-dfi-03-static-analysis-beyond-the-basics.yaml:45 | jump01 | Log on to jump01 with the analyst account, open a terminal and write the start time in the |
| C528 | up-dfi-04-dynamic-malware-detonation.yaml:46 | jump01 | Log on to jump01 over SSH as the analyst account, open a terminal and write the start time |
| C530 | up-dfi-06-malware-capability-assessment-capstone.yaml:46 | jump01 | Log on to jump01 over SSH with the analyst account your instructor assigned and open a ter |
| C533 | up-ir-03-incident-timeline-reconstruction.yaml:49 | jump01 | Log on to jump01 over SSH, open a terminal and record the start time in the lab notes. |
| C511 | up-idm-05-identity-detection-engineering.yaml:51 | siem01 | Log in to siem01 with the analyst account, open Dashboards and list the indices that hold |
| C513 | up-net-01-packet-analysis-deep-dive.yaml:43 | jump01, zeek01 | From jump01, open a shell on zeek01 and list the conn.log files for the lab window, record |
| C515 | up-net-03-network-monitoring-with-zeek-suricata.yaml:46 | jump01 | Log on to the jump host (jump01) over SSH, open a terminal and connect onward to the Zeek |
| C556 | up-cti-02-ioc-management-pivoting.yaml:45 | jump01 | Log on to jump01, open the MISP web interface on misp01 and open the events index. |
| C558 | up-cti-04-intel-led-detection-hunt-operations.yaml:49 | jump01 | Log on to jump01 over SSH as the analyst account, open a browser to the threat intelligenc |

### small-enterprise — 20 courses / 120 lab modules

| Course | Declared at | Module 1 hosts | First step action (gist) |
|---|---|---|---|
| C101 | c101-computer-architecture-systems.yaml:53 | jump-box | Log on to jump-box over SSH with the analyst account your instructor assigned, open a term |
| C102 | c102-operating-system-fundamentals.yaml:51 | jump-box | Log on to jump-box over SSH with the analyst account, open two terminals and note the star |
| C103 | c103-intro-to-networking.yaml:51 | jump-box | Log on to jump-box over SSH, open a terminal and write the start time in the lab notes. |
| C104 | c104-intro-to-programming-python.yaml:53 | jump-box | Log on to jump-box over SSH, open a terminal and record the Python version python3 prints. |
| C105 | c105-foundations-of-cybersecurity.yaml:51 | jump-box | Log on to jump-box over SSH, open a terminal and record the start time in the lab notes. |
| C203 | c203-identity-access-management.yaml:55 | jump-box | Log on to jump-box over SSH and note the start time in the lab record. |
| C211 | c211-lab-simulated-incident-exercise.yaml:57 | jump-box | Log on to jump-box over SSH, open a terminal and record the exercise design start time. |
| C555 | up-cti-01-threat-intelligence-foundations.yaml:50 | jump-box | Log on to jump-box over SSH with the analyst account the instructor assigned and record th |
| C557 | up-cti-03-threat-actor-profiling-with-att-ck.yaml:48 | jump-box | Log on to jump-box over SSH as the analyst account the instructor assigned, open a termina |
| C560 | up-cti-06-strategic-intelligence-for-leadership.yaml:46 | jump-box | Log on to jump-box over SSH as the analyst account, open a terminal and record the hosts o |
| C525 | up-dfi-01-linux-forensics.yaml:49 | jump-box | Log on to jump-box over SSH, open a terminal and record the system date, the time zone and |
| C529 | up-dfi-05-ransomware-incident-forensics.yaml:50 | jump-box | Log on to jump-box over SSH, open the exercise timeline for the ransomware-lite run and re |
| C501 | up-gov-01-foundations-of-security-governance.yaml:46 | jump-box | Log on to jump-box over SSH, open a terminal and write the start time in your lab notes. |
| C502 | up-gov-02-risk-register-craft-assessing-prioritising-risk.yaml:51 | jump-box | Log on to jump-box over SSH, open a terminal and note the start time. |
| C506 | up-gov-06-security-metrics-executive-communication.yaml:45 | jump-box | Log on to jump-box over SSH, open a terminal and write the module start time in your lab n |
| C531 | up-ir-01-first-responder-playbooks.yaml:51 | jump-box | Log on to jump-box over SSH, open a terminal and write the session start time as the first |
| C532 | up-ir-02-live-triage-containment-lab.yaml:48 | jump-box | Log in to jump-box over SSH as the assigned analyst account, open the exercise alert feed |
| C534 | up-ir-04-crisis-communication-stakeholder-management.yaml:48 | jump-box | Log on to jump-box over SSH, open a terminal and note the date and start time of the lab. |
| C535 | up-ir-05-tabletop-exercise-facilitation.yaml:50 | jump-box | Log on to jump-box over SSH, open a terminal and record the start time that anchors the in |
| C554 | up-ot-06-emerging-tech-risk-clinic-ai-saas-edge.yaml:50 | jump-box | Log on to jump-box over SSH as the analyst account, note the start time in the lab notes a |

### medium-enterprise — 18 courses / 108 lab modules

| Course | Declared at | Module 1 hosts | First step action (gist) |
|---|---|---|---|
| C106 | c106-cryptography-basics.yaml:51 | web01 | Log on to web01 as the account the instructor assigned, open a terminal and note the start |
| C108 | c108-linux-system-administration.yaml:56 | web01 | Log on to web01 over SSH with the account your instructor assigned and record the journalc |
| C109 | c109-network-services-protocols.yaml:59 | siem01 | Log on to siem01 with the analyst account your instructor assigned, open a terminal and no |
| C110 | c110-intro-to-risk-management.yaml:55 | siem01 | Log on to siem01 as the analyst account your instructor assigned, open a terminal and reco |
| C201 | c201-network-defense-firewalls.yaml:57 | ws01 | Log on to ws01 with the account your instructor assigned, open a terminal and note the sta |
| C205 | c205-lab-hardened-lab-network.yaml:55 | siem01 | Log on to siem01 as the analyst account the instructor assigned, open a terminal and note |
| C401 | c401-professional-ethics-legal-frameworks.yaml:54 | ws01 | Log on to ws01 with the account your instructor assigned and record the memo's authorised |
| C402 | c402-capstone-project-planning.yaml:53 | siem01 | Log on to siem01 as the analyst user your instructor assigned, open a terminal and note th |
| C403 | c403-capstone-implementation.yaml:52 | siem01 | Log on to siem01 as the analyst user your instructor assigned and open a terminal for the |
| C404 | c404-capstone-assessment-presentation.yaml:53 | siem01 | Log on to siem01 with the analyst account, open a terminal and record the login time. |
| C503 | up-gov-03-policy-to-firewall-rule-making-policy-enforceabl.yaml:45 | ws01 | Log on to ws01 with the account your instructor assigned, open the policy extract and read |
| C505 | up-gov-05-audit-assessment-continuous-improvement.yaml:49 | siem01 | Log on to siem01 with the analyst account the instructor assigned and record the list of h |
| C507 | up-idm-01-authentication-protocols-hands-on.yaml:48 | siem01 | Log on to siem01 as the analyst account the instructor assigned, open a terminal and note |
| C509 | up-idm-03-zero-trust-architecture-in-practice.yaml:48 | ws01 | Log on to ws01 as the standard user your instructor assigned and open a terminal. |
| C510 | up-idm-04-privileged-access-secrets-management.yaml:48 | ws01 | Log on to ws01 with the administrator account your instructor assigned, open a terminal an |
| C514 | up-net-02-firewall-segmentation-engineering.yaml:47 | siem01 | Log on to siem01 with the analyst account the instructor assigned, open a terminal and not |
| C536 | up-ir-06-recovery-operations-business-restoration.yaml:47 | ws01 | Log on to ws01, open a command prompt and record the start time in the lab notes. |
| C538 | up-red-02-vulnerability-assessment-method.yaml:47 | siem01 | Log on to siem01 in the management zone with the analyst account the instructor assigned a |

### large-enterprise — 13 courses / 78 lab modules

| Course | Declared at | Module 1 hosts | First step action (gist) |
|---|---|---|---|
| C202 | c202-secure-communications-tls-vpn.yaml:55 | jump01 | Log on to jump01 over SSH, open a terminal and write the start time in the lab notes. |
| C212 | c212-advanced-incident-response.yaml:56 | jump01 | Log on to jump01, open the case record your instructor assigned and note the start time. |
| C304 | iot-security-foundations.yaml:58 | jump01 | Log on to jump01, the management jump host, open a terminal and record the start time. |
| RMC C204 | rmc-c204-iot-firmware-reverse-engineering.yaml:52 | jump01 | Log on to jump01 and open an SSH session to the analysis host ansible01. |
| RMC C206 | rmc-c206-capstone-planning.yaml:52 | jump01 | Log on to jump01, confirm reachability into the corporate LAN and the server VLAN, and rec |
| RMC C208 | rmc-c208-incident-response-end-to-end.yaml:55 | jump01 | Log on to jump01, open the incident queue on soar01 and record the start time and each ope |
| RMC C212 | rmc-c212-capstone-execution-assessment.yaml:55 | jump01 | Log on to jump01 over SSH, open a terminal, start the lab notebook and record the date and |
| C516 | up-net-04-resilient-network-architecture.yaml:48 | jump01 | Log on to jump01 over SSH, open a terminal and write the start time in the lab notes. |
| C518 | up-net-06-network-defense-capstone-defend-the-enterprise.yaml:47 | jump01 | Log on to jump01 over RDP, open the fw01 dashboard, and write the start time in the lab no |
| C549 | up-ot-01-ics-scada-fundamentals.yaml:46 | eng-ws01 | Log on to eng-ws01, open the controller programming application and record which project i |
| C550 | up-ot-02-ot-network-monitoring-passive-discovery.yaml:50 | jump01 | Log on to jump01 with the account your instructor assigned, open a terminal and write the |
| C551 | up-ot-03-ot-risk-assessment-zone-conduit-design.yaml:48 | jump01, vuln01 | Log on to jump01 and open the vuln01 scanner console; confirm a scan of the server VLAN ho |
| C553 | up-ot-05-ot-incident-response-failover-safety.yaml:49 | jump01 | Log on to jump01, open the monitoring dashboards and note the start time in the lab record |

### cloud-security — 10 courses / 60 lab modules

| Course | Declared at | Module 1 hosts | First step action (gist) |
|---|---|---|---|
| C107 | c107-secure-software-development.yaml:55 | jump01 | Log on to jump01 over SSH, open a terminal and note the start time in the lab record. |
| C303 | c303-cloud-security-aws-azure.yaml:57 | jump01 | Log on to jump01 over SSH, open a terminal and note the start time in the lab notes. |
| RMC C203 | rmc-c203-cloud-security-advanced.yaml:51 | jump01 | Log on to jump01 over SSH, open a terminal and record the start time in the lab notes. |
| RMC C210 | rmc-c210-cloud-native-defense.yaml:55 | jump01 | Log on to jump01 over SSH, open a terminal and note the start time in the lab notes. |
| C543 | up-cld-01-cloud-iam-misconfiguration-hunting.yaml:48 | jump01 | Log on to jump01 over SSH, open a terminal and note the start time in your lab notes. |
| C544 | up-cld-02-container-kubernetes-security.yaml:49 | jump01 | Log on to jump01 over SSH, open a terminal and record the start time. |
| C545 | up-cld-03-ci-cd-pipeline-security.yaml:46 | jump01 | Log on to jump01 over SSH and write the start time in the lab notes. |
| C546 | up-cld-04-cloud-logging-detection.yaml:45 | jump01 | Log on to jump01, open a shell with the cloud command-line tool pointed at the simulated a |
| C547 | up-cld-05-iac-security-policy-as-code.yaml:49 | jump01 | Open an SSH session to jump01, confirm the cloud and Terraform command line tools respond, |
| C548 | up-cld-06-cloud-incident-response.yaml:51 | jump01 | Log on to jump01 over SSH, open a terminal and note the start time. |

### red-team — 5 courses / 30 lab modules

| Course | Declared at | Module 1 hosts | First step action (gist) |
|---|---|---|---|
| C508 | up-idm-02-active-directory-attack-defense.yaml:45 | a-kali01 | Log on to a-kali01 over SSH, open the graph tool already installed there and start a read- |
| C537 | up-red-01-web-exploitation-fundamentals.yaml:47 | a-kali01, t-dns01 | Log on to a-kali01, resolve the host names of t-web01 and t-web02 against t-dns01 and reco |
| C539 | up-red-03-c2-infrastructure-operational-security.yaml:46 | a-kali01 | Log on to a-kali01 over SSH, open a terminal and write the start time in your lab notes. |
| C540 | up-red-04-adversary-emulation-planning-att-ck-based.yaml:47 | a-kali01 | Open a terminal on a-kali01, write the start time in the lab notes and open the drafted ad |
| C541 | up-red-05-post-exploitation-lateral-movement-lab.yaml:52 | a-kali01 | Log on to a-kali01 in the attacker segment and record the starting point and time in the l |

### greyspace-internet — 5 courses / 30 lab modules

| Course | Declared at | Module 1 hosts | First step action (gist) |
|---|---|---|---|
| C504 | up-gov-04-supply-chain-third-party-risk-management.yaml:49 | ws01 | Log on to ws01, open the installed-applications list and record every application with its |
| C512 | up-idm-06-federated-identity-b2b-access-design.yaml:47 | ws01 | Log on to ws01, open the browser and record the start time in the lab notes. |
| C517 | up-net-05-protocol-service-hardening-clinic.yaml:48 | ws01 | Log on to ws01, open a command prompt and record the resolver address the workstation is c |
| C552 | up-ot-04-iot-device-security-testing.yaml:47 | ws01 | On ws01, open the vendor update page for the office router that the instructor names on th |
| C559 | up-cti-05-disinformation-open-source-threat-monitoring.yaml:51 | ws01 | Log on to ws01, open the browser and reach the simulated internet through the office netwo |

### red-vs-blue — 4 courses / 24 lab modules

| Course | Declared at | Module 1 hosts | First step action (gist) |
|---|---|---|---|
| C210 | c210-threat-modeling-att-ck.yaml:56 | jump01 | Log on to jump01 over SSH and write the start time in your lab notes. |
| RMC C201 | rmc-c201-advanced-network-defense.yaml:51 | jump01 | Log on to jump01, confirm which network zones it can reach, and record the path the sessio |
| C524 | up-soc-06-purple-team-operations.yaml:45 | jump01 | Log on to jump01, open the engagement worksheet and record the scope and rules of engageme |
| C542 | up-red-06-full-adversary-emulation-capstone.yaml:49 | jump01 | Log on to jump01 over SSH, take control of red-op01 through the RDP gateway and record the |

## Host naming per template

OBSERVED — the module-1 hosts above (and the full node lists in each
`content/ranges/<template>/template.yaml`) show the two naming styles: small-enterprise
uses hyphenated ids (`jump-box`, `dc-01`, `ws-01`, `fw-01`); the other seven templates use
compact ids (`jump01`, `siem01`, `ws01`, `a-kali01`, `dfir-ws01`, `eng-ws01`, …). The
contract test enforces that every host named anywhere in a course's steps is a node of the
declared template, so a step cannot mix the two styles undetected.

## What this README does not claim

- UNKNOWN — the per-template checklists themselves: `docs/lab-checks/<template>.md` for
  the eight templates land in sibling commits of card #478. The contract test's
  checklist check is marked `xfail(strict=False)` until they are committed.
- UNKNOWN — which templates the 7 held courses will use once their labs are authored.
- UNKNOWN — runtime behaviour (provisioning, snapshot, scenario execution). This README
  derives only from the static YAML; nothing here was executed against a range.
- Deeper lab steps (modules 2-6) name further hosts from the same template; they are
  covered by the contract test but not tabulated here.
