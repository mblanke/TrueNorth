# Lab checks — `red-team` range template

Acceptance checklist for `content/ranges/red-team/template.yaml`, derived from source only.
Base commit: `d5851f6d` (2026-11-07). Every claim is labelled:

- **OBSERVED** — read directly at the cited `file:line` (template or course YAML).
- **INFERRED** — reasoned from what was read; the reasoning is stated.
- **UNKNOWN** — not derivable from source; must be checked on a live build, never filled in.

Courses read in full for this checklist (every `lab_range` step of every module):

- `content/courses/up-idm-02-active-directory-attack-defense.yaml` (C508, 6 modules)
- `content/courses/up-red-01-web-exploitation-fundamentals.yaml` (C537, 6 modules)
- `content/courses/up-red-03-c2-infrastructure-operational-security.yaml` (C539, 6 modules)
- `content/courses/up-red-04-adversary-emulation-planning-att-ck-based.yaml` (C540, 6 modules)
- `content/courses/up-red-05-post-exploitation-lateral-movement-lab.yaml` (C541, 6 modules)

All 30 modules of these five courses declare `template: red-team` (OBSERVED: one
`lab_range: template: red-team` per module in each file; counts verified with
`grep -c "template: red-team"` = 6 per file). No other course in `content/courses/`
uses this template (grep over `content/courses/*.yaml`).

## 1. Nodes

OBSERVED: all rows from `content/ranges/red-team/template.yaml` (line numbers in the
Node column). 28 nodes are declared; `vm_count: 28` (template.yaml:12) matches the node
count. **Discrepancy:** `resource_totals.total_vms: 30` (template.yaml:395) and the
description's "30 VMs" (template.yaml:5) do not match the 28 nodes. ADR 0004 records
the former `vm_count` 30-vs-28 mismatch as **corrected 2026-10-09**
(docs/adr/0004-scheduler-module.md:354-357) — `vm_count: 28` is now what the template
declares and what tests/contracts/test_template_vm_count.py enforces; the remaining
stale fields `resource_totals.total_vms: 30` and the description's "30 VMs" are
covered by no ADR or test (no Python file references `resource_totals`).

