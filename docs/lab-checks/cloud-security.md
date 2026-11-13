# Range acceptance checklist — `cloud-security`

Derived **from source only** at base commit `d5851f6d649a55b54f33e700be9d28be399e1302`.
Sources read: `content/ranges/cloud-security/template.yaml`, every course YAML in
`content/courses/` whose `lab_range.template` is `cloud-security` (10 files), and
`content/scenarios/cloud-breach/scenario.yaml` (the only other content bound to this
template). Labels: **OBSERVED** (read at a cited file:line), **INFERRED** (reasoned from
cited observations), **UNKNOWN** (not derivable from source; a live range build is needed).
No port number, URL or command in this document is invented: the template declares
service *names* only, so every port is UNKNOWN.

## 1. Nodes

All values OBSERVED from `content/ranges/cloud-security/template.yaml` (node blocks,
lines 62–271). 16 nodes; `vm_count: 16` (template.yaml:11).

| id | OS | role | IP | VLAN | services declared |
|---|---|---|---|---|---|
| localstack01 | ubuntu-2404 | cloud_sim_aws | 10.80.0.10 | cloud_sim | localstack, s3, iam, lambda, ec2_mock, cloudtrail_sim, sts (:71) |
| azure-mock01 | ubuntu-2404 | cloud_sim_azure | 10.80.0.20 | cloud_sim | azurite, azure_ad_mock, azure_keyvault_mock (:89) |
| terraform01 | ubuntu-2404 | iac_server | 10.80.0.30 | cloud_sim | terraform, consul (:105) |
| k3s-master | ubuntu-2404 | k8s_master | 10.80.1.10 | container_cluster | k3s_server, kubectl, helm (:120) |
| k3s-worker01 | ubuntu-2404 | k8s_worker | 10.80.1.20 | container_cluster | k3s_agent, containerd (:129) |
| k3s-worker02 | ubuntu-2404 | k8s_worker | 10.80.1.21 | container_cluster | k3s_agent, containerd (:145) |
| registry01 | ubuntu-2404 | container_registry | 10.80.1.30 | container_cluster | harbor, trivy (:161) |
| gitlab01 | ubuntu-2404 | scm | 10.80.2.10 | cicd_pipeline | gitlab_ce, gitlab_runner (:174) |
| jenkins01 | ubuntu-2404 | ci_server | 10.80.2.20 | cicd_pipeline | jenkins, docker (:188) |
| argocd01 | ubuntu-2404 | gitops | 10.80.2.30 | cicd_pipeline | argocd (:202) |
| sonarqube01 | ubuntu-2404 | code_analysis | 10.80.2.40 | cicd_pipeline | sonarqube, postgresql (:214) |
| cloud-siem01 | ubuntu-2404 | siem | 10.80.3.10 | monitoring | opensearch, opensearch_dashboards, cloudtrail_ingest, k8s_audit_ingest (:224) |
| falco01 | ubuntu-2404 | runtime_security | 10.80.3.20 | monitoring | falco, falcosidekick (:233) |
| prowler01 | ubuntu-2404 | cloud_audit | 10.80.3.30 | monitoring | prowler, scoutsuite, steampipe (:242) |
| jump01 | ubuntu-2404 | jump_host | 10.80.4.10 | management | guacamole, ssh, aws_cli, az_cli, kubectl (:252) |
| fw01 | pfsense | firewall | 10.80.4.1 (+ .1 in each other VLAN, :261–271) | management | — (interfaces listed, no services key) |

Every one of the 16 node ids is named in at least one course lab step (see §4), so a
missing node breaks at least one course.

### Seeded state the template declares (OBSERVED, template.yaml:72–96, 106–111, 130–152, 163–165, 175–180, 189–194, 203–206)

- localstack01: S3 buckets `company-backups`, `public-assets` (world-readable), `secret-configs`; IAM users `admin`, `dev-deploy` (`iam:*`), `lambda-exec`; Lambda functions `data-processor`, `auth-handler`.
- azure-mock01: tenant `contoso.onmicrosoft.com` (guest enumeration enabled); vaults `prod-secrets`, `dev-secrets` (overly broad access policy).
- terraform01: exposed state file; hardcoded credentials in tfvars.
- k3s-worker01 pods: `web-app` (`vulnerable-web:latest`; ssrf, rce_via_template_injection), `privileged-debug` (`alpine:latest`; privileged, host PID ns).
- k3s-worker02 pods: `crypto-miner-sim` (`miner-sim:latest`; unexpected outbound traffic), `exposed-dashboard` (`kubernetes-dashboard:latest`; no auth, cluster-admin binding).
- registry01: anonymous pull enabled.
- gitlab01: public repo with secrets; CI variable exposure.
- jenkins01: script console without auth; credential store weakness.
- argocd01: default admin password.

