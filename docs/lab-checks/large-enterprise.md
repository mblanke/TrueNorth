# Acceptance checklist — range template `large-enterprise`

Derived from source only (no live range was available). Base commit `d5851f6d649a55b54f33e700be9d28be399e1302`,
written 2026-11-07. Sources read in full:

- `content/ranges/large-enterprise/template.yaml` (the template)
- every course in `content/courses/*.yaml` whose `lab_range.template` is `large-enterprise` — 13 courses,
  6 modules each, one lab per module (78 lab modules), confirmed by `grep -c` over each file
- `content/scenarios/apt-nation-state/scenario.yaml`, which 5 of those lab modules reference by name

Labels: **OBSERVED** = read at the cited location. **INFERRED** = reasoned from what was read.
**UNKNOWN** = not derivable from source; do not guess it on site. No username, password, listen port or
command appears below unless it is cited; everything else is marked UNKNOWN.

---

## 1. Nodes

50 nodes. OBSERVED at `content/ranges/large-enterprise/template.yaml:101-589` (every row below comes from
that block). `vm_count: 50` at `template.yaml:11` is the field
`tests/contracts/test_template_vm_count.py` enforces against the worker's rendered topology.

| id | OS | role | VLAN | IP | template-declared services |
|---|---|---|---|---|---|
| jump01 | windows-server-2022 | jump_host | management | 10.50.105.10 | rdp, ssh |
| ansible01 | ubuntu-2404 | automation | management | 10.50.105.20 | ansible, ssh |
| fw01 | pfsense | firewall (edge: true) | management (+7 interfaces, §2) | 10.50.105.1 | firewall, nat, vpn |
| ntp01 | ubuntu-2404 | time_server | management | 10.50.105.30 | chrony |
| web01 | ubuntu-2404 | web_server | dmz | 10.50.100.10 | nginx, php, tls |
| web02 | ubuntu-2404 | web_server | dmz | 10.50.100.11 | apache, tomcat, tls |
| mail-relay01 | ubuntu-2404 | mail_relay | dmz | 10.50.100.20 | postfix, spamassassin |
| dns-ext01 | ubuntu-2404 | dns_server | dmz | 10.50.100.30 | bind9 |
| vpn01 | pfsense | vpn_gateway | dmz | 10.50.100.40 | openvpn, ipsec |
| dc01 | windows-server-2022 | domain_controller | corporate_lan | 10.50.101.10 | active_directory, dns, dhcp, gpo |
| dc02 | windows-server-2022 | domain_controller | corporate_lan | 10.50.101.11 | active_directory, dns |
| fs01 | windows-server-2022 | file_server | corporate_lan | 10.50.101.20 | smb, dfs, vshadow |
| print01 | windows-server-2022 | print_server | corporate_lan | 10.50.101.21 | print_spooler |
| ws01–ws08 | windows-11 | workstation | corporate_lan | 10.50.101.100–107 | — (user_profile each, §4) |
| exch01 | windows-server-2022 | mail_server | server_vlan | 10.50.102.10 | exchange_2019, owa, smtp, imap |
| sp01 | windows-server-2022 | collaboration | server_vlan | 10.50.102.20 | sharepoint_2019, iis |
| sql01 | windows-server-2022 | database | server_vlan | 10.50.102.30 | mssql_2022, ssrs |
| sql02 | windows-server-2022 | database | server_vlan | 10.50.102.31 | mssql_2022 (replication_partner: sql01) |
| erp01 | windows-server-2022 | application | server_vlan | 10.50.102.40 | iis, dotnet_runtime |
| ca01 | windows-server-2022 | pki | server_vlan | 10.50.102.50 | adcs, ocsp |
| wsus01 | windows-server-2022 | patch_management | server_vlan | 10.50.102.60 | wsus |
| sccm01 | windows-server-2022 | endpoint_management | server_vlan | 10.50.102.70 | sccm, iis |
| siem01 | ubuntu-2404 | siem | security_vlan | 10.50.103.10 | opensearch, opensearch_dashboards, filebeat |
| ids01 | ubuntu-2404 | ids_ips | security_vlan + monitor NIC on network_monitoring | 10.50.103.20 | suricata, evebox |
| vuln01 | ubuntu-2404 | vulnerability_scanner | security_vlan | 10.50.103.30 | openvas, greenbone |
| edr01 | ubuntu-2404 | edr | security_vlan | 10.50.103.40 | velociraptor_server |
| soar01 | ubuntu-2404 | soar | security_vlan | 10.50.103.50 | thehive, cortex |
| tip01 | ubuntu-2404 | threat_intel | security_vlan | 10.50.103.60 | misp |
| syslog01 | ubuntu-2404 | log_collector | security_vlan | 10.50.103.70 | rsyslog, logstash |
| historian01 | windows-server-2019 | historian | ot_scada | 10.50.104.10 | osisoft_pi_sim |
| hmi01 | windows-10-ltsc | hmi | ot_scada | 10.50.104.20 | scada_hmi_sim |
| hmi02 | windows-10-ltsc | hmi | ot_scada | 10.50.104.21 | scada_hmi_sim |
| plc01 | ubuntu-2404 | plc | ot_scada | 10.50.104.30 | openplc, modbus_tcp |
| plc02 | ubuntu-2404 | plc | ot_scada | 10.50.104.31 | openplc, modbus_tcp |
| eng-ws01 | windows-10-ltsc | engineering_workstation | ot_scada | 10.50.104.40 | plc_programming_sim |
| ot-fw01 | pfsense | firewall | ot_scada | 10.50.104.1 | firewall, ids_passthrough (single NIC, §2) |
| dc03 | windows-server-2022 | domain_controller | ad_forest_b | 10.50.106.10 | active_directory, dns |
| ws09 | windows-11 | workstation | ad_forest_b | 10.50.106.100 | — |
| ws10 | windows-11 | workstation | ad_forest_b | 10.50.106.101 | — |
| fs02 | windows-server-2022 | file_server | ad_forest_b | 10.50.106.20 | smb |
| aadconnect01 | windows-server-2022 | hybrid_identity | cloud_hybrid | 10.50.107.10 | azure_ad_connect_sim, password_hash_sync |
| appproxy01 | windows-server-2022 | app_proxy | cloud_hybrid | 10.50.107.20 | azure_app_proxy_sim |
| arc01 | ubuntu-2404 | cloud_management | cloud_hybrid | 10.50.107.30 | azure_arc_sim, log_analytics_agent |