| Node | template.yaml | OS | Role | VLAN | IP | Declared services | Notes for courses |
|---|---|---|---|---|---|---|---|
| t-web01 | :65-78 | ubuntu-2404 | web_server | target_dmz | 10.70.1.10 | nginx, wordpress | Product search + login form drills (C537 m2); web access log review (C539 m5); foothold host (C541 m1, m3, m6) |
| t-web02 | :80-93 | ubuntu-2404 | web_server | target_dmz | 10.70.1.11 | tomcat, java_app | Session-token and access-control drills (C537 m3); SSRF source toward t-dc01 (C537 m5) |
| t-mail01 | :94-104 | ubuntu-2404 | mail_server | target_dmz | 10.70.1.20 | postfix, dovecot, roundcube | Reachability probe target (C540 m1 step 3, m2 step 3) |
| t-vpn01 | :106-117 | pfsense | vpn_gateway | target_dmz | 10.70.1.30 | openvpn | Reachability probe target (C540 m2 step 3); whether it answers on any port permitted from attacker_infra is UNKNOWN |
| t-dns01 | :118-128 | ubuntu-2404 | dns_server | target_dmz | 10.70.1.40 | bind9 | Name resolution for the web hosts (C537 m1); zone-transfer target (C537 m4; C539 m3); resolver query log review (C539 m5) |
| t-dc01 | :131-145 | windows-server-2022 | domain_controller | target_corp | 10.70.0.10 | active_directory, dns, dhcp, gpo | `ad_domain: targetcorp.local` (:139). Kerberoastable SPN + AS-REP-roastable vulnerabilities declared (:141-144). Serves targetcorp.local DNS (C539 m3); event-log, lockout-policy and sign-in-baseline reviews (C508 m5; C540 m5, m6; C541 m3-m5); SSRF destination (C537 m5) |
| t-dc02 | :146-154 | windows-server-2019 | domain_controller | target_corp | 10.70.0.11 | active_directory, dns | Not named by any course lab step |
| t-fs01 | :156-168 | windows-server-2022 | file_server | target_corp | 10.70.0.20 | smb, dfs | `pre_staged_data` includes `\shares\IT\passwords.xlsx` (:165) — the "documented IT share" of C540 m4 and the stored-credential share of C541 m3; share-audit probe target (C540 m4); file-share-port probe (C541 m1) |
| t-ca01 | :169-180 | windows-server-2022 | pki | target_corp | 10.70.0.30 | adcs | ESC1 template misconfiguration declared (:178-179); not named by any current course lab step |
| t-ws01 | :181-191 | windows-11 | workstation | target_corp | 10.70.0.100 | — | `user_profile: domain_admin` (:188). Local-administrators audit via graph and direct logon (C508 m1, m4); IT-share benign probe source (C540 m4); ticket-volume baseline comparison (C508 m5) |
| t-ws02 | :193-204 | windows-11 | workstation | target_corp | 10.70.0.101 | — | `stored_ssh_keys` vulnerability (:202-203). Pre-captured sign-in log review (C541 m3) |
| t-ws03 | :205-212 | windows-11 | workstation | target_corp | 10.70.0.102 | — | Pre-captured persistence schedule excerpt (C541 m4) |
| t-ws04 | :214-221 | windows-11 | workstation | target_corp | 10.70.0.103 | — | Not named by any course lab step |
| t-ws05 | :223-230 | windows-11 | workstation | target_corp | 10.70.0.104 | — | `user_profile: cfo`; not named by any course lab step |
| t-linux01 | :232-245 | ubuntu-2404 | application | target_corp | 10.70.0.50 | gitlab_ce, docker | Pre-captured command log review (C541 m5) |
| t-sql01 | :247-260 | windows-server-2022 | database | target_db | 10.70.2.10 | mssql_2022 | Database-port probe on 1433 (C541 m1 step 4) |
| t-mysql01 | :261-272 | ubuntu-2404 | database | target_db | 10.70.2.20 | mysql_8 | Not named by any course lab step |
| t-redis01 | :273-286 | ubuntu-2404 | cache | target_db | 10.70.2.30 | redis | Not named by any course lab step |
| a-kali01 | :288-296 | kali-2024 | attack_platform | attacker_infra | 10.70.3.10 | metasploit, burpsuite, bloodhound, impacket, crackmapexec | The Student's home host in every course; SSH logon assumed (C508:48, C539:49); "graph tool already installed there" (C508:48) |
| a-kali02 | :297-305 | kali-2024 | attack_platform | attacker_infra | 10.70.3.11 | metasploit, burpsuite, bloodhound, impacket, crackmapexec | Not named by any course lab step |
| a-teamserver | :306-314 | ubuntu-2404 | c2_server | attacker_infra | 10.70.3.20 | cobalt_strike_sim, sliver | Listener/profile/port inspection (C539 m1, m2, m4, m6) |
| a-redir01 | :315-323 | ubuntu-2404 | redirector | attacker_infra | 10.70.3.30 | nginx_redirect, socat | Web-config backend inspection + HTTPS listener certificate (C539 m2); header comparison (C539 m4) |
| a-redir02 | :324-332 | ubuntu-2404 | redirector | attacker_infra | 10.70.3.31 | dns_redirect, socat | DNS-forwarding inspection (C539 m2); seizure inventory (C539 m6) |
| a-payload | :333-341 | ubuntu-2404 | staging | attacker_infra | 10.70.3.40 | nginx, python_http_server | Staged-file/directory inspection (C539 m1, m3, m6) |
| p-vpnconc01 | :343-352 | pfsense | vpn_gateway | pivot_network | 10.70.4.10 | openvpn, wireguard | Not named by any course lab step (C541 m2 uses p-jump01) |
| p-jump01 | :353-361 | ubuntu-2404 | jump_host | pivot_network | 10.70.4.20 | ssh, proxychains, chisel | Pivot logon and relayed-connection trace (C541 m2) |
| p-jump02 | :362-370 | windows-server-2022 | jump_host | pivot_network | 10.70.4.21 | rdp, ssh | Not named by any course lab step |
| fw01 | :372-392 | pfsense | firewall | pivot_network | 10.70.4.1 | — | `edge: true` (:381); five interfaces, one per VLAN (:383-392). Rule-set reviews in C537 m5, C539 m1-m3/m5/m6, C540 m1/m2/m5/m6, C541 m1/m2 |

