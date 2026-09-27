# TrueNorth Range — VM Build Sheet & Software List

Working list for the people building the VM library. It is derived from what the repo
defines today, not from a new design:

| Source | What it gives |
|---|---|
| `truenorth-content-pack/truenorth-content/vm_catalogue.csv` | Golden image library (build once, clone many) |
| `truenorth-content-pack/truenorth-content/templates/packer/*.pkr.hcl` | Build-time vCPU / RAM / disk per template |
| `content/ranges/*/template.yaml` | Deploy-time roles, specs and services per range |
| `truenorth-ai-vsphere-pack/truenorth-vsphere-architecture.md` | Management-plane VMs |
| `infra/ansible/` | What post-deploy configuration already exists |

Items marked **(proposed)** are not in the repo yet. They are recommendations and need
sign-off.

---

## 0. How the build is layered

There are three tiers, and it matters which tier each piece of software goes into.

| Stage | Code | What goes here | How |
|---|---|---|---|
| Golden template | **T** | Same on every clone, not tied to an identity: OS, patches, VMware Tools, runtimes, common apps, the sensor binary | Packer, then sysprep (Windows) or cloud-init clean (Linux) |
| Role snapshot | **R** | Heavy server products that cannot be sysprepped after install and that need AD: Exchange, SharePoint, SQL with domain service accounts, AD CS, MECM, WSUS | Build once inside a *golden domain*, snapshot, then instant-clone the **whole set** into each isolated range VLAN |
| Post-deploy | **P** | Anything specific to one range: agent enrolment keys, users and mail seeding, scenario vulnerabilities and artifacts, domain join for loose clones | Ansible at range spin-up (`infra/ansible/playbooks/setup-range.yml`) |

Ranges are **no-egress** (see `range.tf`: "No egress from the range segment"). As a result,
anything in stage **P** has to come from an internal software depot, not from the internet
(see §6).

---

## 1. Management plane (persistent, build first)

| VM | OS | vCPU | RAM | Disk | Purpose | Source |
|---|---|---:|---:|---|---|---|
| TN-MGMT01 | Ubuntu Server 24.04 LTS | 12–16 | 64 GB min, 96–128 GB preferred | 100 GB OS + 500 GB data (min) | TrueNorth platform: API, frontend, Postgres, Redis, Celery, Keycloak, MinIO, OpenSearch, AI orchestrator, Terraform | vSphere architecture §2 |
| TN-DC01 | Windows Server 2022 (or 2025) | 4 | 8–16 GB | 100 GB | AD DS + DNS for platform identity (Keycloak LDAP federation). **Not** a range DC | vSphere architecture §2 |
| TN-RANGE01 | any small template | 2 | 4 GB | 40 GB | Throwaway VM that proves TrueNorth can create, start, observe and destroy a VM | vSphere architecture §2 |
| TN-DEPOT01 **(proposed)** | Ubuntu 24.04 | 4 | 16 GB | 100 GB + 1–2 TB | Offline software depot: Nexus/Sonatype OSS (or Artifactory OSS) hosting an internal Chocolatey feed, apt proxy/mirror, PyPI and Docker registry mirror, plus an ISO/installer share. Ranges reach this host and nothing else | Needed for the no-egress rule |
| TN-BUILD01 **(proposed)** | Ubuntu 24.04 | 8 | 16 GB | 200 GB | Runs Packer and Ansible for the template builds. This is the **only** host with controlled internet egress. Can be folded into TN-MGMT01 if capacity is tight | `.github/workflows/packer-build.yml` |
| TN-WSUS01 **(proposed, optional)** | Windows Server 2022 | 2 | 8 GB | 300 GB | Patch source for templates and role snapshots | — |

Licensing decision needed: Windows and Office activation inside isolated range VLANs. The
options are (a) a KMS host cloned into each range, (b) MAK keys baked into the templates,
or (c) evaluation media (180-day). Platform AD-based activation cannot reach range domains.

---

## 2. Golden templates (build once, clone many)

