# Lab checks — range template `red-vs-blue`

Acceptance checklist for `content/ranges/red-vs-blue/template.yaml` (version "2.0",
`vm_count: 40`), derived from source at base commit `d5851f6d`. It exists so the
Monday on-site team can confirm the build is right and know which courses it unlocks.

Courses that use this template (all lab steps run on it):

- `content/courses/c210-threat-modeling-att-ck.yaml` (C210, Threat Modeling & ATT&CK)
- `content/courses/rmc-c201-advanced-network-defense.yaml` (RMC C201, Advanced Network Defense)
- `content/courses/up-soc-06-purple-team-operations.yaml` (C524, Purple Team Operations)
- `content/courses/up-red-06-full-adversary-emulation-capstone.yaml` (C542, Full Adversary Emulation Capstone)

Labels: **OBSERVED** = read in a named file at a line; **INFERRED** = reasoned from
what was read; **UNKNOWN** = not derivable from source. Nothing here was run against a
live range; every "expected" statement is a derivation, not a test result.

---

## 1. Nodes

All 40 node ids, OS, role, zone and address are **OBSERVED** in
`content/ranges/red-vs-blue/template.yaml` (line ranges given per node). Proxmox
placement (`node: coyote` / `node: acme`) is **OBSERVED** per node and matches
`deployment.node_assignment` (19 coyote / 21 acme, template.yaml:689–728).

| id | OS | role | vlan (zone) | ip | node | template lines |
| --- | --- | --- | --- | --- | --- | --- |
| jump01 | ubuntu-2404 | jump_host | management | 10.0.0.10 | coyote | 160–169 |
| score01 | ubuntu-2404 | scoreboard | management | 10.0.0.20 | coyote | 171–180 |
| siem01 | ubuntu-2404 | siem | management | 10.0.0.30 | coyote | 182–191 |
| relay01 | ubuntu-2404 | log_relay | management | 10.0.0.40 | coyote | 193–202 |
| nids01 | ubuntu-2404 | network_ids | management (+monitor NIC) | 10.0.0.50 | coyote | 204–219 |
| ids01 | ubuntu-2404 | ids_sensor | management (+monitor NIC) | 10.0.0.60 | coyote | 670–683 |
| fw01 | pfsense-2.7 | firewall | management | 10.0.0.250 | coyote | 592–604 |
| rtr01 | vyos-1.4 | router (edge) | all six routed zones | .1 in each | acme | 618–645 |
| web01 | ubuntu-2404 | web_server | dmz | 10.20.0.10 | acme | 225–234 |
| web02 | ubuntu-2404 | web_server | dmz | 10.20.0.11 | acme | 237–247 |
| mx01 | ubuntu-2404 | mail_gateway | dmz | 10.20.0.30 | acme | 249–259 |
| rproxy01 | ubuntu-2404 | reverse_proxy | dmz | 10.20.0.40 | acme | 261–271 |
| vpn01 | pfsense-2.7 | vpn_gateway | dmz | 10.20.0.50 | acme | 273–283 |
| dc01 | windows-server-2022 | domain_controller | server_vlan | 10.30.0.10 | coyote | 289–301 |
| dc02 | windows-server-2022 | domain_controller | server_vlan | 10.30.0.11 | acme | 303–314 |
| db01 | ubuntu-2404 | database_server | server_vlan | 10.30.0.30 | acme | 316–326 |
| fs01 | windows-server-2022 | file_server | server_vlan | 10.30.0.40 | acme | 328–338 |
| exch01 | windows-server-2022 | mail_server | server_vlan | 10.30.0.50 | acme | 340–350 |
| bak01 | ubuntu-2404 | backup_server | server_vlan | 10.30.0.60 | acme | 352–362 |
| ca01 | windows-server-2022 | certificate_authority | server_vlan | 10.30.0.70 | coyote | 364–374 |
| webapp01 | ubuntu-2404 | web_application | server_vlan | 10.30.0.80 | acme | 376–386 |
| ws01 | windows-11 | workstation (hr_manager) | corporate_lan | 10.10.0.10 | acme | 392–403 |
| ws02 | windows-11 | workstation (finance_analyst) | corporate_lan | 10.10.0.11 | acme | 405–416 |
| ws03 | windows-11 | workstation (it_admin, rsat) | corporate_lan | 10.10.0.12 | acme | 418–429 |
| ws04 | windows-11 | workstation (executive) | corporate_lan | 10.10.0.13 | acme | 431–442 |
| lnx01 | ubuntu-2404 | workstation (developer) | corporate_lan | 10.10.0.50 | acme | 444–455 |
| lnx02 | ubuntu-2404 | workstation (developer) | corporate_lan | 10.10.0.51 | acme | 457–468 |
| tgen01 | ubuntu-2404 | traffic_generator | corporate_lan | 10.10.0.99 | acme | 647–656 |
| honey01 | ubuntu-2404 | honeypot (canary) | corporate_lan | 10.10.0.200 | acme | 658–668 |
| blue01–blue05 | ubuntu-2404 | blue_analyst | blue_team | 10.40.0.10–.14 | coyote | 474–527 |
| red-c2-01 | kali-2024 | red_c2 | red_team | 10.66.0.10 | coyote | 533–542 |
| red-op01–red-op03 | kali-2024 | red_operator | red_team | 10.66.0.20–.22 | coyote | 544–575 |
| red-phish01 | ubuntu-2404 | red_phishing | red_team | 10.66.0.50 | coyote | 577–586 |
| fw02 | pfsense-2.7 | firewall | management | 10.0.0.251 | acme | 606–616 |