## 2. Networks and addressing

OBSERVED, template.yaml:13–60.

| VLAN | name | CIDR | gateway | purpose |
|---|---|---|---|---|
| 400 | cloud_sim | 10.80.0.0/24 | 10.80.0.1 (fw01) | simulated AWS/Azure endpoints |
| 401 | container_cluster | 10.80.1.0/24 | 10.80.1.1 (fw01) | k3s cluster + registry |
| 402 | cicd_pipeline | 10.80.2.0/24 | 10.80.2.1 (fw01) | GitLab, Jenkins, ArgoCD, SonarQube |
| 403 | monitoring | 10.80.3.0/24 | 10.80.3.1 (fw01) | SIEM, Falco, Prowler |
| 404 | management | 10.80.4.0/24 | 10.80.4.1 (fw01) | jump01, fw01 |

Firewall rules (template.yaml:44–60): `cicd_pipeline → cloud_sim` allow; `cicd_pipeline → container_cluster` allow; `monitoring → *` allow; `management → *` allow. **INFERRED:** anything not listed (e.g. `cloud_sim → container_cluster` direct) is not allowed by these rules. The estate-wide network spec states deny-egress by default with the pfSense instance owning each `.1` gateway (content/catalogue/range_network_spec.md:1, 21 — row `cloud-security`), but the template file itself declares no default policy, so what fw01 actually enforces beyond the four rules is UNKNOWN. `up-cld-03` module 1 has Students verify exactly these rule directions from fw01 (up-cld-03-ci-cd-pipeline-security.yaml:50–52).

## 3. Accounts the courses assume

| Account / principal | Where declared or assumed | Evidence |
|---|---|---|
| SSH login on jump01 | Every course's first lab step is "Log on to jump01 over SSH" (10/10 courses). | up-cld-01-cloud-iam-misconfiguration-hunting.yaml:50–51 and the first step of every module in all 10 course files |
| Instructor-assigned jump01 credentials | up-cld-01 module 6 logs on "with the credentials your instructor assigned". | up-cld-01:493–495 |
| Low-privilege cloud principal on localstack01 | up-cld-01 module 4 logs on "as the low-privilege principal your instructor assigned" and traces escalation through `dev-deploy`. | up-cld-01:315–317, 321–323 |
| `dev-deploy` IAM user (wildcard `iam:*`) | Template declares it; up-cld-01 module 4 names "the dev-deploy policy" explicitly; c303 module 2 exercises a "deployment user". | template.yaml:77–78; up-cld-01:321–323; c303-cloud-security-aws-azure.yaml:152–154 |
| `admin`, `lambda-exec` IAM users | Declared in template; not named by any course step. | template.yaml:77 |
| Deployment / limited k3s service account | up-cld-02 module 3 requests a pod list "using the limited service account the instructor assigned". | up-cld-02-container-kubernetes-security.yaml:238–240 |
| argocd01 default admin | rmc-c210 module 4 signs in "with its default account" and expects the password still accepted. | rmc-c210-cloud-native-defense.yaml:331–333; template.yaml:203–205 |
| Anonymous access (no account) to gitlab01 project | rmc-c203 module 3 and up-cld-05 module 5 open the project "without signing in" / "including anonymous access". | rmc-c203-cloud-security-advanced.yaml:235–237; up-cld-05-iac-security-policy-as-code.yaml:397–398 |
| "Plain developer account" on gitlab01 | up-cld-03 module 1 records what members/CI variables are visible to it. | up-cld-03-ci-cd-pipeline-security.yaml:53–55 |
| Unauthenticated jenkins01 script console | Reached without signing in by three courses. | up-cld-03:56–58; rmc-c210:328–330; up-cld-05:399–400 |
| Azure AD tenant `contoso.onmicrosoft.com` | c303 module 1 queries "its simulated directory tenant"; rmc-c203 module 2 lists directory objects "without signing in". | c303:64–66; rmc-c203:148–150; template.yaml:91–93 |