The build specs come from the Packer files. The Packer files size the **template**; each
range resizes the clone at deploy time (see §3). Build hours and golden size come from
`vm_catalogue.csv`.

| # | Template | OS / media | Build vCPU / RAM / disk | Build hrs | Golden GB | Sensor baked | Role | Notes |
|---:|---|---|---|---:|---:|:-:|---|---|
| 1 | `win10-22h2` | Windows 10 22H2 Ent | 2 / 4 GB / 35 GB | 14 | 35 | yes | Victim/analyst workstation | Workhorse image. Build first |
| 2 | `win11-24h2` | Windows 11 24H2 Ent | 2 / 4 GB / 40 GB | 14 | 40 | yes | Current-estate victim | UEFI + Secure Boot + **vTPM** (needs a vCenter key provider). **Raise the disk to 64 GB**: 40 GB is below the Win11 minimum |
| 3 | `win7-sp1` | Windows 7 SP1 x64 | 2 / 4 GB / 25 GB | 10 | 25 | yes | Legacy vulnerable host | Confirm licensing and EO justification. Isolated ranges only |
| 4 | `srv2016` | Windows Server 2016 Std | 2 / 4 GB / 30 GB | 12 | 30 | yes | Legacy DC/file | |
| 5 | `srv2019` | Windows Server 2019 Std/DC | 2 / 4 GB / 35 GB | 16 | 35 | yes | DC/DNS/DHCP/Exchange/IIS/MSSQL base | Base for most role snapshots. Also hosts SharePoint 2019 (see §7) |
| 6 | `srv2022` | Windows Server 2022 Std/DC | 2 / 4 GB / 35 GB | 14 | 35 | yes | Modern DC/member | Every range template uses this. **Build second** |
| 7 | `precomp-host` | Win10 / Srv2019 derived | per parent | 12 | 35 | yes | Pre-compromised start state | Clone of 1 or 5 with staged artifacts |
| 8 | `detonation-host` | Win10 derived | per parent | 12 | 35 | **no** | Sterile malware detonation | No sensor, no egress ever, snapshot-revert after each use |
| 9 | `ubuntu-lts` | Ubuntu Server 24.04* | 2 / 4 GB / 15 GB | 8 | 15 | yes | Web/app/DB victim, and base for most Linux roles | *Range YAML says `ubuntu-2204`. Pick one (see §7) |
| 10 | `rocky` | Rocky Linux 9 | 2 / 4 GB / 15 GB | 8 | 15 | yes | Enterprise Linux victim | |
| 11 | `kali` | Kali 2024.x installer | 2 / 4 GB / 30 GB | 10 | 30 | no | Attacker / analyst workstation | Ranges deploy it at 4 vCPU / 8 GB / 120 GB |
| 12 | `remnux` | Ubuntu + REMnux installer | per ubuntu-lts | 10 | 25 | no | Malware analysis | Packer marked `todo(vsphere)` |
| 13 | `sift` | Ubuntu + SIFT (cast) | per ubuntu-lts | 10 | 30 | no | DFIR/forensics workstation | Packer marked `todo(vsphere)` |
| 14 | `securityonion` | Security Onion 2.4 ISO | 2 / 4 GB / 60 GB | 16 | 60 | n/a | Sensor + telemetry | **Undersized.** SO 2.4 minimums are about 4+ vCPU, 16–24 GB RAM and 200 GB+ disk. Build at 4 / 16 GB / 200 GB and check against current SO docs |
| 15 | `pfsense` | pfSense CE 2.7 | 2 / 4 GB / 4 GB | 6 | 4 | n/a | Range gateway/firewall | Ranges deploy at 2 / 2 GB / 8–20 GB. Build the disk at 20 GB |
| 16 | `svc-emulators` | ubuntu-lts derived | per parent | 12 | 20 | yes | Internet-service emulation (DNS/NTP/mail/web) | |
| 17 | `ca-host` | ubuntu-lts derived | per parent | 8 | 10 | yes | Certificate authority (step-ca / OpenSSL) | Linux CA. AD CS is a Windows role snapshot |
| 18 | `usersim` | ubuntu-lts derived | per parent | 20 | 15 | no | Noise floor / benign traffic | Suggest CMU SEI **GHOSTS** (server here, clients on Windows) |
| 19 | `blueteam-soc` | mixed | per parent | 20 | 80 | yes | Blue-team SOC stack for live Red-v-Blue | |
| 20 | `cloudlog-emu` | ubuntu-lts derived | per parent | 10 | 20 | n/a | Azure/AWS log emulation | |

