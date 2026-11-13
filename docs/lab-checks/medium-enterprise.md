# Acceptance checklist — range template `medium-enterprise`

Ticket TN10X-F7-template-acceptance. Derived from source only, at base commit d5851f6d.
Source of truth: `content/ranges/medium-enterprise/template.yaml` (version "1.0",
template.yaml:3), plus every course module whose `lab_range.template` is
`medium-enterprise` (18 courses, 6 modules each — see "Courses using this template").

Labels: **OBSERVED** = read at the cited file:line. **INFERRED** = reasoned from what was
read. **UNKNOWN** = not derivable from source; do not guess it on site. No course step or
template line prescribes an exact shell command or tool name, so every check below is
phrased as an observable outcome; the method (which command) is UNKNOWN unless a citation
says otherwise. Course citations (`cNNN` / `up-…`) point at the learner file under
`content/courses/`, at the step's `hosts:` line (occasionally the `action:` line that
follows it).

---

## 1. Nodes

OBSERVED (all rows: template.yaml:58–136).

| id | name | role | OS | vlan | ip | services (template) |
|----|------|------|-----|------|----|---------------------|
| dc01 | Domain Controller | domain_controller | windows-server-2022 | servers | 10.10.200.10 | active_directory, dns, dhcp |
| fs01 | File Server | file_server | windows-server-2022 | servers | 10.10.200.20 | smb, dfs |
| web01 | Web Server | web_server | ubuntu-2404 | dmz | 10.10.30.10 | nginx, php |
| mail01 | Mail Server | mail_server | ubuntu-2404 | dmz | 10.10.30.20 | postfix, dovecot |
| ws01 | Workstation 1 | workstation | windows-11 | corporate | 10.10.100.10 | — |
| ws02 | Workstation 2 | workstation | windows-11 | corporate | 10.10.100.11 | — |
| siem01 | SIEM | siem | ubuntu-2404 | management | 10.10.40.10 | opensearch, filebeat |
| fw01 | Firewall | firewall | pfsense | management | 10.10.40.1 | one NIC per zone (template.yaml:128–136) |

All eight node ids are named by course steps (every id appears in the hosts lists of the
courses in §6) — OBSERVED from the step extraction summarised in §6.

## 2. Networks and addressing

OBSERVED (template.yaml:10–56; the per-VLAN `gw .1` addresses are fw01's per-zone
`interfaces` entries, template.yaml:128–136):

| vlan | id | cidr | hosts |
|------|----|------|-------|
| corporate | 100 | 10.10.100.0/24 | ws01 (.10), ws02 (.11), gw .1 |
| servers | 200 | 10.10.200.0/24 | dc01 (.10), fs01 (.20), gw .1 |
| dmz | 300 | 10.10.30.0/24 | web01 (.10), mail01 (.20), gw .1 |
| management | 400 | 10.10.40.0/24 | siem01 (.10), fw01 (.1) |

fw01 is every zone's gateway at `.1` and each VM's default route (template.yaml:25–27,
127). Firewall rules (names are cited by course steps, see §4):

| rule | src → dst | ports | cited by |
|------|-----------|-------|----------|
| corp-to-servers | corporate → servers | (none — dynamic RPC, template.yaml:33) | c201-network-defense-firewalls.yaml:247 names the rule; up-red-02:528 (quiz option) |
| corp-to-dmz | corporate → dmz | 80, 443, 25, 587, 143, 993 | up-gov-05:242 names the rule |
| dmz-to-servers | dmz → servers | 53, 88, 389, 636 | up-net-02:229 ("the rule that carries dmz-to-servers traffic"); up-net-02:414 restates the port intent |
| logs-to-siem | * → management | 514, 5044, 9200 | rule restated by steps c110:317, up-idm-04:520; c401:422 confirms fw01 ships its decisions to the SIEM |
| mgmt-to-all | management → * | (any) | c401:419, up-idm-04:62/244 describe it |
| default | everything else | deny | template.yaml:25; exercised by c201:67, c401:154, c403:149, up-gov-05:151 |