**UNKNOWN:** every password, SSH key and cloud access key. No course or the template
contains one, and none should be invented; seeding credentials is part of the build, not
the template source. Whether jump01 logins go through Guacamole or straight SSH is
UNKNOWN — the template lists both `guacamole` and `ssh` on jump01 (template.yaml:252),
while every course step says "over SSH".

## 4. Services the course steps name

Derived by reading every lab step of the 10 courses. Service names are the courses' own
words; the mapping to the template's `services:` keys is INFERRED where the wording
differs (e.g. "object-storage buckets" → `s3`).

- **localstack01** — storage buckets and their access settings (rmc-c203:55–57; c303:238–240); IAM identities/users and attached policies (rmc-c203:58–60; up-cld-06:235–237); role trust policies (up-cld-01:61–63); Lambda functions (c303:61–63); control-plane audit trail and its delivery target (up-cld-04:50–52, 134–136; c303:415–417); key-creation and console sign-in events (up-cld-04:316–318); the backup bucket's restore point (up-cld-06:417–419).
- **azure-mock01** — key vault service and per-vault access policies (rmc-c203:61–63; up-cld-01:235–237; up-cld-03:145–147); simulated directory tenant and directory objects (c303:64–66; rmc-c203:148–150).
- **terraform01** — state file/endpoint reachable without credentials (up-cld-05:53–54; rmc-c203:229–231); configuration and variable files holding credentials (up-cld-05:55–58; rmc-c203:232–234; up-cld-03:142–144); reusable module directory with published versions (up-cld-05:311–312); assigned stacks (up-cld-05:225–228); pipeline policy jobs run against it (up-cld-05:143–145).
- **k3s-master** — cluster-wide pod/service/image listings (c107:147–149; up-cld-02:53–56; up-cld-03:234–236); service accounts (rmc-c210:145–147); roles and bindings (up-cld-02:232–234; rmc-c210:232–234); serving-certificate details (rmc-c210:148–150); applying admission and network policies (rmc-c203:330–333; up-cld-06:149–151); redeploying declared workloads (up-cld-06:420–422); kubectl named explicitly (up-cld-05:313–314).
- **k3s-worker01** — the `web-app` pod, including an interactive shell and a request "beyond the parameter it was meant for" (c107:240–242; up-cld-02:324–326); `privileged-debug` securityContext (up-cld-02:235–237; rmc-c210:235–237); runtime container listing (up-cld-02:60–62); write attempt against a read-only root filesystem (up-cld-02:419–421).
- **k3s-worker02** — `exposed-dashboard` rendering without sign-in (c107:243–245); suspect-pod identification and isolation reachability (up-cld-06:146–154); dashboard auth setting (rmc-c210:238–240); pod identifier and start time (rmc-c210:418–420).
- **registry01** — anonymous pull / image catalogue (rmc-c203:324–326; c303:327–329; up-cld-02:148–150); the vulnerability scanner's Critical/High counts (up-cld-02:145–147; rmc-c210:325–327); newest images and digests (up-cld-03:494–496); SBOM generation (c107:515–517); the pipeline-pushed image's tag/digest (up-cld-03:231–233).
- **gitlab01** — instructor-seeded repositories and feature specs (c107:59–61); project settings, members, CI variables (up-cld-03:53–55; c107:521–523); branch protection (up-cld-05:138–139); runner sharing and job credentials (up-cld-03:318–320); pipeline runs incl. policy jobs (up-cld-05:142–145); repository-history credential search (up-cld-03:148–150); suspect change and triggered jobs (up-cld-03:488–490).
- **jenkins01** — web interface and script console without sign-in (up-cld-03:56–58; rmc-c210:328–330); credential store protection (up-cld-05:401–402); job history (up-cld-03:491–493).
- **argocd01** — declared workloads for automatic redeployment (up-cld-06:414–416); default-account sign-in (rmc-c210:331–333); policy checks gating changes (rmc-c210:506–508).
- **sonarqube01** — project reports with rule/severity/file findings (c107:330–332, 422–424).
- **cloud-siem01** — dashboards and incident records with their data sources (up-cld-06:55–57); cloud audit ingest event types (up-cld-01:409–411); cluster/k8s audit records (rmc-c210:241–243); forwarded Falco alerts (up-cld-02:327–330); index retention and integrity settings (up-cld-04:140–142); cross-account timelines (up-cld-04:403–405); supplied log samples (c303:424–426); repo/build/registry event correlation (up-cld-03:497–499).
- **falco01** — sensor running and rules loaded (up-cld-02:321–323); alerts for a shell event and for `privileged-debug` (up-cld-02:327–330, 422–424); runtime events for the cluster (rmc-c203:327–329); recent events with rule name and severity (up-cld-03:404–406).
- **prowler01** — posture assessment against localstack01 and azure-mock01 (c303:504–509); audit report with findings per platform (up-cld-06:58–60); trail/delivery configuration checks (up-cld-04:396–399); assessor toolkit grouped by service (up-cld-05:482–483).
- **fw01** — rules listing which networks admit traffic (rmc-c210:65–67; up-cld-03:50–52).
- **jump01** — SSH sessions (all courses); the cloud CLI pointed at the simulated account (up-cld-04:47–49); "cloud and Terraform command line tools" (up-cld-05:52).