**Referenced by range YAML but missing from the catalogue or Packer (gaps to fill):**

| Template | Used by | Suggested build |
|---|---|---|
| `win10-ltsc` (Windows 10 IoT/Ent LTSC 2021) | large-enterprise HMI + engineering workstations | 2 / 4 GB / 60 GB |
| `vyos-1.4` | red-vs-blue `rtr01` | 1 / 2 GB / 4 GB |
| `c2-server` (catalogue `enabled=no`) | red-team, soc-training, red-vs-blue | ubuntu-lts + Sliver/Mythic, 4 / 8 GB / 60 GB. The catalogue says instructor sign-off is required |
| `win-xp-sp3` (`enabled=no`) | none | Leave disabled unless an EO requires it |

Build order: `srv2022` → `win10-22h2` → `ubuntu-lts` → `pfsense` → `securityonion` →
`kali` → `win11-24h2` → `srv2019` → the rest.

---

## 3. Role builds (what gets deployed per range)

Deploy spec = the largest spec any range template asks for. Stage: **T**/**R**/**P** as in §0.

### Windows servers

| Role | Template | vCPU / RAM / Disk | Ranges | Stage | Key installs |
|---|---|---|---|:-:|---|
| Domain controller | srv2022 (one on srv2019 in red-team) | 4 / 8 GB / 100 GB | all | R | AD DS, DNS, DHCP, GPMC, RSAT |
| File server | srv2022 | 2 / 8 GB / 500 GB | all | R | File Server, DFS-N/R, VSS |
| Exchange | srv2022 | 4 / 16 GB / 200 GB | large-ent, red-vs-blue | R | Exchange 2019 CU15 (or SE), OWA, SMTP/IMAP |
| SharePoint | **srv2019** (see §7) | 4 / 16 GB / 200 GB | large-ent | R | SharePoint 2019 (or SE on srv2022), IIS |
| SQL | srv2022 | 8 / 32 GB / 500 GB | large-ent, red-team | R | SQL Server 2022, SSRS, SSMS |
| PKI | srv2022 | 2 / 4 GB / 60 GB | large-ent, red-team, red-vs-blue | R | AD CS Enterprise CA, OCSP, Web Enrollment |
| WSUS | srv2022 | 2 / 8 GB / 300 GB | large-ent | R | WSUS role |
| MECM/SCCM | srv2022 | 4 / 16 GB / 200 GB | large-ent | R | MECM current branch, ADK + WinPE, SQL (local) |
| Print | srv2022 | 2 / 4 GB / 60 GB | large-ent | R | Print Server role |
| ERP/app | srv2022 | 4 / 16 GB / 200 GB | large-ent | R | IIS, .NET runtimes, sample app |
| Hybrid identity sim | srv2022 | 2 / 4 GB / 60 GB | large-ent | R | Entra Connect (simulated, no tenant) |
| Windows jump | srv2022 | 2 / 4 GB / 60 GB | large-ent, red-team | T+P | RDS/RDP, OpenSSH, RSAT |
| Historian | srv2019 | 4 / 8 GB / 500 GB | large-ent | R | Historian simulator (not OSIsoft PI, which is licensed) |

### Windows clients

| Role | Template | vCPU / RAM / Disk | Ranges | Stage |
|---|---|---|---|:-:|
| User workstation | win11-24h2 | 2 / 4 GB / 60 GB (power users 4 / 16 GB / 120 GB) | all | T + P (domain join) |
| HMI / engineering WS | win10-ltsc | 2 / 4–8 GB / 60–120 GB | large-ent | T + P |
| Analyst seat (Windows) | win10-22h2 | 4 / 16 GB / 120 GB | PO scenarios | T |