ws01–ws08 row expands to: ws01 finance_analyst, ws02 finance_manager, ws03 hr_specialist,
ws04 software_engineer, ws05 devops_engineer, ws06 c_level_executive, ws07 it_administrator,
ws08 marketing_specialist (OBSERVED `template.yaml:207-277`).

Stale metadata: `resource_totals.total_vms: 54` (`template.yaml:592`) does not match the 50 nodes above;
`range_network_spec.md:18` repeats the 54. The contract test enforces only `vm_count: 50`
(`tests/contracts/test_template_vm_count.py:41-45`); the other totals in `resource_totals`
(cores, memory, disk) are OBSERVED as written but not independently recomputed — treat them as
UNKNOWN until a build measures them.

## 2. Networks and addressing

All OBSERVED at `template.yaml:13-99` unless cited otherwise.

| VLAN | name | CIDR | notes |
|---|---|---|---|
| 100 | dmz | 10.50.100.0/24 | internet-facing |
| 101 | corporate_lan | 10.50.101.0/24 | workstations, DCs, file/print |
| 102 | server_vlan | 10.50.102.0/24 | core applications and databases |
| 103 | security_vlan | 10.50.103.0/24 | SOC tooling |
| 104 | ot_scada | 10.50.104.0/24 | `isolated: true` (`template.yaml:38`); reached by nothing until the transit leg is built (issue #235, `template.yaml:35-37`) |
| 105 | management | 10.50.105.0/24 | jump hosts, out-of-band |
| 106 | ad_forest_b | 10.50.106.0/24 | subsidiary forest |
| 107 | cloud_hybrid | 10.50.107.0/24 | simulated hybrid segment |
| 108 | network_monitoring | 10.50.108.0/24 | `isolated: true`; SPAN only, no addresses (`template.yaml:51-57`) |

- **fw01** holds an interface on dmz (.1), corporate_lan (.1), server_vlan (.1), security_vlan (.1),
  management (.1), ad_forest_b (.1), cloud_hybrid (.1) — NOT on ot_scada
  (OBSERVED `template.yaml:557-571`). `edge: true` (`template.yaml:556`) is what makes fw01, not vpn01,
  the range edge.
- **ot-fw01** has one NIC holding only the OT gateway 10.50.104.1; its leg toward fw01 does not exist yet
  (OBSERVED `template.yaml:471-479`).
- **ids01** sniffing: second interface on VLAN 108 with `monitor: true`, no address
  (OBSERVED `template.yaml:371-376`); the `monitoring-mirror` rule copies every zone — OT included — to it
  (OBSERVED `template.yaml:87-91`).
- **Firewall rules** (`template.yaml:59-91`): dmz→server_vlan allow 443/8443/1433; corporate_lan→server_vlan
  allow 80/443/445/1433/389/636; corporate_lan→dmz **deny** (`template.yaml:70-73`);
  ot_scada→corporate_lan **deny**;
  security_vlan→`*` allow; management→`*` allow ports 22/3389/5985/5986; monitoring-mirror (SPAN).
- **AD trust relationships** (`template.yaml:93-99`): corp.truenorth.local ↔ subsidiary.truenorth.local
  (two-way); corp.truenorth.local → partner.external.local (one-way-incoming). partner.external.local has
  no nodes in the template — whether the deployed range provisions the trust objects is UNKNOWN.

## 3. Accounts the courses assume

- **No course step names a username, password or credential.** Every sign-in is "the account your
  instructor assigned": OBSERVED `up-ot-02-ot-network-monitoring-passive-discovery.yaml:52-54`
  (jump01), `up-ot-01-ics-scada-fundamentals.yaml:138-140` (operator account on hmi01),
  `rmc-c212-capstone-execution-assessment.yaml:147-149` (standard user on ws01/ws02),
  `rmc-c206-capstone-planning.yaml:234-236` (assigned standard account with an "account class").
  The actual credentials are an on-site provisioning concern — UNKNOWN from source.
- **Account classes the courses rely on** (INFERRED from the steps above): one standard (non-admin) domain
  user per Student workstation, one OT operator account for the HMIs, and administrative reachability from
  jump01 to dc01/fw01 management views. None is named in source.
- **`user_profile` fields** on ws01–ws08 (§1) are the only per-node account data in the template
  (OBSERVED `template.yaml:214, 223, 232, 241, 250, 259, 268, 277`). Whether a `user_profile` value
  provisions a loggable account is not derivable from `template.yaml` alone — UNKNOWN.
- **Scenario account**: `apt-nation-state` assumes `target_user: j.harper` at domain `corp.example`
  (OBSERVED `content/scenarios/apt-nation-state/scenario.yaml:32-34`, mailbox `…@{{ domain }}` at :43,
  profile path under `C:\Users\{{ target_user }}` at :61) with activity on ws06. The template's AD forest is
  `corp.truenorth.local` (`template.yaml:94`), not `corp.example`, and ws06's `user_profile` is
  `c_level_executive`, not a named j.harper. Whether the deployed range provisions j.harper on ws06:
  UNKNOWN — flag to the scenario owner before running the five scenario labs (§7).
- **AD forests**: `corp.truenorth.local` (dc01, dc02), `subsidiary.truenorth.local` (dc03),
  `partner.external.local` (trust target only, no hosts) — OBSERVED `template.yaml:94-99, 176, 187, 490`.

## 4. Services the course steps name

Every entry is OBSERVED at the cited step (host lists come from the same `steps` block).
Template service names appear in parentheses from `template.yaml` §1.

| Host | What course steps assume it provides | Cited in |
|---|---|---|
| jump01 | Logon session — the first lab step of 12 of 13 courses (up-ot-01's M1 instead starts on the eng-ws01 console, §6); the transport is named only in some courses — SSH in c202, rmc-c212, up-net-04; SSH-or-RDP in iot (M2 lab); RDP in up-net-06; lab notebook/workspace | c202:57-58; rmc-c212:58; up-net-04:50-51; iot-…foundations:146; up-net-06:49-50 |
| fw01 | readable rule set in order; zone-to-zone rules; per-rule ports; interfaces per zone; route corporate_lan↔server_vlan; DMZ-facing rules; no interface/rule toward the OT cell; rules mentioning the OT VLAN | up-net-04:52-60; up-net-06:51-53; up-ot-03:147-149; up-ot-05:148-150, 134-135 |
| web01 | a TLS service that reports negotiated protocol version, cipher suite, certificate dates/issuer/chain; HTTPS home page with status code and server header | c202:59-67, 150-155; up-net-04:233-235 |
| web02 | same TLS behaviour as a second client/second web server; a distinct web stack; TLS weaknesses a scanner can report | c202:62-67; up-net-04:233-241; c202:409-414 |
| ca01 | CA management view listing issued certificates (subject, expiry, chain, enrolment path); which services hold its certificates | c202:147-149, 501-502; iot-…foundations:235-237; rmc-c208:342-344 |
| vpn01 | offered VPN services and tunnel modes; a VPN session that assigns client addresses and installs routes | c202:234-239 |
| mail-relay01 | mail logs recording sender, recipient, size, time per message | c202:321-323 |
| exch01 | message logs holding fields for the same messages; a service a client can connect to and observe TLS (or not) | c202:324-326, 498-500 |
| dc01 | domain role/FSMO query; domain and forest names; trust listing with direction; accounts signing in and their source hosts; admin group membership; password policy values; OS of registered servers and workstations; sign-in/auth event log | rmc-c206:57-59, 326-328; up-net-06:54-56; rmc-c212:59-61, 415-417; up-ot-03:236-238; c212:160-162 |
| dc02 | a DNS Server service that can be stopped and restarted; second DC for single-homing checks; answerable name lookups | up-net-04:419-425, 144-146 |
| dc03 | benign reachability from jump01; the far end of the corp↔subsidiary trust and admin path | rmc-c206:66-68; up-net-06:517-519 |
| sql01 | replication-partner listing; service version, patch level, listening configuration; TCP 1433 reachable from ws01 (corporate_lan) and from web01 (dmz) | rmc-c206:148-149; up-net-04:56-57; up-ot-03:516-518 |
| sql02 | lists sql01 as replication partner; differs from sql01 in version/patch/listening | rmc-c206:148-149; up-net-04:147-149 |
| fs01 | shadow-copy listing with creation times; service records; a target a peer can execute a runbook against; scenario PsExec target | c212:342-343; rmc-c212:241-243; scenario.yaml:120-131 |
| ws01 | outbound connections to sql01 (1433) and toward plc01 (expected to fail at the zone boundary); DHCP/DNS servers it is configured to use | up-net-04:55-63; iot-…foundations:331-336 |
| ws02 | configured DNS servers; name lookup against dc02; second sign-in test source | up-net-04:416-423; rmc-c212:150-152 |
| ws06 | EDR process tree with parent process and command line; business-service records; reimage-or-clean decision; scenario victim host | c212:157-159; up-net-06:240-242, 337-339; scenario.yaml:53-57 |
| siem01 | dashboards per incident window; log-source inventory per zone; authentication-event search; OpenSearch dashboards served on siem01 itself; mail dashboards; saved queries; traffic records for OT addresses | c212:60-62; up-net-06:57-64; c202:327-329; rmc-c212:329-331; up-ot-02:434-442 |
| ids01 | monitor NIC receiving mirrored copies of **every** zone incl. OT; protocol view of mirrored segment traffic; per-asset protocols/ports; alert view; detection-rule source and rule writing; how far records reach back | up-ot-02:55-63; iot-…foundations:62-64, 337-339; up-ot-02:339-350 |
| edr01 | endpoint check-in inventory by zone; process tree per host; agent reporting per device-segment host; collected evidence; **cannot** reach hmi01 through the response console | c212:519-520; iot-…foundations:424-426; up-ot-05:334-336 |
| soar01 | case record/queue with workstreams, leads and states; case workflow steps; case history | c212:63-65; rmc-c208:57-58, 430-432; up-net-06:419-421 |
| tip01 | indicator records with confidence ratings; feed source-reliability/timeliness; whether chain indicators were known beforehand | c212:250-261; up-net-06:430-432 |
| vuln01 | scanner console with recent server-VLAN scan results (host, finding id, severity); scan of a TLS service; scan results for a named host | up-ot-03:50-55; c202:409-411; rmc-c212:326-328 |
| syslog01 | log-forwarder feeds and which hosts send them logs; the collector behind a siem01 export; absence of OT-cell feeds | iot-…foundations:517-519; c212:432-434; up-ot-05:59-61 |
| ntp01 | the time source it serves and which hosts synchronise against it | iot-…foundations:520-522 |
| ansible01 | SSH from jump01; staged lab directory with two firmware image files; signature-scan, disassembly, emulation and fuzzing tooling | rmc-c204:54-58, 148-152, 332-342 |
| historian01 | tag list with collection intervals; OS version and support status; writes kept updating during containment; first write after a node returns | up-ot-01:146-147; up-ot-03:239-241; up-ot-05:346-348, 443-444 |
| hmi01/hmi02 | operator logon; HMI screens and alarm list; process view staying live; display state around a failover | up-ot-01:138-145; up-ot-05:240-241, 337-342 |
| plc01/plc02 | go-online with the ladder program; holding-register polls with function codes and register addresses; control mode (auto/manual) switching; running configuration vs baseline | up-ot-01:51-58; up-ot-01:228-232; up-ot-05:242-249, 435-439 |
| eng-ws01 | controller programming application; packet capture on its OT interface; console access from the isolated OT cell (no management host can route to it) | up-ot-01:48-50, 226-227, 500-502 |
| ot-fw01 | rule editor (source, destination, action per rule); every segment it connects; the teaching point that the transit leg toward fw01 is not built; drill containment rule applied and reverted | up-ot-01:319-327; up-ot-05:145-147, 529-531, 544-546 |
| aadconnect01 | synchronisation records showing whether an affected account also exists in the cloud segment | rmc-c208:160-162, 249-250 |
| dns-ext01, print01, sp01, erp01, wsus01*, sccm01*, appproxy01, arc01, ws09, ws10, fs02 | **not named by any course step** in the 13 courses read. wsus01 and sccm01 appear only in `iot-security-foundations` (patch view :238-240, compliance view :241-243) — listed here because those labs depend on them | iot-…foundations:238-243 |

## 5. Post-build checks

Only what source cites. A check you cannot source is UNKNOWN — do not invent a port, command or
credential on site.

**Host answers (reachability)**

1. From ws01, `Test-NetConnection` to sql01 over TCP 1433 succeeds (corporate_lan→server_vlan rule allows
   1433) — OBSERVED command `up-net-04-resilient-network-architecture.yaml:56-57`, ports
   `template.yaml:65-68`.
2. From web01, the database port toward sql01 is live (dmz→server_vlan allows 1433) — OBSERVED step
   `up-ot-03-…yaml:516-518`, ports `template.yaml:60-64`.
3. From jump01, benign connectivity toward dc03 succeeds and toward historian01 fails
   (management→ad_forest_b carries; the OT cell is unreachable) — OBSERVED steps
   `rmc-c206-capstone-planning.yaml:66-68` and `rmc-c212-capstone-execution-assessment.yaml:65-67`;
   isolation `template.yaml:38, 74-77`.
4. From ws01, a connection toward plc01 is decided by a firewall (zone pair crosses corporate_lan→ot_scada,
   denied) — OBSERVED step `iot-security-foundations.yaml:334-336`, deny rule `template.yaml:74-77`.
5. edr01's response console cannot reach hmi01 (isolated cell) while the OT cell still runs — OBSERVED
   `up-ot-05-…yaml:334-336`; this failure is itself the expected result.
6. fw01 has **no** interface or rule facing ot_scada, and ot-fw01 has no transit leg — OBSERVED
   `template.yaml:557-571, 471-479`; taught as fact in `up-ot-01-…yaml:325-327` and
   `up-ot-05-…yaml:148-150`.
7. ids01's monitor NIC sees mirrored copies of every zone including OT — OBSERVED step
   `up-ot-02-…yaml:55-57`, mirror rule `template.yaml:87-91`.

**Service listens**

8. TLS service on web01 and web02 answers and reports a negotiated protocol version and cipher suite
   (numbers not named in source — the negotiated values are the lab evidence itself) — OBSERVED
   `c202-…:59-67`. The **listen port** of that TLS service is UNKNOWN; the only TLS port number in source
   is the firewall rule's 443 (`template.yaml:63`) and the scenario's dst_port 443
   (`scenario.yaml:251`).
9. HTTPS request to the home page of web01 and web02 returns a status code and a server header naming each
   host's stack — OBSERVED `up-net-04-…:233-241`.
10. vpn01 offers VPN services and a session can be brought up that assigns addresses and installs routes —
    OBSERVED `c202-…:234-239`. Protocol choice (openvpn vs ipsec, `template.yaml:165`) and ports: UNKNOWN
    from the courses.
11. ca01's CA view lists issued certificates with subject and expiry — OBSERVED `c202-…:147-149`.
12. vuln01 completes a server-VLAN scan whose results name host, finding id and severity — OBSERVED
    `up-ot-03-…:50-55`.
13. edr01 shows endpoints checked in, listed by zone; zones with no endpoint reporting are a lab output —
    OBSERVED `c212-…:519-520`.
14. siem01 serves its OpenSearch dashboards **on siem01 itself** and shows per-zone log sources — OBSERVED
    `up-net-06-…:57-61`. Listen port: UNKNOWN from source.
15. ntp01 serves a time source the OT devices' synchronisation can be checked against — OBSERVED
    `iot-security-foundations.yaml:520-522`. Protocol/port (chrony, `template.yaml:580`): port UNKNOWN.
16. historian01 collects named tags with intervals; plc01/plc02 answer holding-register polls whose
    function codes and register addresses are recordable (Modbus semantics OBSERVED
    `up-ot-01-…:228-232`; **TCP port number not named in any source** — UNKNOWN; template names the
    service `modbus_tcp` only, `template.yaml:451`).
17. ansible01 answers SSH from jump01 and holds a lab directory with two staged firmware images — OBSERVED
    `rmc-c204-…:54-58`. How the images are staged (provisioning artefact) is UNKNOWN — stop condition if
    absent.
18. aadconnect01 exposes synchronisation records readable from jump01 — OBSERVED
    `rmc-c208-…:160-162`.

**Account logs in**

19. Logon to jump01 works for the assigned account — the first lab step of 12 of the 13 courses
    (up-ot-01's M1 instead starts on the eng-ws01 console, item 22) — OBSERVED `c202-…:57-58`. The
    transport is named only in some courses: SSH in c202, rmc-c212, up-net-04 (`up-net-04-…:50-51`);
    SSH-or-RDP in iot's M2 (`iot-…:146`); RDP in up-net-06 — OBSERVED `up-net-06-…:49-50`. SSH and RDP
    are template services (`template.yaml:110`); the management allow rule names ports 22/3389/5985/5986
    (`template.yaml:85`).
20. Operator logon to hmi01 (and hmi02) with the instructor-assigned operator account — OBSERVED
    `up-ot-01-…:138-145`. The account's name/password: UNKNOWN (§3).
21. Standard-user sign-in on ws01 and ws02, then a sign-in attempt against dc01 — OBSERVED
    `rmc-c212-…:147-152`. Account names: UNKNOWN (§3).
22. Console logon to eng-ws01, which no management host can route to — OBSERVED
    `up-ot-01-…:500-502`.
23. Administrative access from jump01 to dc01's domain role and trust views, and to fw01's rule set —
    OBSERVED `rmc-c206-…:57-59` and the fw01 rows in §4. Account class: UNKNOWN (§3).

## 6. Courses using this template

13 courses, 78 lab modules, all `template: large-enterprise` (OBSERVED: 6 `lab_range` blocks per file, all
with that template, verified by grep count). 5 lab modules run the `apt-nation-state` scenario
(`scenario.yaml:3` declares `range_template: large-enterprise`).
"First lab step that would fail if the template were wrong" = the earliest step whose action needs a
specific template element (host, service, rule, isolation).

| Course (file stem) | Modules | Scenario labs | First lab step that fails if the template is wrong |
|---|---|---|---|
| c202-secure-communications-tls-vpn | 6 | — | M1 step 2: from jump01, connect to the TLS service on web01 and record protocol version, cipher suite, certificate dates (`content/courses/c202-…yaml:59-61` → needs jump01 SSH + web01 TLS) |
| c212-advanced-incident-response | 6 | M2 (:149) | M1 step 2: siem01 dashboard for the incident window with open alerts per network zone (`c212-…yaml:60-62` → needs siem01 dashboards + the 8-zone layout) |
| iot-security-foundations | 6 | — | M1 step 2: ids01 protocol view of the *mirrored* device-segment traffic (`iot-…yaml:62-64` → needs VLAN 108 SPAN + ids01 monitor NIC) |
| rmc-c204-iot-firmware-reverse-engineering | 6 | — | M1 step 2: two staged firmware images in the lab directory on ansible01 (`rmc-c204-…yaml:56-58` → needs ansible01 SSH + staged artefacts) |
| rmc-c206-capstone-planning | 6 | — | M1 step 2: enumerate server-VLAN hosts through dc01 with roles (`rmc-c206-…yaml:57-59` → needs dc01 + the server_vlan population) |
| rmc-c208-incident-response-end-to-end | 6 | M2 (:146), M3 (:241) | M1 step 1: open the incident queue on soar01 (`rmc-c208-…yaml:57-58` → needs soar01's case/queue) |
| rmc-c212-capstone-execution-assessment | 6 | — | M1 step 2: query dc01 for the groups holding administrative rights (`rmc-c212-…yaml:59-61` → needs dc01 directory data) |
| up-net-04-resilient-network-architecture | 6 | — | M1 step 2: from jump01, read the fw01 rules permitting corporate-LAN→server-VLAN traffic and record each rule's ports (`up-net-04-…yaml:52-54` → needs jump01 SSH + fw01's corp-to-server rule; step 1, SSH logon, is at :50-51) |
| up-net-06-network-defense-capstone-defend-the-enterprise | 6 | M2 (:141), M3 (:235) | M1 step 1: RDP logon to jump01 and open the fw01 dashboard (`up-net-06-…yaml:49-50` → needs jump01 RDP + fw01 dashboard) |
| up-ot-01-ics-scada-fundamentals | 6 | — | M1 step 1: eng-ws01 opens the controller programming application (`up-ot-01-…yaml:48-50` → needs eng-ws01 + plc_programming_sim); step 2 goes online to plc01 (:51-53) |
| up-ot-02-ot-network-monitoring-passive-discovery | 6 | — | M1 step 2: ids01 monitor interface receiving mirrored copies of every zone (`up-ot-02-…yaml:55-57` → needs VLAN 108 mirror incl. OT) |
| up-ot-03-ot-risk-assessment-zone-conduit-design | 6 | — | M1 step 1: vuln01 console shows a recent server-VLAN scan (`up-ot-03-…yaml:50-52` → needs vuln01 + a scan result) |
| up-ot-05-ot-incident-response-failover-safety | 6 | — | M1 step 2: siem01 indexes searched for OT-segment log sources *and the reason the isolated cell gives none* (`up-ot-05-…yaml:53-55` → needs siem01 + OT isolation) |

Scenario-note for the 5 scenario labs: the scenario drives ws06 → fs01 → sql01 laterally
(`scenario.yaml:120-143`), which the template's corporate_lan/server_vlan layout supports, but it names
`corp.example` and user `j.harper` while the template forest is `corp.truenorth.local` and no j.harper
exists in the template (§3) — UNKNOWN whether the deployed range renders those variables into live state.

## 7. What source cannot answer (bring to site or leave UNKNOWN)

- Any username, password or credential (§3). Provisioning them is an on-site step.
- Listen ports for every template service except those named by a firewall rule or course step (1433, 443,
  8443, 22, 3389, 5985, 5986 — `template.yaml:63, 68, 85`).
- Whether `user_profile` values provision loggable accounts, and whether the two declared AD trust
  relationships are provisioned as live trust objects.
- Whether the `apt-nation-state` variables (`j.harper`, `corp.example`) map onto the deployed forest.
- How the ansible01 firmware lab directory is staged (a provisioning artefact, not a template node).
- Everything a derivation needs a live range for: image readiness, boot order, pfSense configuration,
  actual scan/detection content — stop condition per the ticket.