## 2. Networks and addressing

OBSERVED (template.yaml:14-61): five VLANs, all 10.70.x.0/24:

| VLAN id | Name | CIDR | Purpose (:16-35) |
|---|---|---|---|
| 300 | target_corp | 10.70.0.0/24 | Target corporate network |
| 301 | target_dmz | 10.70.1.0/24 | Target DMZ, internet-facing services |
| 302 | target_db | 10.70.2.0/24 | Target database tier, restricted |
| 303 | attacker_infra | 10.70.3.0/24 | Attacker team infrastructure |
| 304 | pivot_network | 10.70.4.0/24 | Pivot / lateral-movement network |

OBSERVED (template.yaml:37-61) — the full firewall rule list on fw01:

| Rule | Src → Dst | Ports | Action |
|---|---|---|---|
| internet-to-dmz (:38-42) | attacker_infra → target_dmz | 80, 443, 25, 53 | allow |
| dmz-to-corp (:43-47) | target_dmz → target_corp | 8080, 1433, 389 | allow |
| corp-to-db (:48-52) | target_corp → target_db | 1433, 3306, 5432 | allow |
| db-isolation (:53-56) | target_db → attacker_infra | all | deny |
| pivot-all (:57-61) | pivot_network → * | all | allow |

INFERRED consequences the courses rely on (from the rule list alone; runtime result must
still be observed on the build):

- attacker_infra has **no** rule to target_corp, so a-kali01 cannot reach t-dc01
  directly — exactly what C537 m5 step 2 expects the Student to demonstrate
  (up-red-01:415-416).
- attacker_infra → target_dmz is limited to 80/443/25/53 — the "permitted ports" C540 m1
  step 2 and C541 m1 step 2 ask the Student to read off fw01.
- target_dmz → target_db has no rule (corp-to-db applies to target_corp only), so the
  C541 m1 step 4 probe from the t-web01 foothold to t-sql01:1433 is expected to be
  denied; the course asks the Student to record the result either way (up-red-05:65-67).
- pivot_network → * is the rule C541 m2 depends on for the p-jump01 relay
  (up-red-05:153-155).
- db-isolation blocks target_db → attacker_infra; C540 m2 step 2 quotes it
  (up-red-04:141-143).
- **Gap (INFERRED):** C508's graph collection runs on a-kali01 against the target domain
  in target_corp (up-idm-02:48-49, :141-142), but no firewall rule permits
  attacker_infra → target_corp on any port. How the collection reaches the directory
  (pivot host, pre-arranged rule, or staged graph data) is UNKNOWN — a build must make
  this step work without inventing a rule change.

fw01's per-VLAN interface addresses (template.yaml:383-392): target_corp 10.70.0.1,
target_dmz 10.70.1.1, target_db 10.70.2.1, attacker_infra 10.70.3.1, pivot_network
10.70.4.1. OBSERVED. C540 m2 step 1 asks the Student to read exactly this interface list
(up-red-04:139-140).

## 3. Accounts the courses assume