### Linux services (victim / infrastructure)

| Role | Template | vCPU / RAM / Disk | Stage | Key installs |
|---|---|---|:-:|---|
| Web | ubuntu-lts | 2 / 4 GB / 40 GB | T+P | nginx or Apache, PHP-FPM, Tomcat, TLS |
| Mail | ubuntu-lts | 2 / 4 GB / 60 GB | T+P | Postfix, Dovecot, Roundcube, SpamAssassin |
| DNS | ubuntu-lts | 1 / 2 GB / 20 GB | T | BIND9 |
| DB | ubuntu-lts | 2 / 8 GB / 100 GB | T | MySQL 8, PostgreSQL |
| Cache | ubuntu-lts | 2 / 4 GB / 20 GB | T | Redis |
| GitLab / app | ubuntu-lts | 4 / 8 GB / 200 GB | T | GitLab CE, Docker |
| Reverse proxy | ubuntu-lts | 2 / 2 GB / 16 GB | T | HAProxy, ModSecurity |
| Backup | ubuntu-lts | 2 / 2 GB / 32 GB | T | rsync, (Veeam agent — licensed) |
| Log relay / syslog | ubuntu-lts | 2 / 4 GB / 500 GB | T | rsyslog, Logstash, Filebeat |
| NTP | ubuntu-lts | 1 / 1 GB / 10 GB | T | chrony |
| Linux jump | ubuntu-lts | 2 / 4 GB / 40 GB | T | Apache Guacamole, SSH, proxychains |
| Firewall / VPN | pfsense | 2–4 / 2–4 GB / 20 GB | T+P | OpenVPN, IPsec, WireGuard, Snort/Suricata pkg |
| Router | vyos | 1 / 2 GB / 4 GB | T+P | OSPF/BGP |

### Blue / SOC

| Role | Template | vCPU / RAM / Disk | Key installs |
|---|---|---|---|
| SIEM | ubuntu-lts | 8 / 32 GB / 500–1000 GB | OpenSearch + Dashboards, Logstash, Sigma rules |
| Security Onion | securityonion | 8 / 16 GB / 500 GB | Zeek, Suricata, Strelka, Elastic |
| IDS / NIDS | ubuntu-lts | 4 / 8 GB / 200 GB | Suricata, Zeek, EveBox, Arkime |
| PCAP | ubuntu-lts | 2 / 4 GB / 1000 GB | Stenographer, Arkime |
| EDR server | ubuntu-lts | 4 / 8 GB / 200 GB | Velociraptor server |
| SOAR | ubuntu-lts | 4 / 8 GB / 100 GB | TheHive 5, Cortex |
| Threat intel | ubuntu-lts | 2 / 4 GB / 60 GB | MISP |
| Vuln scanner | ubuntu-lts | 4 / 8 GB / 100 GB | Greenbone Community (OpenVAS) |
| DFIR workstation | sift | 4 / 16 GB / 200 GB | Autopsy, Volatility 3, Plaso, YARA |
| Blue analyst (Linux) | ubuntu-lts | 2 / 4 GB / 32 GB | Wireshark, Zeek, Velociraptor client, xRDP |
| Honeypot | ubuntu-lts | 1 / 2 GB / 16 GB | Cowrie, Dionaea, Elasticpot (T-Pot) |
| Traffic gen | usersim | 2 / 2 GB / 8 GB | tcpreplay, Scapy, GHOSTS |
| Scoreboard | ubuntu-lts | 2 / 4 GB / 32 GB | TrueNorth scoring engine |

### Red