## 3. Accounts the courses assume

From the template (OBSERVED, template.yaml:138–152 — passwords are generated per range and
shown on the Credentials panel):

| machine | username | template role | courses assume |
|---------|----------|---------------|----------------|
| dc01 | Administrator | admin, student-invisible; "also the first domain admin once AD DS is promoted" | log on to dc01 with the administrator account: up-idm-04:241, up-idm-04:516 ("find the built-in Administrator account") |
| fs01 | Administrator | admin | no step logs in to fs01 as Administrator — UNKNOWN who administers fs01 (steps only touch its shares) |
| ws01, ws02 | Administrator | admin | log on to ws01 with the administrator account: up-idm-04:50 |
| web01 | ubuntu | admin (cloud-init) | "log on to web01 over SSH with the account your instructor assigned": c108:58, up-idm-01:53/144 — the course steps never name `ubuntu`; mapping INFERRED from template.yaml:148 |
| mail01 | ubuntu | admin (cloud-init) | same pattern: c108:146, c108:325 (from siem01) |
| siem01 | ubuntu | admin | no step uses it — UNKNOWN |
| siem01 | analyst | user, **student-visible** | "log on to siem01 with the analyst account" — cited in 15 of the 18 courses: c108:504, c109:61, c110:57, c201:327, c205:57, c401:235, c402:55, c403:54, c404:55, up-gov-03:380, up-gov-05:51, up-idm-01:50, up-idm-03:402, up-net-02:49, up-red-02:49 |

Assumed by steps but **not** in the template accounts list:

- **Domain user accounts** on ws01/ws02: "sign in to ws01 with the domain account the
  instructor assigned" (up-idm-01:233, up-idm-01:509); "the standard user your instructor
  assigned" (c403:142, up-idm-03:50, up-idm-03:138); the *same* standard account works on
  ws02 (up-idm-03:147). Requires the workstations to be domain-joined and a domain user to
  exist. **UNKNOWN** how these are created — they are not template accounts; the
  instructor packs carry "lab setup" (content/courses/instructor/c106-cryptography-basics.yaml:2).
- **A "standard directory user"** for the LDAP bind on dc01 (c109:346). UNKNOWN.
- **An SSH key** "your instructor issued" for mail01 (c108:337). UNKNOWN material.
- **The SIEM search-page login** "the instructor supplies" (up-idm-01:324) — consistent
  with template.yaml:152: "OpenSearch logins are scenario content". UNKNOWN credential.
- An "administrative account" on web01 for the sudo exercise (c108:233) — plausibly
  `ubuntu` with sudo, INFERRED only.

## 4. Services the course steps name

Per host; every row is OBSERVED at the cited step unless marked.