OS aliases all resolve in the image catalogue: `ubuntu-2404` (`content/catalogue/vm_iso_catalogue.csv:10`),
`windows-11` (row `win11-24h2`, :5), `windows-server-2022` (row `srv2022`, :8),
`kali-2024` (row `kali`, :9), `pfsense-2.7` (:15), `vyos-1.4` (:25) — **OBSERVED**.
Build status is not uniform. `pfsense` is `exists(proxmox);todo(vsphere)` (:15) — packer
only for Proxmox. The `vyos` row (:25) is `todo(vsphere)` with no `exists(proxmox)`
marker; its note points at the keystroke build in `appliances.pkr.hcl`, and that file's
vyos source is a `vsphere-iso` build (`infra/vsphere/packer/appliances.pkr.hcl:170–171`)
with no vyos packer under `infra/proxmox/` (**OBSERVED**) — whether a Proxmox build of
vyos works is **UNKNOWN**. `range_network_spec.md:60` still lists `red-vs-blue`'s
`vyos-1.4` router as GAP-vyos. Whether rtr01's image exists for the target hypervisor
on the day is **UNKNOWN** (decide per `range_network_spec.md:60–62`).

Hosts a course step names: 22 distinct ids (`blue01, db01, dc01, exch01, fs01, honey01,
jump01, lnx01, mx01, nids01, red-c2-01, red-op01, relay01, rproxy01, rtr01, score01,
siem01, tgen01, vpn01, web01, web02, ws01`) — all exist in the template (**OBSERVED**,
cross-checked against `nodes:`). 18 nodes are never named by a course step: `bak01,
blue02–05, ca01, dc02, fw01, fw02, ids01, lnx02, red-op02, red-op03, red-phish01,
webapp01, ws02–04` (**OBSERVED**). They are still part of the build the courses assume
(see §3 and §4).

## 2. Networks and addressing

All **OBSERVED** in `template.yaml:35–67` unless noted.

| vlan id | name | cidr | purpose |
| --- | --- | --- | --- |
| 100 | management | 10.0.0.0/24 | scoring, jump boxes, SIEM, IDS |
| 200 | corporate_lan | 10.10.0.0/24 | corporate endpoints |
| 300 | dmz | 10.20.0.0/24 | internet-facing web, mail relay, VPN |
| 400 | server_vlan | 10.30.0.0/24 | AD, DB, shares, Exchange, PKI |
| 500 | blue_team | 10.40.0.0/24 | Blue analyst workstations |
| 666 | red_team | 10.66.0.0/24 | isolated Red enclave |
| 700 | network_monitoring | 10.70.0.0/24 | SPAN only, `isolated: true`, no addresses, no gateway (:61–67) |

- Every routed zone's gateway is rtr01 at `.1` (rtr01 interfaces, template.yaml:633–645);
  the template says so in prose too (:628–629). **OBSERVED**.