| Account / logon | Where the course assumes it | Source | Derivable from template? |
|---|---|---|---|
| "Assigned student account" in the t-web01 application | Sign-in for the injection/auth-bypass drills | up-red-01:142-143 (m2 s1) | **UNKNOWN** — the template declares no web-application users and no per-student account provisioning |
| "Assigned student account" in the t-web02 application | Two-login token comparison, access-control drill | up-red-01:236-237 (m3 s2) | **UNKNOWN** — same gap |
| SSH logon to a-kali01 | Every graph-tooling module (C508) and C2-infra module (C539) | up-idm-02:48, up-red-03:49 | **UNKNOWN** — credentials unspecified; `ssh` is not in a-kali01's declared `services:` list (template.yaml:295), though p-jump01 does declare ssh (:360) |
| Interactive logon to t-dc01 | Security-event-log review (C508 m5), lockout-policy reads (C540 m5, m6), account-expiry and domain-log reads (C541 m3, m4), sign-in-baseline reads (C541 m5) | up-idm-02:411, up-red-04:409-410, up-red-05:258-259, :351-352, :441-442 | **UNKNOWN** — account and credential unspecified |
| Interactive logon to t-ws01 | Direct local-Administrators read (C508 m4 s4) | up-idm-02:330-331 | **UNKNOWN** — account unspecified; `user_profile: domain_admin` (template.yaml:188) suggests an IT-admin profile exists but no credentials are declared |
| Logon to t-web01 and t-dns01 for log review | Web access log (C539 m5 s1), resolver query log (C539 m5 s2) | up-red-03:392, :394 | **UNKNOWN** — account unspecified |
| Logon to p-jump01 | Pivot module starting point | up-red-05:151 | **UNKNOWN** — account unspecified; p-jump01 declares ssh/proxychains/chisel (template.yaml:360) |
| Access to fw01's rule set | Rule reviews in C537 m5, C539 m1/m2/m3/m5/m6, C540 m1/m2/m5/m6, C541 m1/m2 | e.g. up-red-03:57, up-red-05:61-63 | **UNKNOWN** — access method (SSH console, web UI) and credentials unspecified |
| Directory read credentials for the graph collection | Read-only collection against targetcorp.local from a-kali01 | up-idm-02:48-49 | **UNKNOWN** — no directory user is declared anywhere in the template |
| t-ws01's domain account reading the IT share on t-fs01 | Benign share-access probe; the course says "record … the account used", implying it is pre-established | up-red-04:321-322 | **UNKNOWN** — share permissions and the account are not declared |

INFERRED: no course step names a password, hash or credential value, so no credential
material can be checked from source; all logon checks below stop at "an account exists
and logs in" and mark the credential UNKNOWN.

## 4. Services the course steps name

Only services named by at least one lab step are listed. Ports appear only where a
course step or a template firewall rule states them; otherwise UNKNOWN.