## 5. Post-build checks

Only observable outcomes the source supports. **Every port and exact command syntax is
UNKNOWN** — the template declares service names, not ports, and no course step contains a
command line (`commands:` appears only in scenario and inject-pack files, e.g.
content/scenarios/cloud-breach/scenario.yaml:43; grep finds zero in `content/courses/`).
Run each check from jump01 unless the source says otherwise.

| # | Check | Expected after a correct build | Source |
|---|---|---|---|
| 1 | SSH to jump01 | Login succeeds; prompt works. | up-cld-01:50–51 (pattern in all 10 courses) |
| 2 | Cloud CLI on jump01 answers against the simulated account | Trail configuration is returned, including active event classes. | up-cld-04:47–49 |
| 3 | localstack01 object storage and IAM respond | Bucket names `company-backups`, `public-assets`, `secret-configs`; IAM users `admin`, `dev-deploy`, `lambda-exec`; functions `data-processor`, `auth-handler` are listed. | template.yaml:73–81; c303:61–63 |
| 4 | `public-assets` anonymous read | Readable without an account; the other buckets are not the anonymous-read finding. | template.yaml:74–75 |
| 5 | azure-mock01 directory and vaults respond | Tenant `contoso.onmicrosoft.com` queryable; vaults `prod-secrets`, `dev-secrets` listed with access policies. | template.yaml:91–96; c303:64–66 |
| 6 | terraform01 state endpoint without credentials | Responds unauthenticated (this is the intended easy finding) and lists resource addresses; tfvars contain credential values. | template.yaml:106–111; rmc-c203:229–234; up-cld-05:53–58 |
| 7 | k3s-master API answers kubectl | Pod listing across the cluster returns, including `web-app`, `privileged-debug`, `crypto-miner-sim`, `exposed-dashboard`. | c107:147–149, 237–239; up-cld-02:53–56 |
| 8 | k3s-worker02 dashboard without auth | Dashboard renders and reports a privileged (cluster-admin) session. | template.yaml:150–152; c107:243–245 |
| 9 | registry01 anonymous pull + scanner | Anonymous catalogue pull succeeds; Critical/High finding counts are retrievable. | template.yaml:163–165; c303:327–329; up-cld-02:145–147 |
| 10 | gitlab01 anonymous project reachability | The seeded project opens without sign-in; CI variables visible to a developer account. | rmc-c203:235–237; up-cld-03:53–55 |
| 11 | jenkins01 script console unauthenticated | Console responds without sign-in. | template.yaml:190–192; up-cld-03:56–58 |
| 12 | argocd01 default admin login | Default admin password still accepted. | template.yaml:203–205; rmc-c210:331–333 |
| 13 | sonarqube01 report opens | Instructor-supplied project report lists findings with rule, severity, file. | c107:330–332 |
| 14 | cloud-siem01 feeds arriving | Search interface shows each log feed with a recent timestamp. | rmc-c203:412–414 |
| 15 | falco01 sensor up and forwarding | Rules loaded; a generated event appears on falco01 and its forwarded copy on cloud-siem01. | up-cld-02:321–330 |
| 16 | prowler01 assessment runs | Posture assessment completes against localstack01 and azure-mock01 and returns findings with severities. | c303:504–509 |
| 17 | fw01 rules visible | Rule list from jump01 shows the allow rules in §2 (pipeline → cloud_sim, pipeline → containers). | up-cld-03:50–52; template.yaml:44–52 |
| 18 | Account logins | Instructor-assigned jump01 credentials, the assigned low-privilege cloud principal, and the assigned limited k3s service account each authenticate. | up-cld-01:493–495, 315–317; up-cld-02:238–240 |