- rtr01 enforces `network.firewall_rules` "in order, first match wins, default drop"
  (:69, :629–631); the renderer confirms `default-action 'drop'`
  (`control-plane/worker/worker/vyos_config.py:163`) and renders `action: mirror` as a
  SPAN session, not a filter rule (`vyos_config.py:199–200`). **OBSERVED**.
- Rules that matter to the courses (**OBSERVED**, template.yaml:71–151): red→dmz allow
  (:73–77); red→corp, red→server, red→mgmt deny (:78–92); logs to management on
  TCP+UDP 514/5044/9200 (:94–99); corp→server AD port set incl. 53/88/389/445/636
  (:105–110); corp→server only 1433/3306 for DB (:111–116); corp→dmz 80/443/25/587
  (:117–122); dmz→server only 1433/3306/25/587 (:123–128); blue→mgmt only
  443/514/5601/9200 (:130–134); blue→server allow all (:136–139); mgmt→all allow
  (:141–145); all→network_monitoring mirror (:147–151). Port rules are TCP and UDP
  (`vyos_config.py:224`). **OBSERVED**.
- **INFERRED access-path note for the on-site team:** there is no blue_team→corporate_lan
  allow rule, so blue01 cannot reach ws01/lnx01/honey01 directly; steps that pair them
  (e.g. `c210:319`, `c210:406`, `up-red-06:432`) must run by way of jump01, which
  mgmt→all (:141–145) and its RDP-gateway role (:169) permit. Same reasoning for
  blue01→dmz (no blue→dmz rule): reach dmz hosts via jump01 or rproxy paths.
- fw01/fw02 are **not in the forwarding path** — one management NIC each, "forwarding
  nothing", pending issue #235 (:602–604, :616). Do not expect them to filter anything.

## 3. Accounts the courses assume

- The template defines **no accounts and no credentials** — there is no accounts section
  in `template.yaml` (**OBSERVED**, whole file read). Every logon the courses assume is
  therefore provisioned outside source: usernames/passwords for SSH on jump01, for the
  domain, for dashboards are **UNKNOWN**.
- AD forest `corp.range.local` on dc01 (all five FSMO roles, template.yaml:299–300) and
  dc02 (:313); workstations ws01–04 are described as "Domain-joined" (:403, :416, :429,
  :442). **OBSERVED.** RMC C201 M1 step 3 assumes the workstation zone authenticates
  against this directory (`rmc-c201:59–61`). **OBSERVED** (step text).
- Per-workstation user profiles `hr_manager, finance_analyst, it_admin, executive,
  developer` (template.yaml:402, :415, :428, :441, :454, :467). **OBSERVED** as profile
  keys; what local accounts they map to is **UNKNOWN**.
- RMC C201 M5 step 2 assumes a signed-in interactive account on ws01 whose access is
  lost on containment (`rmc-c201:427–429`). **OBSERVED** (step text); the account is
  **UNKNOWN**.
- C542 assumes a failed sign-in attempt at vpn01 produces failed sign-in events
  (`up-red-06:152–154`, quiz `up-red-06:108–115`); whether vpn01 has an account store
  that logs failures is **UNKNOWN**.
- All four courses open with "Log on to jump01 over SSH" (`c210:58–59`, `rmc-c201:53–55`,
  `up-soc-06:47–48`, `up-red-06:51–53`) and C542 continues "take control of red-op01
  through the RDP gateway" (`up-red-06:52–53`). jump01's `services: [ssh, rdp_gateway]`
  (:168) matches. **OBSERVED.** Credentials: **UNKNOWN**. red-op01 is Kali; whether it
  accepts RDP sessions is **UNKNOWN** (not stated in source).

## 4. Services the course steps name

Derived by reading every `lab_range` step of the four courses (24 modules, 6 each).
Service names are the template's `services:` keys. "Installable" = present in the
deploy-time software catalogue `content/catalogue/software_catalogue.yaml` (:59–151);
names not in the catalogue and not in its `roles:` list are skipped with a warning at
deploy (`control-plane/worker/worker/software_catalogue.py:135,155,157`) — **OBSERVED**.