**dc01** — DNS server answering name queries (c109:64, up-red-02:139, c404:60,
up-ir-06:51, c403:57 "look up the addresses of dc01, fs01, web01 and mail01"); directory
answering an unauthenticated bind (c109:342; "dc01 is pre-configured to accept one for
this exercise", c109:331 — lab setup, UNKNOWN) and a user bind (c109:346); sign-in service
answering a connection attempt (c110:319); Kerberos issuing a TGT and an fs01 service
ticket (up-idm-01:236, up-idm-01:239); domain password policy readable (up-gov-03:297,
up-gov-05:57, up-gov-05:148, c402:152, c402:323, up-idm-03:143); domain-admin group
membership enumerable (up-gov-05:238, up-idm-03:140, up-idm-04:53); DHCP role named
(up-ir-06:51). DHCP lease behaviour is **UNKNOWN** — no step checks a lease.

**fs01** — file share answers clients (c110:146, c201:243, c205:248/254/534/537,
c401:62, c401:149, c403:144, c404:63 "the shares it offers to domain users", up-gov-03:216,
up-idm-03:57/145/147/321, up-net-02:145); DFS namespaces (up-ir-06:54); administrative
share (up-idm-03:145); backup catalogue with restore points and a verification report
(up-ir-06:133/135); malware quarantine area (up-ir-06:229); a stored runbook (up-ir-06:483);
the accounts its services run under are listable (up-idm-04:56). The backup catalogue,
quarantine area and runbook are instructor-placed content — **UNKNOWN** whether the bare
template creates them (stop condition: needs a live range).

**web01** — web service on port 80 (up-gov-05:242) and HTTPS on 443 (c201:163, c402:238,
up-idm-03:235); presents a certificate chain (c106:317, c106:404, c109:160); returns
response headers and a home page (c109:163); states a software name/version in its banner
or version header (c401:329); reachable by browser from ws01 (c109:166); SSH
login (c108:58, c403:61, up-idm-01:53/144/417, up-idm-01:512); shadow password file
readable with granted elevation (up-idm-01:148); deployed application and configuration
files searchable for credentials (up-idm-04:334); a captured OAuth authorisation flow file
readable (up-idm-01:417–429); web01→dc01 name resolution works under dmz-to-servers
(up-idm-03:230, up-net-02:232/417); web01→fs01 on the file-share port is refused
(up-gov-03:216, c205:254, up-idm-03:233, up-net-02:235/414). SSH is required by many steps
but is **not** in web01's template `services` list — INFERRED present on the Ubuntu image,
UNKNOWN as a template guarantee (gap worth closing).

**mail01** — SMTP service returning a greeting that states its software name and version
(up-net-02:419; c401:333 "record the software name and version its greeting states");
message-submission port
reachable from ws01 (up-gov-03:219; template corp-to-dmz allows 25 and 587,
template.yaml:37); IMAPS on 993 (c402:238); mailboxes the instructor assigns are readable
from ws01 (c109:250); a mail-transfer log readable from siem01 (c109:256); mail services'
credential settings readable at start-up (up-idm-04:156); SSH login (c108:146, c403:61).
SSH gap same as web01.

**siem01** — the analyst OS account logs in (§3); a log search / log store interface
(c201:327, c205:337, c401:235, c402:154, c403:227); log sources from every zone visible
with newest timestamps (c205:340, c404:66, c110:315, up-idm-03:404); per-host event counts
(c403:230); sign-in events searchable by authentication package (NTLM vs Kerberos,
up-idm-01:327–333); firewall deny events forwarded from fw01 (up-gov-03:382, c401:425);
records identify a person (c401:241); the log shipper's configuration readable with
credential settings (up-idm-04:153); siem01's own listening sockets and outbound
connections inspectable (c108:504–513).

**fw01** — appliance configuration login (up-gov-03:49); interface list with per-zone
address blocks (c201:61, c205:60); rule list readable, in order (many, e.g. c201:64,
c404:57, up-net-02:139); rule reachable from siem01 over the management network (c109:515,
c403:63, up-gov-05:54); rule-match log showing which rule decided a connection (c201:249,
up-net-02:332/422); state table showing a live session (c201:156, up-net-02:238); rules
editable under instructor approval (up-gov-05:334, up-net-02:326/511) and revertible
(up-net-02:335); outbound blocking/redirect/rate-limit capabilities inspectable (c201:515);
firewall decisions shipped to the logging platform (c401:422, c401:425, up-idm-04:520).

**ws01 / ws02** — domain sign-in (§3); local Administrator (§3); a shared folder on ws02
reachable from ws01 inside corporate (c201:70, c201:331, up-idm-03:318); ws02→dc01
administrative share attempt with an ordinary account fails (up-idm-04:59); ws01→web01 on
a port outside the rule list fails (c201:67, c205:251, up-gov-05:151,
up-idm-03:235); ws01→siem01:443 fails ("a port no rule ... allows
from the workstation network", c403:149); name resolution via dc01 (c404:60, up-ir-06:51,
up-red-02:58); restore target for a small file set (up-ir-06:144); ws02 as the approved-
media build workstation (up-ir-06:222).

## 5. Post-build checks

Every row: OBSERVED outcome with citation; **method UNKNOWN** (no source line names a
command). "From siem01" paths rely on mgmt-to-all; workstation paths on corp-to-servers /
corp-to-dmz.

**Host answers**

| # | check | expected | source |
|---|-------|----------|--------|
| H1 | From siem01, a reachability test to dc01, fs01, web01, mail01 | each responds, with its zone | up-red-02:52; c403:316 |
| H2 | From siem01, ws01 and ws02 answer the same check | answer, or a recorded timeout/error | c403:319 |
| H3 | From siem01, a directory query to dc01 | answers | c110:63, c110:319 |
| H4 | From siem01, a name query to dc01's DNS | answers (test domain named by instructor) | c109:64, up-red-02:139 |
| H5 | From siem01, the file share on fs01 | answers | c110:146, c205:248 |
| H6 | From ws01, the file share on fs01 | connects, rule corp-to-servers carries it | c201:243, up-net-02:145 |

**Service listens**

| # | check | expected | source |
|---|-------|----------|--------|
| S1 | web01 HTTP on port 80 | answers, reports a web service version | up-gov-05:242; c401:329 |
| S2 | web01 HTTPS on port 443 | answers, presents a certificate chain and headers | c201:163, c402:238, c109:160–163 |
| S3 | mail01 IMAPS on port 993 | answers | c402:238 |
| S4 | mail01 SMTP greeting (submission port; corp-to-dmz allows 25 and 587) | greeting returned under corp-to-dmz | up-net-02:419, up-gov-03:219, template.yaml:37 |
| S5 | dc01 Kerberos | TGT on domain sign-in; fs01 service ticket after opening the share | up-idm-01:236–239 |
| S6 | SIEM search page on siem01 | opens with the instructor-supplied login | up-idm-01:324 |
| S7 | SIEM log sources | every zone represented with an event in the last hour | c205:340, c110:315 |
| S8 | fw01 from management | interface list, rule list, rule-match log, state table all readable | c205:60, c201:64/156/249 |
| S9 | ws01↔ws02 shared folder | succeeds inside corporate; no fw01 boundary on the path | c201:70–73 |

**Account logs in**

| # | check | expected | source |
|---|-------|----------|--------|
| A1 | siem01 `analyst` | logs in (OS account) | template.yaml:151; c108:504 |
| A2 | web01 and mail01 SSH login with the assigned account | succeeds | c108:58, c108:146 |
| A3 | dc01 Administrator | logs in; built-in Administrator visible | template.yaml:143; up-idm-04:516 |
| A4 | ws01 Administrator | logs in | template.yaml:146; up-idm-04:50 |
| A5 | domain/standard user on ws01 and ws02 | same account signs in on both | up-idm-03:147; c403:142 — account creation UNKNOWN |

**Negative checks (default deny)**

| # | check | expected | source |
|---|-------|----------|--------|
| N1 | ws01→web01 on a port outside corp-to-dmz | refused or timeout | c201:67, c205:251, up-idm-03:235 |
| N2 | ws01→siem01:443 | refused ("no rule ... allows from the workstation network") | c403:149 |
| N3 | web01→fs01 file-share port | refused (dmz-to-servers allows 53/88/389/636 only) | c205:254, up-net-02:414, template.yaml:43 |
| N4 | after a rule change, revert restores the baseline rule list | matches again | up-net-02:335 |

## 6. Courses using this template

OBSERVED: 18 course files under `content/courses/` declare `template: medium-enterprise`;
in all 18, **all 6 modules** use it → 108 lab sessions. The same 18 exist as staff-only
instructor packs with lab setup and answer keys (`content/courses/instructor/`, header at
instructor/c106:2). One scenario also targets this template:
`content/scenarios/insider-threat-advanced/scenario.yaml:3` (`range_template`). The
template-vs-course count table and the 97-course mapping live in `docs/lab-checks/README.md`
(companion deliverable, not this file).

"First step that would fail if the template were wrong" = the first step whose failure
follows from a template defect (node missing, account missing, service or firewall path
absent). The plain login is listed because it fails first if the node or account is wrong;
the second column fails first if services or addressing are wrong.

| course file | modules | first failing step (node/account) | first step failing on services / paths |
|-------------|---------|-----------------------------------|----------------------------------------|
| c106-cryptography-basics.yaml | 6 | :53 log on to web01 | :317 TLS session to web01's HTTPS service |
| c108-linux-system-administration.yaml | 6 | :58 SSH to web01 | :325 siem01→mail01 SSH (mgmt-to-all path) |
| c109-network-services-protocols.yaml | 6 | :61 analyst on siem01 | :64 DNS query to dc01 |
| c110-intro-to-risk-management.yaml | 6 | :57 analyst on siem01 | :63 directory query to dc01 |
| c201-network-defense-firewalls.yaml | 6 | :59 log on to ws01 | :61 fw01 configuration interface |
| c205-lab-hardened-lab-network.yaml | 6 | :57 analyst on siem01 | :60 fw01 interface list from siem01 |
| c401-professional-ethics-legal-frameworks.yaml | 6 | :56 log on to ws01 | :59 directory on dc01 answers from ws01 |
| c402-capstone-project-planning.yaml | 6 | :55 analyst on siem01 | :58 fw01 rule set from siem01 |
| c403-capstone-implementation.yaml | 6 | :54 analyst on siem01 | :57 name lookups for dc01/fs01/web01/mail01 |
| c404-capstone-assessment-presentation.yaml | 6 | :55 analyst on siem01 | :57 fw01 rule table |
| up-gov-03-policy-to-firewall-rule-making-policy-enforceabl.yaml | 6 | :47 log on to ws01 | :49 fw01 appliance configuration login |
| up-gov-05-audit-assessment-continuous-improvement.yaml | 6 | :51 analyst on siem01 | :54 fw01 administration page from siem01 |
| up-idm-01-authentication-protocols-hands-on.yaml | 6 | :50 analyst on siem01 | :53 siem01→web01 SSH |
| up-idm-03-zero-trust-architecture-in-practice.yaml | 6 | :50 standard user on ws01 | :52 fw01 rule list from management |
| up-idm-04-privileged-access-secrets-management.yaml | 6 | :50 administrator on ws01 | :53 domain-admin groups query to dc01 |
| up-ir-06-recovery-operations-business-restoration.yaml | 6 | :49 log on to ws01 | :51 resolve dc01 by name, list its roles |
| up-net-02-firewall-segmentation-engineering.yaml | 6 | :49 analyst on siem01 | :52 fw01 rule list from siem01 |
| up-red-02-vulnerability-assessment-method.yaml | 6 | :49 analyst on siem01 | :52 reachability to the four server hosts |

## 7. UNKNOWN summary (stop conditions — need a live range or instructor setup)

- Exact commands and tool names for every check in §5: no source line names one.
- Domain-user and directory-user account creation; workstation domain join.
- SSH on web01/mail01: required by steps, absent from the template `services` lists;
  port never named (c108:331 says only "the listener still answers on its port").
- dc01's exercise-specific unauthenticated-bind configuration (c109:331).
- SIEM search login (scenario content, template.yaml:152) and instructor-supplied files:
  ciphertexts, scan reports, captured OAuth flow, policy extracts, authorisation memos,
  baseline documents, backup catalogue, quarantine area, fs01 runbook.
- Whether fw01 deny events actually arrive in the SIEM (up-gov-03:382) and whether DHCP
  leases addresses — both need a built range.
- Certificate content on web01: deployment-dependent, not fixed by the template (compare
  c403:66, which asks the Student to make exactly this distinction).