| Service (as the course uses it) | Named by | Host | Port | Source |
|---|---|---|---|---|
| DNS name resolution for the web hosts | C537 m1 s1 — "resolve the host names of t-web01 and t-web02 against t-dns01" | t-dns01 (bind9) | UNKNOWN (not stated) | up-red-01:50-51; template.yaml:125 |
| Unauthenticated zone transfer | C537 m4 s4, C539 m3 s2 | t-dns01 | UNKNOWN (not stated) | up-red-01:332-333; up-red-03:223-224; template vulnerability `zone_transfer` template.yaml:127 |
| HTTP web application with product search, login form, robots/sitemap | C537 m1-m2, m4, m6 | t-web01 (nginx, wordpress) | "the ports the internet rules allow" = 80/443/25/53 permitted from attacker_infra; which of these the host listens on is UNKNOWN | up-red-01:50-66, :142-155; up-red-05:58-59; template.yaml:41, :72 |
| Web application with session tokens and an administrative function | C537 m3, m5 (SSRF source) | t-web02 (tomcat, java_app) | UNKNOWN (not stated) | up-red-01:233-246, :418-423; template.yaml:87-88 |
| Mail service answering on a permitted port | C540 m1 s3, m2 s3 — "test each admitted service on … t-mail01" | t-mail01 (postfix, dovecot, roundcube) | 25 is the only mail port in the allow list; whether the host answers on it is UNKNOWN | up-red-04:55-57, :145-146; template.yaml:41 |
| AD directory queries: SPN accounts, password ages, delegation, privileged sessions, Domain Admins path | C508 m1-m4, m6 | t-dc01 (active_directory) | UNKNOWN (not stated) | up-idm-02:141-142, :233-234, :239-242, :509-510 |
| Kerberos service-ticket request leaving a directory event | C508 m3 s4 | t-dc01 | UNKNOWN (not stated) | up-idm-02:239-240 |
| DNS serving the targetcorp.local zone | C539 m3 s1 — "query t-dc01, the domain controller whose DNS serves the targetcorp.local zone" | t-dc01 (dns) | UNKNOWN (not stated) | up-red-03:222; template.yaml:138-139 |
| Windows security event log / directory-service log / sign-in baseline / lockout policy / account expiry record | C508 m5; C540 m4 s5, m5 s1, m6 s4; C541 m3 s5, m4 s4, m5 s3 | t-dc01 | n/a (host-local log) | up-idm-02:411-412; up-red-04:329-330, :409-410, :507-509; up-red-05:258-259, :351-352, :441-443 |
| SMB file share ("documented IT share") with pre-staged credential document | C540 m4 s1-s3; C541 m3 s1 | t-fs01 (smb, dfs) | 445 (named by the course step) | up-red-04:318-325; up-red-05:69-70; template.yaml:163-167 |
| Share access auditing + its event trail | C540 m4 s1, s3 | t-fs01 | n/a | up-red-04:318-319, :323-325 |
| Web access log | C539 m5 s1; C540 m4 s4 | t-web01 | n/a | up-red-03:392; up-red-04:327-328 |
| Resolver query log | C539 m5 s2 | t-dns01 | n/a | up-red-03:394 |
| Graph tooling ("the graph tool already installed there") | C508 m1 s1, all C508 modules | a-kali01 | n/a | up-idm-02:48-49. Which installed tool this is (bloodhound is declared on a-kali01, template.yaml:295) is INFERRED, not stated — treat the exact tool as UNKNOWN |
| SSH logon for the Student's home base | C508 m1 s1, C539 m1 s1 | a-kali01 | UNKNOWN (port not stated) | up-idm-02:48; up-red-03:49 |
| C2 listeners with configured ports/profiles | C539 m1 s2, m2 s3, m4 s1, m6 s1 | a-teamserver (cobalt_strike_sim, sliver) | UNKNOWN — the course records the ports as an outcome; the template declares service names only | up-red-03:51, :139, :308-310, :478-479 |
| HTTPS redirect listener presenting a certificate; forwards to a backend | C539 m2 s1, s5; m4 s2, s4 | a-redir01 (nginx_redirect, socat) | UNKNOWN (port not stated) | up-red-03:135, :143, :310-311, :314 |
| DNS redirector forwarding zones | C539 m2 s2; m6 s2 | a-redir02 (dns_redirect, socat) | UNKNOWN (port not stated) | up-red-03:137, :480 |
| Staging web service + staged file directories | C539 m1 s4, m3 s3, m6 s4 | a-payload (nginx, python_http_server) | UNKNOWN (port not stated) | up-red-03:55, :226, :484 |
| fw01 rule set (list, read, compare against scope) | C537 m5; C539 m1 s5, m2 s4, m3 s5, m5 s3, m6 s3; C540 m1 s2, m2 s1-s2, m5 s2, m6 s2; C541 m1 s3, m2 s2 | fw01 | n/a | up-red-01:412-413; up-red-03:57, :141, :230, :396, :482; up-red-04:53-54, :140, :143, :412-413, :502-503; up-red-05:61-63, :153-155 |
| LDAP probe | C541 m1 s4 — "probe t-dc01 on the LDAP port 389" | t-dc01 | 389 | up-red-05:65-66 |
| Database-port probe | C541 m1 s4 — "t-sql01 on its database port 1433" | t-sql01 (mssql_2022) | 1433 | up-red-05:65-67 |
| File-share-port probe | C541 m1 s5 — "toward t-fs01 on its file-sharing port 445" | t-fs01 | 445 | up-red-05:69-70 |
| Relayed connection p-jump01 → t-dc01 with per-hop port | C541 m2 s3 — "record each host the path crosses in order with the port that answered" | p-jump01 → t-dc01 | UNKNOWN (port recorded as an outcome) | up-red-05:157-158 |