| course step names | hosts | template services | installable today? | citation |
| --- | --- | --- | --- | --- |
| SSH logon | jump01 | ssh, rdp_gateway (:168) | ssh is a catalogue role (skipped silently); rdp_gateway not in catalogue | `c210:58`, `rmc-c201:53`, `up-soc-06:47`, `up-red-06:51` |
| RDP gateway to red-op01 / blue01 | jump01 | rdp_gateway | not in catalogue → skipped with warning | `up-red-06:52–53`, `up-red-06:144–145`, `up-red-06:234–236`, `up-red-06:328–330`, `up-red-06:423–425` |
| enumerate DMZ hosts/services | web01, web02, mx01, rproxy01, vpn01 | nginx, php, https (:233); apache, tomcat (:245); postfix, dovecot, spamassassin (:257); haproxy, modsecurity (:269); openvpn, ipsec (:281) | nginx, apache2, postfix, haproxy installable; php, dovecot, spamassassin, tomcat, modsecurity, openvpn, ipsec not in catalogue | `c210:60–61`, `c210:144–145` |
| HTTP response header / service version from web01 | web01 | nginx | nginx installable | `up-red-06:54–56`, `up-red-06:146–148` |
| mail banner from mx01 | mx01 | postfix | postfix installable | `up-red-06:57–59`, `up-red-06:149–151` |
| VPN portal / version / sign-in attempt | vpn01 | openvpn, ipsec | not in catalogue | `up-red-06:60–62`, `up-red-06:152–154` |
| router ruleset read on rtr01 | rtr01 | ospf, bgp, inter_vlan_routing (:626) | n/a (VyOS config, not catalogue software) | `rmc-c201:56–58`, `rmc-c201:153–154`, `up-soc-06:49–50`, `c210:63–64` (instructor-placed copy on blue01) |
| directory/auth on dc01 | dc01 | active_directory, dns, gpo, dhcp (:297) | Windows roles, not catalogue | `rmc-c201:59–61` |
| log sources on siem01 / dashboards | siem01 | opensearch, opensearch_dashboards, syslog (:190) | none in catalogue → skipped with warning | `up-soc-06:51–52`, `c210:149–151`, `c210:316–318`, `rmc-c201:62–64`, `up-red-06:63–65` |
| log path web01→relay01→siem01 | relay01 | rsyslog, filebeat (:201) | not in catalogue | `c210:149–151`, `c210:235–237` |
| signatures on nids01 / mirror events | nids01 | suricata, zeek, evebox (:218) | none in catalogue | `up-soc-06:53–54`, `rmc-c201:249–251`, `rmc-c201:333–335`, `up-red-06:240–242` |
| scoring on score01 | score01 | http, https, scoring_engine (:179) | not in catalogue | `up-soc-06:219–220`, `up-soc-06:378–379` |
| investigation tool on blue01 | blue01 | ssh, rdp, wireshark, zeek, velociraptor_client (:482) | wireshark installable; zeek, velociraptor_client not | `up-soc-06:217–218`, `up-soc-06:305–306` |
| Sigma rule writing in siem01 | siem01 | (as siem01 above) | — | `up-soc-06:299–300`, `up-soc-06:463–464` |
| benign replay from tgen01 | tgen01 | tcpreplay, scapy, cron (:655) | not in catalogue | `up-soc-06:301–302`, `up-soc-06:303–304` |
| C2 listener + implant callback | red-c2-01, red-op01 | cobalt_strike, metasploit (:541); burpsuite, bloodhound, impacket (:552) | none in catalogue | `up-soc-06:131–134` |
| honeypot contact → siem01 alert | honey01 | cowrie, dionaea, elasticpot (:666) | none in catalogue | `rmc-c201:241–248` |
| PostgreSQL attempt ws01→db01 TCP 5432 | ws01, db01 | postgresql, mysql (:324) | postgresql installable; mysql not (mariadb-server is a separate name) | `rmc-c201:155–157` |
| endpoint telemetry on ws01 / lnx01 | ws01, lnx01 | rdp, sysmon, defender (:400); ssh, auditd, docker (:452) | not in catalogue (sysmon/defender are Windows services, not catalogue software) | `c210:319–321`, `c210:406–407`, `up-red-06:432–433` |
| whether exch01 and db01 ship events to siem01 | exch01, db01 | exchange, owa, smtp (:348); postgresql, mysql (:324) | exchange resolves to role image `srv2022-exchange2019` (`windows_roles.py:114–116`), image build `todo(vsphere)` per `vm_iso_catalogue.csv:29` | `up-red-06:337–339` |
| TLS session to web01 seen by nids01 | nids01, web01 | suricata, zeek (:218, :233) | not in catalogue | `rmc-c201:333–335` |
| deny-rule test red-op01→ws01 | red-op01, ws01 | (path test, no new service) | — | `rmc-c201:518–520` |