| Role | Template | vCPU / RAM / Disk | Key installs |
|---|---|---|---|
| Attack platform / operator | kali | 4 / 8 GB / 120 GB | see §5.3 |
| C2 teamserver | c2-server | 4 / 8 GB / 60–100 GB | Sliver, Mythic, Empire, Metasploit |
| Redirector | ubuntu-lts | 1 / 1 GB / 20 GB | nginx, socat, iptables |
| Payload / staging | ubuntu-lts | 2 / 2 GB / 40–100 GB | nginx, SFTP, DNS-exfil listener |
| Phishing | ubuntu-lts | 2 / 4 GB / 40 GB | GoPhish, Postfix, nginx |

### OT (large-enterprise)

| Role | Template | vCPU / RAM / Disk | Key installs |
|---|---|---|---|
| PLC | ubuntu-lts | 1 / 1 GB / 10 GB | OpenPLC runtime, Modbus TCP |
| OT firewall | pfsense | 2 / 2 GB / 20 GB | — |

### Cloud-security range

All of these are ubuntu-lts, and most run as containers. The Docker images must be
pre-loaded into the depot registry.

| Role | vCPU / RAM / Disk | Key installs |
|---|---|---|
| AWS sim | 4 / 16 GB / 100 GB | LocalStack |
| Azure sim | 4 / 8 GB / 60 GB | Azurite (+ mocks) |
| k3s master + 2 workers | 4 / 8 GB / 100 GB each | k3s, helm, kubectl, containerd |
| Registry | 2 / 4 GB / 200 GB | Harbor, Trivy |
| GitLab / Jenkins / ArgoCD / SonarQube | 2–4 / 4–8 GB / 40–200 GB | as named |
| Falco / Prowler | 2 / 4 GB / 40 GB | Falco, falcosidekick, Prowler, ScoutSuite, Steampipe |
| IaC | 2 / 4 GB / 40 GB | Terraform, Consul |

---

## 4. Capacity per range (one instance)

Summed from `content/ranges/*/template.yaml`:

| Range | VMs | vCPU | RAM | Disk (thin, provisioned) |
|---|---:|---:|---:|---:|
| small-enterprise | 5 (jump, DC, 3 users) | — | — | — (YAML has no specs yet) |
| medium-enterprise | 8 | 20 | 44 GB | 1.0 TB |
| cloud-security | 16 | 52 | 126 GB | 1.7 TB |
| soc-training | 23 | 69 | 157 GB | 3.9 TB |
| red-team | 28 | 67 | 149 GB | 2.1 TB |
| red-vs-blue | 40 | 80 | 160 GB | 1.1 TB |
| large-enterprise | 50 | 142 | 375 GB | 7.4 TB |

Instant clones share the parent's disk and memory pages, so real consumption is well below
these numbers. Treat them as the ceiling per concurrent range when sizing the R6625 hosts
(1 TB RAM each in the hypervisor variant).

---

## 5. Software list

Stage: **T** template, **R** role snapshot, **P** post-deploy.
Source: `choco:<id>` (Chocolatey), `apt:<pkg>`, `vendor` (installer from the vendor, kept in
the depot), `gh` (GitHub release), `docker` (image mirrored to the depot).

### 5.1 Windows — baseline on every Windows template

| App | Stage | Source | Notes |
|---|:-:|---|---|
| VMware Tools | T | vendor | Required for guest customization and instant clone |
| Latest CU + .NET Framework 4.8.1 | T | WSUS / vendor | |
| VC++ 2015–2022 redist (x64 + x86) | T | `choco:vcredist140` | Exchange also needs VC++ 2012 and 2013 |
| PowerShell 7 | T | `choco:powershell-core` | |
| OpenSSH Server + WinRM (HTTPS) | T | Windows capability | Ansible transport |
| Sysmon + config | T | `choco:sysmon`, config in depot | Ansible currently downloads it from the internet; see §7 |
| Winlogbeat / Elastic Agent | T (binary), P (enrol) | `choco:winlogbeat` | Match the SIEM/Security Onion version |
| Velociraptor client | P | depot | Needs per-range server cert/config |
| 7-Zip | T | `choco:7zip` | |
| Notepad++ | T | `choco:notepadplusplus` | |
| Google Chrome, Firefox ESR | T | `choco:googlechrome`, `choco:firefoxesr` | Edge is built in |
| Adobe Acrobat Reader | T | `choco:adobereader` | Phishing/PDF scenarios need a real reader |
| PowerShell logging, audit policy | T/P | Ansible (exists) | Script block, module, transcription |