## 4a. Pre-captured evidence the courses assume (staged artefacts)

OBSERVED: the courses repeatedly read "pre-captured" evidence staged on hosts. The
template declares staged data **only** on t-fs01 (`pre_staged_data`, template.yaml:164-167).

| Staged artefact | Read by | Source | Declared in template? |
|---|---|---|---|
| `\shares\IT\passwords.xlsx` (+ finance/executive files) on t-fs01 | C540 m4 (IT-share probe), C541 m3 (stored-credential chain) | template.yaml:164-167; up-red-04:321-322; up-red-05:247-248 | **Yes** |
| Claim sheet "listing three claimed findings" on a-kali01 | C537 m4 s1 | up-red-01:323-324 | **No — UNKNOWN** how it is staged |
| Observed copy of the fw01 rule set on a-kali01 | C537 m5 s1 | up-red-01:412-413 | **No — UNKNOWN** |
| Graph-tool collection data against the target domain | C508 m1 s1, m2 s1, m6 s1 | up-idm-02:48-49, :138-139, :504-505 | **No — UNKNOWN** (see the firewall gap in §2) |
| Pre-captured audit excerpt staged on the t-web01 foothold (names the t-fs01 share and the reused account) | C541 m3 s1-s2 | up-red-05:247-251 | **No — UNKNOWN** |
| Pre-captured sign-in log on t-ws02 | C541 m3 s3 | up-red-05:253-254 | **No — UNKNOWN** |
| Pre-captured schedule excerpt on t-ws03 (persistence window) | C541 m4 s1, s3 | up-red-05:342-343, :348-349 | **No — UNKNOWN** |
| Pre-captured domain log excerpt covering the persistence window on t-dc01 | C541 m4 s4 | up-red-05:351-352 | **No — UNKNOWN** |
| Pre-captured command log on t-linux01 | C541 m5 s1-s2 | up-red-05:436-440 | **No — UNKNOWN** |
| Sign-in baseline on t-dc01 | C541 m5 s3-s4 | up-red-05:441-446 | **No — UNKNOWN** |
| Pre-captured connection log on p-jump01 | C541 m2 s4 | up-red-05:159-161 | **No — UNKNOWN** |

A build that provisions only what the template declares will not satisfy these steps.
This is the single largest gap between the five courses and the template as written.

## 5. Post-build checks

Ports and commands are listed only where a course step or a template rule states them;
everything else is phrased as the observable outcome the courses themselves define. No
command syntax is invented: where no course names a command, the check names the
observation instead.

**Reachability (host answers):**