**INFERRED** summary for the on-site team: many course-critical capabilities (SIEM,
network sensors, honeypot, C2 framework, traffic replay, scoring engine, RDP gateway)
are *named* by the template but are **not** provisioned by today's software catalogue,
so a bare build of this template will not satisfy those steps until the catalogue or
golden images grow. Which deployment path (Proxmox vs vSphere) is used changes what is
buildable — see §1 image-status note.

## 5. Post-build checks (expected after the build)

Only checks derivable from source. Exact probe commands for this template are not
written down anywhere in the repo (**OBSERVED**: no lab-check runbook in `content/`
or `docs/`; the only literal probe command in `content/` is an inject in the
unrelated `ics-attack` scenario, `content/scenarios/ics-attack/scenario.yaml:64`),
so concrete commands are **UNKNOWN**; phrase each as a behavior to confirm.

Reachability (**OBSERVED** addresses; commands UNKNOWN):

1. jump01 answers at 10.0.0.10 and accepts SSH logon (courses' first step everywhere).
2. From jump01 (mgmt→all, template.yaml:141–145) every zone's hosts answer — the DMZ five
   (web01, web02, mx01, rproxy01, vpn01; "DMZ — 5 VMs", template.yaml:222) are exercised
   by `c210:60–61`.
3. blue01 (10.40.0.10) reaches siem01 only on 443/514/5601/9200 (blue-to-mgmt,
   template.yaml:130–134) and reaches dc01/db01/fs01/exch01 freely (blue-to-server,
   :136–139).
4. red-op01 (10.66.0.20) reaches the DMZ (red-to-dmz, :73–77) and must **fail** toward
   ws01 (red-to-corp-deny, :78–82) — the negative check RMC C201 M6 step 2 performs
   (`rmc-c201:518–520`).
5. ws01→db01 on TCP 5432 must **fail**: default drop (`vyos_config.py:163`) plus
   corp→server DB ports limited to 1433/3306 (template.yaml:111–116). **OBSERVED**
   derivation; the course asserts the same expectation (`rmc-c201:138–143,155–157`).

Service listens (port citable from source vs UNKNOWN):

- Log shipping to management on 514/5044/9200 TCP+UDP is permitted for every zone
  (logs-to-siem, template.yaml:94–99; TCP+UDP per `vyos_config.py:224`). That something
  must *listen* there is **INFERRED**; that opensearch/filebeat are actually installed is
  **UNKNOWN** (not in the software catalogue).
- siem01 dashboards must be reachable from blue01 within blue-to-mgmt's 443/5601/9200
  (template.yaml:130–134); which port the dashboards bind is **UNKNOWN** (5601 is the
  conventional OpenSearch Dashboards port and appears in the rule — **INFERRED**, not
  bound to a service name by source).
- web01 HTTP response header exists (`up-red-06:54–56`) with nginx installable; the port
  is not stated in source — **UNKNOWN**.
- mx01 shows a mail banner (`up-red-06:57–59`) with postfix installable; ports 25/587
  appear in the ruleset (:117–128) — **INFERRED** those are the SMTP ports.
- vpn01 discloses a version string before sign-in (`up-red-06:60–62`); port **UNKNOWN**.
- honey01 presents services/banners to the network (`rmc-c201:241–243`) and raises an
  alert in siem01 when touched (`rmc-c201:247–248`); which ports and whether the
  honeypot software is installed — **UNKNOWN** (not in catalogue).
- nids01 and ids01 monitor NICs hold no address and receive mirror copies of every zone
  (template.yaml:211–216, :677–680; mirror = SPAN, `vyos_config.py:199–200`). Check:
  both sensors see cross-zone traffic. **OBSERVED** design; sensor install **UNKNOWN**.

Account logon:

- SSH logon to jump01 works (all four courses, §3) — account **UNKNOWN**.
- Domain logon on ws01–04 against corp.range.local (dc01/dc02) — credentials
  **UNKNOWN**; check is "a domain user can sign in on a workstation", not a named user.
- Failed sign-in events from vpn01 reach the central log platform (C542's expected
  signal, `up-red-06:108–115,243–245`) — depends on vpn01 telemetry existing; today
  **UNKNOWN**.

## 6. Courses using this template

All figures **OBSERVED** by reading the course files (6 modules each, every module's
`lab_range.template` is `red-vs-blue` — 24 lab_range blocks total, verified by grep
`template: red-vs-blue`).

| course | file | modules on template | first lab step that would fail if the template were wrong |
| --- | --- | --- | --- |
| C210 Threat Modeling & ATT&CK | `content/courses/c210-threat-modeling-att-ck.yaml` | 6 / 6 | M1 S1, log on to jump01 over SSH — `c210:58–59`; the first template-specific one is M1 S2, enumerate the five DMZ hosts web01/web02/mx01/rproxy01/vpn01 — `c210:60–61` |
| RMC C201 Advanced Network Defense | `content/courses/rmc-c201-advanced-network-defense.yaml` | 6 / 6 | M1 S1, log on to jump01 and confirm reachable zones — `rmc-c201:53–55`; M1 S2 needs rtr01 and its ruleset — `rmc-c201:56–58` |
| C524 Purple Team Operations | `content/courses/up-soc-06-purple-team-operations.yaml` | 6 / 6 | M1 S1, log on to jump01 and open the worksheet — `up-soc-06:47–48`; M1 S2 needs rtr01's red-enclave rules — `up-soc-06:49–50` |
| C542 Full Adversary Emulation Capstone | `content/courses/up-red-06-full-adversary-emulation-capstone.yaml` | 6 / 6 | M1 S1, log on to jump01 *and* take control of red-op01 through the RDP gateway — `up-red-06:51–53`; M1 S2 needs web01 reachable from red-op01 — `up-red-06:54–56` |

Per-course first template-specific failure, in one line each:

- **C210**: without the five DMZ hosts, M1 S2 (`c210:60–61`) cannot record "each DMZ
  host with the service it exposes". Without the logs-to-siem rule and relay01, M2 S4
  (`c210:149–151`) cannot trace web01→relay01→siem01.
- **RMC C201**: without rtr01's default-drop ruleset, M1 S2 (`rmc-c201:56–58`) and M2 S3
  (ws01→db01 TCP 5432, `rmc-c201:155–157`) both lose their subject; without honey01,
  M3 (`rmc-c201:241–248`) has no deception asset to alert.
- **C524**: without rtr01's red-to-dmz allow and red-to-corp/server denies
  (`up-soc-06:49–50`), M1 S2 cannot scope the engagement; without red-c2-01 and red-op01,
  M2 (`up-soc-06:131–134`) has no listener or operator host.
- **C542**: without jump01's RDP gateway and red-op01, M1 S1 (`up-red-06:51–53`) fails
  at "take control of red-op01"; without web01/mx01/vpn01 in the DMZ, the plan's first
  three baseline steps (`up-red-06:54–62`) cannot record headers, banner or version.

## 7. Unknowns the on-site team must resolve before the build

1. Whether the `vyos-1.4` router image exists for the chosen hypervisor
   (`range_network_spec.md:60–62`, `vm_iso_catalogue.csv:25`) — rtr01 is the sole
   enforcement point; no rtr01, no range.
2. Whether the Proxmox deployment path described in the template header
   (`template.yaml:4`) can build the Exchange role image `srv2022-exchange2019`
   (status `todo(vsphere)`, `vm_iso_catalogue.csv:29`) — C542 M4 S4
   (`up-red-06:337–339`) expects exch01 to be present and either shipping or silent.
3. Every course-critical capability named in §4 but absent from
   `content/catalogue/software_catalogue.yaml` (SIEM, sensors, honeypot, C2, replay,
   scoring engine, RDP gateway) — **UNKNOWN** how they get installed today.
4. All credentials (jump01 SSH, domain users, dashboards, vpn01 portal) — **UNKNOWN**,
   provisioned outside source.
5. Which port siem01's dashboards actually bind (blue01 can only use 443/5601/9200,
   template.yaml:130–134) — **UNKNOWN**.
