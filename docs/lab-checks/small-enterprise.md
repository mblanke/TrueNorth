# Lab acceptance checklist — range template `small-enterprise`

On-site acceptance checklist for `content/ranges/small-enterprise/template.yaml`,
derived from repository sources only (the template plus every course whose
`lab_range.template` is `small-enterprise`). No live range was consulted; wherever a
check needs a running range to confirm, it is marked UNKNOWN rather than guessed.

Ticket: TN10X-F7-template-acceptance. Derived at base commit `d5851f6d`.

Labels used throughout:

- **OBSERVED** — stated in a cited source file:line (or in command output shown here).
- **INFERRED** — derived from cited observations; the reasoning is stated.
- **UNKNOWN** — not derivable from the sources read; needs a live range or another owner.
- **PROPOSED** — a suggested acceptance step that no source requires.

## 1. Nodes

All facts in this table are **OBSERVED** from `content/ranges/small-enterprise/template.yaml`
(nodes at lines 60–124; ids, OS, role, address, VLAN, declared services, tags).

| Node id | Name | OS | Role | Address (VLAN) | Declared services | Tags | Specs |
|---|---|---|---|---|---|---|---|
| jump-box | Jump Host | ubuntu-2404 | jump_host | 10.0.1.10 (mgmt) | ssh | mgmt | 2 c / 4 GiB / 60 GB |
| dc-01 | Domain Controller | windows-server-2022 | domain_controller | 10.0.2.10 (srv) | active_directory, dns | srv | 2 c / 4 GiB / 60 GB |
| ws-01 | Workstation 1 | windows-11 | workstation | 10.0.3.10 (user) | — (none declared) | user, users | 2 c / 4 GiB / 60 GB |
| ws-02 | Workstation 2 | windows-11 | workstation | 10.0.3.11 (user) | — (none declared) | user, users | 2 c / 4 GiB / 60 GB |
| ws-03 | Workstation 3 | windows-11 | workstation | 10.0.3.12 (user) | — (none declared) | user, users | 2 c / 4 GiB / 60 GB |
| fw-01 | Firewall | pfsense | firewall | 10.0.1.1 (mgmt; one NIC per VLAN at .1, lines 117–124) | — (appliance) | — | 2 c / 2 GiB / 20 GB |

- `vm_count: 6` matches the six nodes (**OBSERVED**, template.yaml:13). The field is
  kept honest by `tests/contracts/test_template_vm_count.py:41-45`, which renders the
  template with the worker's real code (**OBSERVED**, test source).
- Tag roles: `user` is the host/src tag scenarios aim at; `users` is ransomware-lite's
  phishing target group (**OBSERVED**, template.yaml:81–82). Both, plus `mgmt` and
  `srv`, must resolve after build — pinned by
  `tests/contracts/test_small_enterprise_scenarios.py:121-125` (**OBSERVED**, test source).
- Host names `dc-01`, `ws-01`, `ws-02`, `ws-03`, `jump-box` and address `10.0.2.10`
  must all resolve; scenarios reference them by name and by address
  (**OBSERVED**, `tests/contracts/test_small_enterprise_scenarios.py:41-46,121-125`).

## 2. Networks and addressing

All **OBSERVED** from template.yaml unless noted.