**UNKNOWN in checks:** TCP ports for every service; whether cloud-siem01's ingest holds
historical audit records for a given exercise window before the course runs (up-cld-01:409–411
and up-cld-04 assume records exist); the exact cloud-CLI profile/alias Students use on
jump01; whether jump01 reachability to each VLAN comes from the `management → *` allow
rule alone (INFERRED yes) or needs more routing.

## 6. Courses using this template

10 courses × 6 modules, every module lab-backed on this template (60 lab modules).
Verified: `grep -c '^- ordinal:' <file>` = 6 for each, and 6 `lab_range` blocks each.

| Course file | Modules using template | First lab step that fails if the template is wrong |
|---|---|---|
| content/courses/c107-secure-software-development.yaml | 6 (all) | Module 1 step 2 (:59–61) — the instructor-seeded repository on **gitlab01** does not exist |
| content/courses/c303-cloud-security-aws-azure.yaml | 6 (all) | Module 1 step 2 (:61–63) — **localstack01** lists no buckets/users/functions |
| content/courses/rmc-c203-cloud-security-advanced.yaml | 6 (all) | Module 1 step 2 (:55–57) — **localstack01** bucket listing fails |
| content/courses/rmc-c210-cloud-native-defense.yaml | 6 (all) | Module 1 step 2 (:59–61) — **localstack01/azure-mock01** expose no services |
| content/courses/up-cld-01-cloud-iam-misconfiguration-hunting.yaml | 6 (all) | Module 1 step 2 (:52–54) — no identity service on **localstack01** |
| content/courses/up-cld-02-container-kubernetes-security.yaml | 6 (all) | Module 1 step 2 (:53–56) — **k3s-master** returns no pods |
| content/courses/up-cld-03-ci-cd-pipeline-security.yaml | 6 (all) | Module 1 step 2 (:50–52) — **fw01** rule list unavailable (step 1 is the generic jump01 login) |
| content/courses/up-cld-04-cloud-logging-detection.yaml | 6 (all) | Module 1 step 1 (:47–49) — the cloud CLI on jump01 reaches no simulated account; first dedicated-node step is **localstack01** at :50–52 |
| content/courses/up-cld-05-iac-security-policy-as-code.yaml | 6 (all) | Module 1 step 1 (:52) needs cloud+Terraform CLIs; first dedicated-node step is **terraform01** at :53–54 |
| content/courses/up-cld-06-cloud-incident-response.yaml | 6 (all) | Module 1 step 2 (:55–57) — **cloud-siem01** dashboards show no incident records |

The only scenario bound to this template is `content/scenarios/cloud-breach/scenario.yaml`
(`range_template: cloud-security`, scenario.yaml:3), used by up-cld-04 module 6
(`scenario: cloud-breach`, up-cld-04:485). It drives an attacker timeline against the
`company-backups` bucket (scenario.yaml:44–48), so a build missing that bucket's seeded
content also breaks that module.

## 7. Template inconsistency observed (report to template owner)

`resource_totals` (template.yaml:273–277) does not match the node specs above it:
it claims 20 VMs / 62 cores / 160 GB / 1980 GB, while the 16 declared nodes sum to
**16 VMs / 52 cores / 126 GB / 1740 GB** (recomputed with a YAML read of the node specs;
`vm_count: 16` at template.yaml:11 agrees with the nodes). The catalogue's network spec
also lists cloud-security at **20 VMs** (content/catalogue/range_network_spec.md:21), so
the totals block appears to have been carried from that earlier spec while the node list
shrank to 16. **INFERRED:** capacity planning on site should use the per-node specs, not
the `resource_totals` block or the catalogue row. This is a template-file defect, not
something this checklist can fix.

## 8. Open items (UNKNOWN, need a live range or template owner)

1. Every service port and URL (template declares names only).
2. All credentials/keys (instructor seeding, not in source).
3. Whether fw01 default-denies unlisted VLAN pairs (the estate spec says deny-egress by default, range_network_spec.md:1, but the template declares no default policy).
4. Pre-populated audit-log windows that up-cld-01 module 5 and up-cld-04 assume.
5. The relationship between jump01's `guacamole` and `ssh` services.
6. Exact CLI invocations for prowler/trivy/kubectl workflows (tool names are cited; syntax is not).