| # | Check | Expected | Source / label |
|---|---|---|---|
| R1 | From a-kali01, resolve the t-web01 and t-web02 host names against t-dns01 | Both names resolve to addresses the Student can record (10.70.1.10 / .11 per template, but the record is what the course grades) | up-red-01:50-51. OBSERVED requirement; resolved values INFERRED from template.yaml:70, :85 |
| R2 | From a-kali01, connect to each DMZ service the fw01 rule admits, on t-web01, t-mail01, t-dns01 | At least the services the courses test answer "within the permitted ports" (80/443/25/53); the courses grade the recorded answers, not a fixed port | up-red-04:55-57; up-red-05:58-59. OBSERVED requirement; which ports answer is UNKNOWN until the build is probed |
| R3 | Repeat R2 against t-vpn01 | Whether the VPN concentrator answers on any permitted port is UNKNOWN; C540 m2 s3 requires an answer to be recordable or the path scoped out | up-red-04:145-146. UNKNOWN |
| R4 | From a-kali01, attempt a connection to t-dc01 | Must fail (no attacker_infra→target_corp rule) — the step exists to demonstrate denial | up-red-01:415-416. OBSERVED requirement, INFERRED from template.yaml:37-61 |
| R5 | From the t-web01 foothold, probe t-dc01 on 389 and t-sql01 on 1433; probe t-fs01 on 445 | Each result recordable with the rule responsible; 389 expected permitted (in dmz-to-corp), 1433 expected denied (t-sql01 is in target_db and no target_dmz→target_db rule exists), 445 expected denied (t-fs01 is in target_corp but 445 is not in dmz-to-corp's port list) | up-red-05:64-70. OBSERVED requirement; per-port outcomes INFERRED from template.yaml:43-47 |
| R6 | From p-jump01, trace a connection toward t-dc01 | It arrives; every crossed host recordable with the port that answered (pivot-all permits pivot_network → *) | up-red-05:157-158; template.yaml:57-61. OBSERVED requirement |
| R7 | From a-kali01, reach the fw01 rule set and each attacker-side host (a-teamserver, a-redir01, a-redir02, a-payload) for inspection | Rule set readable; every attacker-side node's declared services inspectable (C539 requires service names, ports, configs and staged paths as outcomes) | up-red-03:51-57, :135-139, :226, :396. OBSERVED requirement |
| R8 | Reachability within attacker_infra: a-kali01 traffic capture toward the a-teamserver listener for two minutes | Callback traffic observable | up-red-03:312-313. OBSERVED requirement; same-VLAN reachability INFERRED |

**Service listens / evidence exists:**

| # | Check | Expected | Source / label |
|---|---|---|---|
| S1 | t-dns01 zone transfer (unauthenticated) | The query is answered one way or the other and the outcome is recordable; template declares `zone_transfer` as a vulnerability, so a permissive result is the templated state | up-red-01:332-333; up-red-03:223-224; template.yaml:127. OBSERVED requirement |
| S2 | t-dc01 serves the targetcorp.local zone | Records (type, name, value) returnable to a query from a-kali01 | up-red-03:222. OBSERVED requirement; zone content UNKNOWN beyond the domain name template.yaml:139 |
| S3 | t-fs01 exposes the IT share with `\shares\IT\passwords.xlsx` | Share listable; the staged files present | template.yaml:164-167. OBSERVED |
| S4 | t-fs01 share auditing is on and its events land somewhere findable | Auditing state and event location recordable; the benign probe from t-ws01 produces a findable event | up-red-04:318-325. OBSERVED requirement |
| S5 | t-web01 web access log records an external request | The C540 m4 probe and C539 m5 review both find line-level evidence | up-red-04:327-328; up-red-03:392. OBSERVED requirement |
| S6 | t-dns01 resolver query log records external-domain queries | Entries recordable | up-red-03:394. OBSERVED requirement |
| S7 | a-teamserver listeners, a-redir01 forwarding config + certificate, a-redir02 forwarding config, a-payload staged directories | All inspectable with the values the courses ask to be recorded (ports, backend address, zones, certificate subject/issuer, sleep/jitter/protocol) | up-red-03:51, :135-139, :143, :308-314, :478-484. OBSERVED requirement; the actual values UNKNOWN |
| S8 | All pre-captured artefacts in §4a are present on their hosts | Every listed excerpt/log is readable by the step that needs it | §4a sources. OBSERVED requirement; **UNKNOWN** until a build is checked |

**Account logs in:**

| # | Check | Expected | Source / label |
|---|---|---|---|
| A1 | SSH into a-kali01 works for the Student | Logon succeeds; the graph tool is present and starts a collection | up-idm-02:48-49; up-red-03:49. OBSERVED requirement; credentials and installed-tool inventory UNKNOWN |
| A2 | The assigned student account signs in to the t-web01 and t-web02 applications | Login forms accept it; C537 m2/m3 drills runnable | up-red-01:142-143, :236-237. OBSERVED requirement; account provisioning UNKNOWN |
| A3 | Interactive logon to t-dc01, t-ws01, p-jump01, t-web01, t-dns01 works for whichever account the instructor assigns | The log-review and policy-read steps complete | up-idm-02:330-331, :411-412; up-red-03:392, :394; up-red-05:151. OBSERVED requirement; accounts/credentials UNKNOWN |
| A4 | Domain authentication inside targetcorp.local works (directory queries return users, groups, computers) | Graph counts and SPN/delegation/session queries return data including the declared kerberoastable and AS-REP-roastable exposures | up-idm-02:51-55, :141-144; template.yaml:141-144. OBSERVED requirement; directory population UNKNOWN |

## 6. Courses using this template

OBSERVED: five courses, 30 modules total, every module `template: red-team`
(`grep -c "template: red-team"` = 6 per file). The per-template 97-course rollup lives
in `docs/lab-checks/README.md` (separate deliverable; not duplicated here).

| Course (file) | Course code | Modules | First lab step that fails if the template is wrong |
|---|---|---|---|
| `up-red-01-web-exploitation-fundamentals.yaml` | C537 | 6 | m1 s1 — log on to a-kali01 and resolve t-web01/t-web02 against t-dns01 (up-red-01:49-51). Needs a-kali01, t-dns01 with records for both web hosts, and reachable HTTP services; none of these can be substituted |
| `up-idm-02-active-directory-attack-defense.yaml` | C508 | 6 | m1 s1 — SSH to a-kali01 and start the graph collection against the target domain (up-idm-02:47-49). Fails if a-kali01 lacks SSH + graph tooling or if the directory is unreachable (see §2 gap) |
| `up-red-03-c2-infrastructure-operational-security.yaml` | C539 | 6 | m1 s2 — inspect the services and listening ports on a-teamserver (up-red-03:50-51). Fails if a-teamserver is not built with its C2 services |
| `up-red-04-adversary-emulation-planning-att-ck-based.yaml` | C540 | 6 | m1 s2 — read the fw01 rules applying to attacker_infra (up-red-04:52-54). Fails if fw01 is not the edge with the five-VLAN rule set; m1 s3 additionally needs the DMZ services answering (up-red-04:55-57) |
| `up-red-05-post-exploitation-lateral-movement-lab.yaml` | C541 | 6 | m1 s2 — from a-kali01, test t-web01 on the ports the internet rules allow (up-red-05:57-59). Fails if the attacker_infra→target_dmz rule or the DMZ web service is missing; m1 s4/s5's 389/1433/445 probes (up-red-05:64-70) additionally pin the inter-segment rule set |

## 7. Summary of gaps a build must close (all UNKNOWN from source)

1. **Staged evidence** (§4a): ten of the eleven pre-captured artefacts the courses read
   are not declared anywhere in the template — only t-fs01's `pre_staged_data` is.
2. **Accounts and credentials** (§3): per-student web-application accounts, SSH
   credentials on a-kali01, and the logon accounts for t-dc01 / t-ws01 / p-jump01 /
   t-web01 / t-dns01 / fw01 are assumed but never declared.
3. **Directory reachability** (§2): C508's graph collection from a-kali01 to
   targetcorp.local has no permitting firewall rule; the transport is UNKNOWN.
4. **Service ports**: the template declares service names without ports for every
   attacker-side host and for the web/mail/DNS targets; every course step that needs a
   port records it as an observed outcome. A build check must confirm each declared
   service actually listens and note its port.
5. **Stale totals**: `resource_totals.total_vms: 30` vs 28 declared nodes
   (template.yaml:395 vs :12). ADR 0004 corrected the former `vm_count` 30-vs-28
   mismatch on 2026-10-09 (docs/adr/0004-scheduler-module.md:354-357); these residual
   fields are covered by no ADR or test.

## 8. Stop conditions — not derivable from source

Whether each check above *passes* on a real build (which ports answer, which accounts
log in, what the zone transfer returns, what the C2 listeners are configured with) needs
a live range and is deliberately left UNKNOWN. This checklist states only what the
source requires a build to make possible.