| VLAN | Name | CIDR | Purpose (template's words) |
|---|---|---|---|
| 10 | mgmt | 10.0.1.0/24 | Management: the jump host and the firewall's management leg (lines 28–31) |
| 20 | srv | 10.0.2.0/24 | Servers: the domain controller (lines 32–35) |
| 30 | user | 10.0.3.0/24 | User workstations (lines 36–39) |

- fw-01 holds address `.1` on every VLAN (10.0.1.1 / 10.0.2.1 / 10.0.3.1) and is each
  network's gateway (**OBSERVED**, template.yaml:41, 110–124; the `.1` convention is
  applied by the worker's renderer per the template comment at line 117).
- Template inputs: `range_id`, `cidr_mgmt`, `cidr_srv`, `cidr_user` (defaults
  10.0.1.0/24, 10.0.2.0/24, 10.0.3.0/24). Template output: `jump_host_ip`
  (**OBSERVED**, template.yaml:18–24).
- Firewall rules exactly two are defined (**OBSERVED**, template.yaml:44–58):
  - `user-to-srv-ad`: user → srv, allow, ports 53, 88, 123, 135, 389, 445, 464, 636
    and 49152-65535 (DNS, Kerberos, NTP, RPC endpoint mapper, LDAP, SMB, kpasswd,
    LDAPS, Windows dynamic RPC; lines 45–53).
  - `mgmt-to-all`: mgmt → `*`, allow, all services (lines 54–58).
- Anything not listed is pfSense's default deny, and nothing leaves the range but depot
  traffic — stated by the template comment (lines 41–43, citing
  `worker/pfsense_config.py`). The statement is **OBSERVED** as the template's claim;
  runtime enforcement was not executed for this document (**INFERRED**: the on-site
  check "default action denies" in §5 is the acceptance evidence).
- Policies: `egress: restricted`, `east_west: allowed_by_vlan`
  (**OBSERVED**, template.yaml:126–128). Consequence for labs: workstation-to-server
  traffic is allowed only on the `user-to-srv-ad` ports; egress beyond the range is
  expected to fail (**INFERRED** from the rules plus c103:247 and c103:249, where a
  trace to a destination outside the range stops at the firewall).
- Courses read this rule set as exactly two named rules and rely on the names:
  up-gov-01:134 "Record the two fw-01 rules, user-to-srv-ad and mgmt-to-all" and
  up-gov-02:242 "the rule that allows every service from the management network"
  (**OBSERVED**, course steps).

## 3. Accounts the courses assume

Defined by the template (**OBSERVED**, template.yaml:130–142; passwords are generated
once per range and shown, sealed, on the range's Credentials panel and staff-only wiki
page, lines 130–133):

| Machine | Username | Role | Student-visible | Notes |
|---|---|---|---|---|
| dc-01 | Administrator | admin | no | Local Administrator; also the first domain admin once AD DS is promoted (lines 135–136) |
| ws-01 / ws-02 / ws-03 | Administrator | admin | no | Built-in Administrator (Sysprep) (lines 137–139) |
| jump-box | ubuntu | admin | no | Template's cloud-init install user (lines 132, 140) |
| jump-box | analyst | user | **yes** | "The way in: SSH on the jump host" (lines 141–142) |

Course assumptions:

- **analyst over SSH is the universal entry.** All 20 courses and all 120 lab modules
  begin "Log on to jump-box over SSH" with the analyst account — **OBSERVED** at
  c101:56, c102:54, c103:54, c104:56, c105:54, c203:58, c211:61, up-cti-01:53,
  up-cti-03:51, up-cti-06:49, up-dfi-01:52, up-dfi-05:54, up-gov-01:49, up-gov-02:54,
  up-gov-06:48, up-ir-01:54, up-ir-02:52, up-ir-04:51, up-ir-05:53, up-ot-06:53
  (module 1 step 1 of each; later modules repeat it). If the analyst account or the SSH
  service is missing, every course fails at its first step.
- **A "standard user" domain account on the workstations is assumed but not defined by
  the template.** Steps sign in to ws-01 as "the standard user (account) the instructor
  supplies/assigned": up-cti-03:139, c102:339 and c102:426 ("the account your instructor
  assigned"), up-ir-05:234–235, up-ot-06:56–57 and up-ot-06:443–444. The template's `accounts:` list has no such account
  (**OBSERVED**, template.yaml:134–142), so who provisions it is **UNKNOWN** from the
  template — instructor setup notes own it: "Create or confirm the standard user account
  on ws-01 that Students sign in with" (instructor/c103:133–136). Acceptance must confirm
  the credential exists before the labs that need it.
- The `ubuntu` account is not named by any course step read for this document
  (**INFERRED** from the course reads above; it exists for install/bootstrap per
  template.yaml:132, 140).
- Credentials reach students via the Credentials panel; instructor material expects it
  visible during lab setup (**OBSERVED**, instructor/up-ir-01-first-responder-playbooks.yaml:142).
- pfSense keeps its own appliance config login (**OBSERVED**, template.yaml:133); its
  username is not stated in the template (**UNKNOWN**).

## 4. Services the course steps name

- **SSH on jump-box** — declared `services: [ssh]` (**OBSERVED**, template.yaml:69) and
  exercised by every course's first step (§3). The TCP port is never numbered in the
  template or the course steps read (**UNKNOWN**; the SSH default is not citable from
  these sources).
- **Active Directory + DNS on dc-01** — declared `services: [active_directory, dns]`
  (**OBSERVED**, template.yaml:79). Course steps rely on:
  - name lookups answered by dc-01, including a response code for a non-existent name:
    c103:330–336, up-cti-01:59, up-ot-06:66 (**OBSERVED**);
  - the domain password policy readable from dc-01 (minimum length, lockout threshold,
    lockout duration): up-cti-03:305, c105:408, c102:320, up-ot-06:440–441,
    up-ir-05:59–60 (**OBSERVED**);
  - domain sign-ins from the workstations, with the services dc-01 answering named as
    "Kerberos, LDAP and SMB": up-cti-03:139; successful sign-ins by account with source
    host: up-ir-02:61–62, c203:498–500 (**OBSERVED**);
  - domain shares: SYSVOL opened from ws-01 (c203:418), an administrative share on
    dc-01 probed by a standard user (up-ot-06:443–444) (**OBSERVED**);
  - domain groups / administrative accounts with member counts (up-ot-06:340–342) and
    the local Administrators group on ws-01 (c102:339) (**OBSERVED**);
  - a remote administration session from jump-box to ws-01 (up-cti-03:137)
    (**OBSERVED**); which protocol carries it is not named (**UNKNOWN**).
- **fw-01 management surface** — rule listing and editing through "the fw-01
  administration interface" opened from jump-box: c211:254, up-ir-04:56–57,
  up-ot-06:250–251 (**OBSERVED**); the management page must load from the management
  network, and the same test from ws-01 is recorded "whatever the range shows"
  (up-ot-06:249–254, 347–351) (**OBSERVED** — the check, not the outcome); the status
  page shows firmware/version information (up-ot-06:256–257) (**OBSERVED**).
- **fw-01 logging** — connection records inbound and outbound for a run window
  (up-dfi-05:58, 145, 493) and "traffic and block summaries the firewall on fw-01 can
  report" (up-gov-06:50) (**OBSERVED**).
- **Central log collection** — a "log collection area" reachable from jump-box
  (up-gov-06:222) and log destinations on ws-02/dc-01 "which central log each host
  forwards to" (up-ir-01:233–235) (**OBSERVED** as course assumptions). How host log
  forwarding is implemented on this template is not in the template or the worker files
  read (**UNKNOWN** — confirm on site).
- **Exercise feed and range log (scenario-provided, not template services)** — the
  exercise alert feed (up-ir-02:52), the exercise timeline for the ransomware-lite run
  (up-dfi-05:54), and "bad.example query records in the range log" (c211:448)
  (**OBSERVED**). These come from the `ransomware-lite` scenario binding, not from
  template.yaml; the scenario–template contract is pinned by
  `tests/contracts/test_small_enterprise_scenarios.py` (**OBSERVED**, test source).
- **Instructor-supplied artefacts** — the template builds none of these; the instructor
  must bring them (**OBSERVED** as course assumptions): fw-01 rule-set extract
  (c105:59, c103:417), password-policy extract (c105:408), shared-folder rights extract
  (c105:235), sign-in log on jump-box (c105:493), leaver list (c203:64), backup record
  (up-dfi-05:234), memory image for Volatility 3 (up-dfi-01:312), approved-destination
  list (up-ot-06:159–160), incident summaries (up-ir-04:59). The instructor setup notes
  undertake that the rule-set extract is "taken from the baseline rule set unmodified"
  (instructor/c105:102–103) — that it matches a given deployment is **UNKNOWN** until
  compared on site.
- Ports citable from source are only the `user-to-srv-ad` list (template.yaml:51),
  TCP 3389 as an expected-**failing** probe from jump-box (c105:151, "a port the recorded
  services do not cover"; instructor/c105:104–106 fixes the expectation: dc-01 answers no
  service on that port at baseline, so the expected outcome is a failed connection), and
  port 445 as the user→srv probe the response toolkit runs from the workstations
  (c211:257–259: expected to **fail** once the containment rule stands; c211:347–349:
  result recorded as a fact line, outcome not fixed in source) (**OBSERVED**). Any other
  port or command syntax in an acceptance script would be
  invented (**UNKNOWN** by policy: do not add one).

## 5. Post-build checks

Only outcomes and ports citable from the sources above. Where the sources name an
action but no tool ("path trace", "packet capture", "list the rules"), the on-site
engineer chooses the tool; the expected outcome is the contract. A blank "expected"
means the source records the check but explicitly not the outcome.

| # | Check | How (source-named action) | Expected | Source | Label |
|---|---|---|---|---|---|
| 1 | jump-box reachable, analyst logs in | SSH logon to jump-box as analyst | session opens; start time recordable | any course M1 step 1 (e.g. c105:54) | OBSERVED |
| 2 | dc-01 answers | "From jump-box, ping dc-01 once and record the address that answered" | 10.0.2.10 answers | c103:59–60 | OBSERVED |
| 3 | unused address silent | "ping one unused address in the management network, for example 10.0.1.99" | no answer ("times out", per instructor/c103:105) | c103:59–60 | OBSERVED |
| 4 | all five hosts reachable from jump-box | "test reachability of fw-01, dc-01, ws-01, ws-02 and ws-03" | recorded per host; outcome whatever the range shows | up-ir-04:52–54 | OBSERVED (check; outcome not fixed) |
| 5 | gateways answer on each network | "test reachability of each gateway address on fw-01 in the instructor's addressing plan" | 10.0.1.1 / 10.0.2.1 / 10.0.3.1 respond | up-cti-01:56–57 | OBSERVED (check; outcome not fixed) |
| 6 | exactly two firewall rules, by name | "list the rules on fw-01 … record the exact name of the rule that would carry containment traffic" | `user-to-srv-ad`, `mgmt-to-all`, in order | up-ir-01:56–58, up-gov-01:134, instructor/c103:133 | OBSERVED |
| 7 | default deny behind the rules | "record the default action fw-01 takes for connection attempts that no rule allows" | deny (template.yaml:41–43 claim) | up-cti-06:55, up-ot-06:437–438 | OBSERVED as template claim; on-site result UNKNOWN |
| 8 | 3389 unanswered mgmt→srv | "attempt one connection to dc-01 on TCP port 3389, a port the recorded services do not cover" | failed connection — dc-01 answers no service on that port at baseline | c105:151; instructor/c105:104–106 | OBSERVED |
| 9 | 445 probe user→srv via response toolkit | "run one port 445 connection test on ws-02 towards dc-01, so the connection originates at ws-02" | fails after the containment rule (c211:257–259); the ws-01 run's result is recorded as a fact line, outcome not fixed (c211:347–349) | c211:257, 347 | OBSERVED |
| 10 | DNS answers, NXDOMAIN behaviour | "look up one name against dc-01 … look up a name that does not exist … record the response code" | answers; response code recorded | c103:330–336 | OBSERVED (check; exact code not fixed) |
| 11 | domain services answer | "test whether dc-01 answers on one service the recorded rule permits"; "test the domain logon services on dc-01 and record the service ports" | ports of user-to-srv-ad answer | up-cti-01:151–152, up-cti-01:62 | OBSERVED (check; per-port results not fixed) |
| 12 | workstation domain sign-in | "From ws-01, sign in to the domain as the standard user and record the services dc-01 answered with, such as Kerberos, LDAP and SMB" | sign-in succeeds; services recorded | up-cti-03:139 | OBSERVED (needs §3 standard user — provisioning UNKNOWN) |
| 13 | analyst is *not* an admin on dc-01 | analyst attempts an administrative action on dc-01 and records the exact denial text | denial observed (the denial is the teaching point) | up-gov-01:221 | OBSERVED (check; exact text UNKNOWN) |
| 14 | fw-01 management page | "From jump-box on the management network, open the management page of fw-01 and record whether it loads" | loads from mgmt; from ws-01 record "whatever the range shows" | up-ot-06:249–254 | OBSERVED |
| 15 | SSH logon as `ubuntu` or Administrator | — | not required by any course step read | §3 | UNKNOWN (existence check PROPOSED: credentials visible, sealed, on Credentials panel — template.yaml:130–133) |
| 16 | listening services inventory | "List the listening ports and record each port and address, with the owning service" (jump-box) | inventory recorded; no fixed list in source | c102:508–511 | OBSERVED (check; list not fixed) |

Not derivable from source, therefore **UNKNOWN** (do not script these from invention):
exact command syntax for traces and captures ("run one path trace", c103:156,
"start a packet capture", c103:330 — tools unnamed); whether ws-01's management-page
test *should* succeed (sources explicitly leave the outcome open, up-ot-06:253–254);
whether the central log collection area exists on a fresh deploy (§4).

## 6. Courses using this template

**OBSERVED**: exactly 20 course files under `content/courses/` declare
`lab_range.template: small-enterprise`, six modules each (120 `lab_range` blocks;
line numbers below). No `content/courses/instructor/*.yaml` copy declares the template
— instructor files carry expected values and setup notes instead (e.g.
instructor/up-gov-02:98–111) (**OBSERVED**, grep across `content/courses`).

"First step that would fail if the template were wrong" cites each course's module 1
step 1 (universal SSH logon, §3) plus, where the course's module 1 goes further, the
first step that pins a specific template fact. C1xx courses are drafts with
`provenance: unsourced` (**OBSERVED** in the course headers, e.g. c105:1–25) — the labs
still define the template expectations recorded here, but their pedagogy is
unvalidated (**INFERRED**).

| Course | Modules | Scenario binding | First step that fails if the template is wrong |
|---|---|---|---|
| c101-computer-architecture-systems | 6 | — | M1 s1 SSH logon as analyst (c101:56); first hardware pin: `lscpu` on jump-box (c101:59) |
| c102-operating-system-fundamentals | 6 | — | M1 s1 SSH logon with analyst account (c102:54); first cross-host pin: local Administrators on ws-01 (c102:339) |
| c103-intro-to-networking | 6 | — | M1 s1 SSH logon (c103:54); first network pin: ping dc-01 / unused 10.0.1.99 (c103:59–60) |
| c104-intro-to-programming-python | 6 | — | M1 s1 SSH logon + `python3` version on jump-box (c104:56); runs on jump-box only |
| c105-foundations-of-cybersecurity | 6 | — | M1 s1 SSH logon (c105:54); first firewall pin: 3389 probe to dc-01 (c105:151, module 2) |
| c203-identity-access-management | 6 | — | M1 s1 SSH logon (c203:58); first data pin: account inventory vs leaver list (c203:64) |
| c211-lab-simulated-incident-exercise | 6 | ransomware-lite, modules 1–2 (c211:58, 154) | M1 s1 SSH logon (c211:61); first scenario pin: phishing target group announced at first inject (c211:159, module 2) |
| up-cti-01-threat-intelligence-foundations | 6 | — | M1 s1 SSH logon with analyst (up-cti-01:53); gateway + name-service tests (up-cti-01:56–62) |
| up-cti-03-threat-actor-profiling-with-att-ck | 6 | — | M1 s1 SSH logon as assigned analyst (up-cti-03:51); first pin: three range networks and each host's role (up-cti-03:135, module 2) |
| up-cti-06-strategic-intelligence-for-leadership | 6 | — | M1 s1 SSH logon as analyst + record hosts and roles (up-cti-06:49); default-action check (up-cti-06:55) |
| up-dfi-01-linux-forensics | 6 | — | M1 s1 SSH logon (up-dfi-01:52); **uses jump-box only** — no dc-01/ws-01/fw-01 reference in the file (grep, OBSERVED) |
| up-dfi-05-ransomware-incident-forensics | 6 | ransomware-lite, modules 1–2 (up-dfi-05:51, 136) | M1 s1 SSH logon + open exercise timeline (up-dfi-05:54); first scenario pin: name-resolution records on dc-01 (up-dfi-05:56) |
| up-gov-01-foundations-of-security-governance | 6 | — | M1 s1 SSH logon (up-gov-01:49); first firewall pin: both rule names recorded (up-gov-01:134, module 2) |
| up-gov-02-risk-register-craft-assessing-prioritising-risk | 6 | — | M1 s1 SSH logon (up-gov-02:54); first pin: default-deny test from ws-01 (up-gov-02:249, module 2) |
| up-gov-06-security-metrics-executive-communication | 6 | — | M1 s1 SSH logon (up-gov-06:48); first pin: fw-01 traffic/block summaries (up-gov-06:50) |
| up-ir-01-first-responder-playbooks | 6 | — | M1 s1 SSH logon (up-ir-01:54); first pin: fw-01 rule names (up-ir-01:56–58) |
| up-ir-02-live-triage-containment-lab | 6 | ransomware-lite, all 6 modules (up-ir-02:49, 142, 234, 325, 416, 509) | M1 s1 SSH logon as assigned analyst + open exercise alert feed (up-ir-02:52) |
| up-ir-04-crisis-communication-stakeholder-management | 6 | — | M1 s1 SSH logon (up-ir-04:51); first pin: reachability of fw-01, dc-01, ws-01…ws-03 (up-ir-04:52–54) |
| up-ir-05-tabletop-exercise-facilitation | 6 | ransomware-lite, modules 2 and 6 (up-ir-05:139, 493) | M1 s1 SSH logon (up-ir-05:53); first pin: fw-01 rules in order (up-ir-05:55–57) |
| up-ot-06-emerging-tech-risk-clinic-ai-saas-edge | 6 | — | M1 s1 SSH logon as analyst + list hosts reachable from jump-box (up-ot-06:53–54) |

Cross-course reads:

- Most courses only *read* fw-01's baseline rule set. Student steps that change it
  appear in c211 (add a deny rule blocking ws-02, c211:254) and up-ir-02 (write the rule
  blocking a command channel, up-ir-02:424–426); an instructor also disables one rule of
  their choice before an up-ir-01 lab (instructor/up-ir-01:147–151)
  (**OBSERVED**, sweep for rule-writing actions across `content/courses`) — acceptance
  should confirm rules can be added, disabled and reverted to the two-rule baseline.
- Scenario-dependent courses need the `ransomware-lite` artefacts (alert feed, exercise
  timeline, name-resolution records on dc-01, connection records on fw-01) to exist at
  run time: c211, up-ir-02, up-dfi-05, up-ir-05 (**OBSERVED**, scenario lines above).
  The scenario→template name/address contract is enforced by
  `tests/contracts/test_small_enterprise_scenarios.py` (**OBSERVED**).
- up-dfi-01 and c104 exercise only jump-box; a broken dc-01 or fw-01 does not fail
  their labs (**OBSERVED**, file-wide grep for host names).