### 5.2 Windows — by role

**User workstations (win10/win11)**

| App | Stage | Source | Notes |
|---|:-:|---|---|
| Microsoft Office LTSC 2024 Pro Plus (Word, Excel, PowerPoint, Outlook) | T | Office Deployment Tool + `config.xml`, volume licence | **Not** Microsoft 365 Apps: M365 needs internet sign-in and will not activate in a no-egress range. Macro-phishing and Outlook scenarios need Office |
| Outlook profile → range Exchange | P | Ansible / GPO autodiscover | |
| Microsoft Teams | — | — | Skip. It needs cloud |
| VLC | T | `choco:vlc` | Realism |
| Java JRE (only if a scenario needs it) | P | `choco:temurin17jre` | |
| GHOSTS client | P | depot | Drives Office, browser and mail for the noise floor |
| Scenario-vulnerable software | P | depot | Per scenario, never in the template |

**Analyst workstation (Windows analyst seat, DFIR on Windows)**

| App | Stage | Source |
|---|:-:|---|
| Wireshark | T | `choco:wireshark` |
| Npcap | T | vendor. **Free Npcap cannot install silently**; an unattended install needs Npcap OEM |
| Sysinternals Suite | T | `choco:sysinternals` |
| NetworkMiner | T | `choco:networkminer` |
| Eric Zimmerman tools (+ Timeline Explorer) | T | `Get-ZimmermanTools.ps1` → depot |
| FTK Imager | T | vendor (registration required) |
| Autopsy | T | `choco:autopsy` |
| KAPE | T | vendor. Commercial use needs a licence |
| Hayabusa, Chainsaw, DeepBlueCLI | T | gh |
| CyberChef (offline HTML) | T | gh |
| Volatility 3, YARA, Python 3 | T | `choco:python`, pip from depot |
| Ghidra, x64dbg, dnSpyEx, PEStudio | T | `choco:ghidra`, `choco:x64dbg.portable`, gh, vendor |
| VS Code, Git | T | `choco:vscode`, `choco:git` |
| PuTTY, WinSCP | T | `choco:putty`, `choco:winscp` |
| RSAT | T | Windows capability |

**Servers (role snapshots)**

| Product | Base | Prerequisites (put them all in the depot) | Licence |
|---|---|---|---|
| AD DS / DNS / DHCP / GPMC | srv2022 | built in | Windows Server |
| AD CS (Enterprise CA, OCSP, Web Enrollment) | srv2022 | built in | Windows Server |
| Exchange Server 2019 CU15 (or Exchange SE) | srv2022 | .NET 4.8.1, VC++ 2012 + 2013, UCMA 4.0, IIS URL Rewrite 2.1, AD schema prep | Exchange key (or unlicensed trial mode) |
| SharePoint Server 2019 | **srv2019** | SharePoint prerequisite installer (offline files), SQL instance | SharePoint key |
| SharePoint Server Subscription Edition (alternative) | srv2022 | as above | SE licence |
| SQL Server 2022 + SSRS + SSMS | srv2022 | .NET 4.8 | Developer (non-production) or Standard. Confirm which applies to training |
| WSUS | srv2022 | built in, WID or SQL | Windows Server |
| MECM current branch | srv2022 | Windows ADK + WinPE add-on, SQL, IIS, BITS, RDC | MECM licence / eval |
| IIS + .NET 8 hosting bundle (ERP) | srv2022 | `choco:dotnet-8.0-windowshosting` | — |
| Print Server | srv2022 | built in | — |

### 5.3 Linux

**Baseline on every Linux template (ubuntu-lts, rocky)**

| Package | Stage | Source |
|---|:-:|---|
| open-vm-tools, cloud-init | T | apt / dnf. Required for vSphere customization |
| auditd, rsyslog, chrony | T | apt / dnf |
| Filebeat / Elastic Agent | T (binary), P (enrol) | vendor repo mirrored |
| Sysmon for Linux (optional) | T | Microsoft repo mirrored |
| Velociraptor client | P | depot |
| curl, jq, python3, vim, tcpdump, net-tools, unzip | T | apt |
| Docker CE (for container roles) | T | Docker repo mirrored |

**Victim services** (in the role, stage T unless noted):
nginx, apache2, php-fpm, tomcat10 + openjdk-17, mysql-server 8, postgresql, redis-server,
postfix, dovecot-imapd, roundcube, spamassassin, bind9, samba, haproxy +
libapache2-mod-security2, WordPress, and GitLab CE. The intentionally vulnerable apps
(DVWA, OWASP Juice Shop, `vulnerable-web`) are stage **P**, from depot Docker images.

**Blue / SOC**

| Tool | Source |
|---|---|
| Security Onion 2.4 | ISO (its own appliance) |
| OpenSearch + Dashboards, Logstash | vendor apt repo mirrored |
| Zeek | OBS repo mirrored |
| Suricata | OISF PPA mirrored |
| Arkime, EveBox | vendor .deb |
| Stenographer | build from source → .deb in depot |
| Velociraptor server | gh |
| TheHive 5 + Cortex | StrangeBee packages (the community licence has limits; check user count) |
| MISP | official install script, pre-staged |
| Greenbone Community | Docker Compose, images mirrored. Feed sync needs egress, so sync on TN-BUILD01 |
| Wireshark, tshark, tcpdump, ngrep | apt |
| Volatility 3, Plaso, YARA, sigma-cli, Chainsaw | pip / gh |
| Autopsy, The Sleuth Kit | SIFT / apt |
| SIFT | `cast` installer (template 13) |
| REMnux | `remnux` installer (template 12) |
| Cowrie, Dionaea, Elasticpot | T-Pot or Docker images |
| xRDP + XFCE (for blue analyst desktops) | apt |

**Red (Kali)**

| Tool | Source | Notes |
|---|---|---|
| kali-linux-default metapackage | Kali repo mirrored | Metasploit, Nmap, Burp CE, Responder, Hydra, John, Hashcat, SQLmap… |
| BloodHound CE + SharpHound | Docker images | |
| Impacket, NetExec | apt / pipx | NetExec replaces the deprecated CrackMapExec that the YAML names |
| Certipy, Evil-WinRM, Kerbrute | apt / gh | AD CS and AD attacks |
| Sliver, Mythic, Empire (+ Starkiller) | gh / Docker | Open-source C2 |
| Cobalt Strike | vendor | **Licensed and export-controlled.** Use Sliver/Mythic unless a licence is held. The YAML already calls it `cobalt_strike_sim` in places |
| Burp Suite Pro | vendor | Licence; CE is in Kali |
| GoPhish | gh | |
| Chisel, Ligolo-ng, proxychains4, socat | gh / apt | Pivoting |
| SecLists, wordlists | depot tarball | Ansible currently pulls SecLists from GitHub (§7) |

**OT**: OpenPLC runtime (PLCs), FUXA or ScadaBR (HMI, on win10-ltsc or Linux), pymodbus
and ModbusPal (field-device simulation).

**Cloud**: LocalStack, Azurite, k3s, helm, kubectl, Harbor, Trivy, GitLab CE +
gitlab-runner, Jenkins, Argo CD, SonarQube, Falco + falcosidekick, Prowler, ScoutSuite,
Steampipe, Terraform, Consul, AWS CLI v2, Azure CLI, Apache Guacamole. These are mostly
Docker/Helm, so every image and chart must be in the depot registry.

---

## 6. Ninite vs Chocolatey vs winget

Ninite works as a convenience for the template build only. It should not be the
deployment mechanism:

| | Ninite (free) | Ninite Pro | Chocolatey + internal feed | winget |
|---|---|---|---|---|
| Works in a no-egress range | No. Downloads live | Yes, with its offline cache | **Yes**, from the TN-DEPOT01 feed | Partly (needs a private REST source) |
| Unattended / scripted | Semi | Yes | Yes (Ansible `win_chocolatey` **already used**) | Yes |
| Catalogue covers our list | Only common apps: browsers, 7-Zip, Notepad++, VLC, PuTTY, WinSCP, VS Code, Python, runtimes | same | Almost everything in §5.2 | Most |
| Version pinning | No (always latest) | Limited | Yes | Yes |
| Licence | Home use only. Business use needs Pro | Paid per machine | Free (open source). Business edition optional | Free |

**Recommendation:** standardize on **Chocolatey with an internal feed on TN-DEPOT01**, with
pinned versions. The repo already uses it (`infra/ansible/inventory/group_vars/workstations.yml`).
Use Packer for stage **T** on TN-BUILD01, where egress is allowed, and Ansible for stage
**P**, pointed only at the depot. Office, Exchange, SharePoint, SQL and MECM are outside
Ninite and Chocolatey. Keep their vendor media plus offline prerequisites on the depot share.

Linux equivalent: an apt/dnf mirror or proxy (aptly or Nexus apt proxy) on TN-DEPOT01, with
`sources.list` baked into the template so it points there.

---

## 7. Problems found while building this list

These are repo issues the build team will hit. They are listed here and not fixed.

1. **Post-deploy installs assume internet.** `infra/ansible/roles/workstation/tasks/main.yml`
   and `roles/domain_controller/tasks/main.yml` download Sysmon from
   `download.sysinternals.com`; `roles/attacker/tasks/main.yml` pulls SecLists from GitHub;
   and Chocolatey defaults to the community feed. All of these fail in a no-egress range.
   Repoint them at the depot.
2. **Invalid CIDRs** in `content/ranges/medium-enterprise/template.yaml`: `10.10.300.0/24` and
   `10.10.400.0/24` (an octet cannot exceed 255). The same scheme appears in the IPs
   `10.10.300.x` and `10.10.400.x`.
3. **Ubuntu version mismatch.** The catalogue says `ubuntu-lts` = 24.04; every range YAML and
   `infra/proxmox|hyperv/packer` say `ubuntu-2204`.
4. **Templates referenced but not catalogued:** `win10-ltsc`, `vyos-1.4`; `c2-server` is used
   by three ranges but is `enabled=no`.
5. **Security Onion template is undersized** (4 GB RAM / 60 GB). It will fail the SO 2.4
   installer's checks.
6. **Windows 11 template** is 40 GB, below Microsoft's 64 GB minimum, and needs a vTPM (vCenter
   key provider) or a documented TPM-check bypass for lab use.
7. **red-vs-blue `exch01` disk is 48 GB.** This is too small for Exchange (install + logs +
   a mailbox database). Use ≥150 GB.
8. **Exchange 2019 and SharePoint 2019 reached end of support on 14 Oct 2025.** That is fine
   for a deliberately vulnerable range, but it should be an explicit choice. The current
   versions are Exchange SE and SharePoint SE. Also, **SharePoint 2019 is not supported on
   Windows Server 2022** (large-enterprise `sp01` is `windows-server-2022`). Use srv2019, or
   SharePoint SE.
9. **CrackMapExec** (red-team YAML) is unmaintained. Its successor is NetExec.
10. `small-enterprise/template.yaml` uses a different schema (`assets`/`count`) with no specs,
    so capacity cannot be computed for it.

## 8. Decisions needed before the build starts

- Windows/Office activation inside ranges: KMS clone, MAK, or evaluation media.
- Ubuntu 22.04 or 24.04 as the single `ubuntu-lts`.
- Exchange/SharePoint: 2019 (vulnerable, EOL) or SE (current), or one of each.
- Licences to procure: Office LTSC VL, Exchange, SharePoint, SQL (if not Developer), MECM,
  Npcap OEM, Burp Pro, KAPE, and Cobalt Strike (or drop it).
- Whether to stand up TN-DEPOT01 and TN-BUILD01 as separate VMs or fold them into TN-MGMT01.
