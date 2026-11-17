# Acceptance checklist — range template `greyspace-internet`

Card TN10X-F7 (template acceptance), skill `tn-curriculum-traceability`.
Subject: `content/ranges/greyspace-internet/template.yaml` ("Small Office with
Greyspace Internet", version 1.0 — template.yaml:1-3). Derived from the working tree at
base commit `d5851f6d649a55b54f33e700be9d28be399e1302` by reading the template, the five
courses that build on it, ADR 0007 / 0012 / 0013 and the Greyspace runtime sources.

Labels: **OBSERVED** = read directly in a cited file; **INFERRED** = derived from cited
sources, derivation shown; **UNKNOWN** = not derivable from the sources read, must be
confirmed on a built range before scheduling a course against it. No check below has been
run against a live range; every check status is `blocked` until someone runs it.

## 1. Nodes

All from template.yaml:27-61 — **OBSERVED**.

| id | name | role | os | vlan | ip | services | specs |
|---|---|---|---|---|---|---|---|
| edge01 | Edge Router | router | vyos | servers | 10.40.120.1 | nat, dns_forwarder | 2c / 2048 MB / 10 GB |
| dc01 | Domain Controller | domain_controller | windows-server-2022 | servers | 10.40.120.10 | active_directory, dns | 4c / 8192 MB / 100 GB |
| ws01 | Workstation 1 | workstation | windows-11 | corporate | 10.40.110.10 | — | 2c / 4096 MB / 60 GB |
| ws02 | Workstation 2 | workstation | windows-11 | corporate | 10.40.110.11 | — | 2c / 4096 MB / 60 GB |

edge01's interfaces span all three VLANs (servers, corporate, greyspace) —
template.yaml:34 — **OBSERVED**; it is the only node that touches the greyspace VLAN.

Not in the node list but added at build time: a **gs-core VM** joins the VLAN the
`greyspace.network` key names (here `greyspace`) when the backend is `vsphere_api`;
`plan_host` adds it to the rendered template before VLANs are reserved (ADR 0007
decision 6) — **OBSERVED** (ADR + template.yaml:70). The template's own comment puts it
at the last address of 100.64.190.0/24, i.e. 100.64.190.254 (template.yaml:22) —
**OBSERVED** (comment) / **INFERRED** (address arithmetic). edge01, the router holding
that network's gateway, gets the `greyspace_route` (ADR 0007 decision 6) — **OBSERVED**.

**UNKNOWN — accounts:** the template declares no `accounts:` section at all. ADR 0012
decision 12 says a range template declares its accounts, and ADR 0013 removes the
generic-password fallback in favour of unique sealed per-range credentials. What
credentials dc01, ws01, ws02 and edge01 actually receive when a range built from this
template is deployed is therefore not derivable from these sources.

## 2. Networks and addressing

Range side — **OBSERVED** (template.yaml:14-25):

| VLAN | name | cidr |
|---|---|---|
| 110 | corporate | 10.40.110.0/24 |
| 120 | servers | 10.40.120.0/24 |
| 190 | greyspace | 100.64.190.0/24 (edge router .1, gs-core .254 — template.yaml:22) |

Greyspace side — the block is `corpus_tier t0`, `site_packs [news, search, social,
webmail, dev]`, `npc_profile office-day`, `threat_infra true`, `trust_ca true`,
`network greyspace` (template.yaml:63-70) — **OBSERVED**.

- ISP prefixes: Northline Transit (ASN 64501) 198.18.0.0/16 and Bluewater Carrier
  (ASN 64502) 198.19.0.0/16 (`control-plane/api/app/greyspace/fixture.py:21-24`) —
  **OBSERVED** for the T0 corpus the template pins.
- Infrastructure segment: the first ISP's first /24, i.e. 198.18.0.0/24 (`check-t0.sh`
  header, greyspace/scripts/check-t0.sh:28-29; ADR 0007 decision 4). Service offsets:
  root DNS .10, TLD .11, authoritative .12, mail .25, resolver .53, threat .66, web farm
  .80, NTP .123 (config.py:65-72; ADR 0007 decision 4 lists .10/.11/.12/.53/.66/.80) —
  **OBSERVED** offsets; the concrete addresses below are **INFERRED** (offsets +
  segment): resolver 198.18.0.53, mail 198.18.0.25, threat 198.18.0.66, web farm
  198.18.0.80, NTP 198.18.0.123. One Greyspace stack per Docker host (fixed supernet —
  check-t0.sh:28-29) — **OBSERVED**.
- Site addresses: even-indexed sites on 198.18.10.x, odd ones on 198.19.20.x
  (fixture.py:65-67) — **OBSERVED** rule, per-site addresses **INFERRED** from it
  (maplewire-news.com 198.18.10.11, harbourdaily.ca 198.19.20.12, findly-search.com
  198.19.20.14, quillmail.com 198.18.10.19, …). Threat domains are fixed:
  update-cdn-sync.net → 198.19.250.10, acme-payroll-portal.com → 198.19.250.11
  (fixture.py:55-56) — **OBSERVED**.
- Rendered site set — **INFERRED** (`site_packs` filters the corpus categories,
  config.py:163-167, validated at 177-182): the T0 sites in the five requested categories — news: maplewire-
  news.com, harbourdaily.ca, northfield-gazette.org, weatherwise.ca; search:
  findly-search.com, portalhub.net; social: chirpline.com, friendgrid.net, pastebox.io;
  webmail: quillmail.com; dev: codehub.dev, stackwise.dev, pkgmirror.net
  (fixture.py:27-48). The gov, video and shopping sites are then *not* rendered.
- Greyspace's own zone is `gs-infra.net`; NTP at `ntp.gs-infra.net`, CA published at
  `http://pki.gs-infra.net/root.crt`, MX for every site zone at `mail.gs-infra.net`,
  webmail at `webmail.<zone>` of webmail sites (ADR 0007 decision 8) — **OBSERVED**
  (ADR text; the concrete `webmail.quillmail.com` is **INFERRED**).

## 3. Accounts the courses assume

- **OBSERVED:** no username or password appears in the template or in any of the five
  course files. The template declares no accounts (see §1).
- **OBSERVED — instructor-assigned, seeded outside the template:** the steps refer to
  accounts that must exist at exercise time but that no source here creates:
  - "the standard operator account the instructor assigned" for a management service on
    edge01 (up-ot-04:326-328);
  - "the guest account the instructor assigned" signing in from ws02, plus domain
    accounts (an external/guest account with description and last sign-in date) and
    groups granting a partner share on dc01 (up-idm-06:227-234);
  - "the domain accounts whose names carry the vendor prefix" on dc01 (up-gov-04:415-420).
- **UNKNOWN:** how these accounts, and the dc01 partner share, are seeded (template,
  injector, instructor prep) — none of the files read defines them. Confirm before
  scheduling up-idm-06 and up-gov-04.
- **UNKNOWN:** which accounts are handed to Students (ADR 0012 decision 12 requires the
  template to say; this one does not).

## 4. Services the course steps name

| Service | Named by | Grounding |
|---|---|---|
| Recursive resolver behind edge01 | up-net-05:54-55, up-gov-04:56-57, up-cti-05:62-64, up-idm-06:58-60 | edge01 `dns_forwarder` (template.yaml:36); Unbound resolver in the stack (ADR 0007 d2) — **OBSERVED**. That edge01 forwards range clients to the Greyspace resolver is **INFERRED** from `greyspace_route` ("zones allowed to it, the Greyspace resolver", ADR 0007 d6). |
| DNS on dc01 | up-net-05:59-61, up-cti-05:243-245 | dc01 services `active_directory, dns` (template.yaml:45) — **OBSERVED**. |
| News / social / search / dev sites | news: up-ot-04:410-412; social + search: up-cti-05:57-61; dev: up-gov-04:233-238 | All four categories are in `site_packs` (template.yaml:66) and exist in the T0 fixture (fixture.py:27-48) — **OBSERVED**. But the T0 "search" sites are static pages (fixture.py:82-106) and the plan says "Search not done" (docs/greyspace-plan.md slice 3) — **OBSERVED** gap. |
| Webmail with a sign-in host | up-idm-06:52-53, up-ot-04:410-412 | webmail category + `webmail.<zone>` (ADR 0007 d8) — **OBSERVED**; what a sign-in redirect looks like is **UNKNOWN** (no identity provider exists in the stack sources read). |
| Mail auth results (SPF / DKIM / DMARC) in webmail | up-net-05:237-254, 332-348 | **UNKNOWN** — no SPF/DKIM/DMARC/DNSSEC implementation found anywhere under the Greyspace runtime sources read; Mailpit is the mail service (ADR 0007 d8). Must be proven on a built range. |
| Encrypted resolver queries (DoT, tcp/853) | up-net-05:132-134 (topics name 53/853 at 127-128) | **UNKNOWN** — no DoT listener appears in the rendered stack sources or check-t0.sh. |
| Time service | up-net-05:426-443 | chrony at `ntp.gs-infra.net` (ADR 0007 d8; check-t0.sh:165-167) — **OBSERVED**; dc01 as the office internal time source is course content — **OBSERVED** as an assumption (up-net-05:426-443), dc01 time service itself not in the template — **UNKNOWN**. |
| HTTPS from the Greyspace CA | up-idm-06:57, 395-404 | `trust_ca: true` (template.yaml:69); one cert per zone via SNI, root at `http://pki.gs-infra.net/root.crt` (ADR 0007 d8; check-t0.sh:126-135) — **OBSERVED**. |
| Router management service on edge01 | up-ot-04:321-328, 498-506 | edge01 is VyOS (template.yaml:31) — **OBSERVED**; which management service(s) listen and on which port is **UNKNOWN** (nothing in the sources read fixes SSH or any port on edge01). |
| Resolver query log on edge01 | up-gov-04:56-57, 323-340, 507-524 | **UNKNOWN** — no query-logging capability for edge01's `dns_forwarder` is established in the sources read. |
| Packet capture on edge01 | up-ot-04:407-409 | **UNKNOWN** — capability plausible for VyOS but not citeable from these sources. |
| Threat-actor infrastructure | up-cti-05 (lookalike domains, 144-158) | threat stub, two domains, stub-only pages (template.yaml:68; fixture.py:50-59; docs/greyspace-plan.md slice 5 "Stub only") — **OBSERVED**. |
| Downloadable signed package on the developer site | up-gov-04:233-238 | **UNKNOWN** — T0 site files are static pages only: index.html, about.html, news/latest.html, robots.txt, style.css (fixture.py:82-106); no package artefact exists in the fixture. |
| Vendor update page for an office router | up-ot-04:50-51 | **UNKNOWN** — no router-vendor site or update page exists in the T0 fixture (fixture.py:27-48). This step needs instructor-provided overlay content or a corpus change. |

## 5. Post-build checks

None run yet — **every status is `blocked`**. Commands are cited from
`greyspace/scripts/check-t0.sh` (the CI proof the stack passes end to end, ADR 0007
Consequences) and run *inside* the stack there; the range-side resolver address students
use is not fixed by any source, so from ws01/ws02 the tool and address are **UNKNOWN**
where marked. Expected values cite their source.

| # | Check | Command (source) | Expected | Status |
|---|---|---|---|---|
| P1 | Stack health after configure | `bin/gs health` on gs-core (ADR 0007 d7 — the configure stage runs it) | exits clean | blocked |
| P2 | Resolver answers a site name | `dig +short +time=2 +tries=1 @198.18.0.53 <site> A` (check-t0.sh:80) — range-side address UNKNOWN | the site's manifest IP (fixture.py:65-67) | blocked |
| P3 | NXDOMAIN for a nonexistent name | `dig @198.18.0.53 no-such-site-….com A` (check-t0.sh:87) | NXDOMAIN | blocked |
| P4 | Site served by name over HTTP | `curl -fsS http://<site>/` (check-t0.sh:100; port implicit — **INFERRED** tcp/80, nginx:alpine) | fixture page body | blocked |
| P5 | HTTPS with Greyspace CA cert | `curl --cacert root.crt https://<site>/` after fetching `http://pki.gs-infra.net/root.crt` (check-t0.sh:126-135) | serves the fixture page; chain validates to the Greyspace root | blocked |
| P6 | BGP between the ISPs | `vtysh -c 'show ip bgp'` on isp-a (check-t0.sh:93) | peer prefix 198.19.0.0/16 learned | blocked |
| P7 | MX for a site zone | `dig +short @198.18.0.53 MX <zone>` (check-t0.sh:152) | `mail.gs-infra.net` (ADR 0007 d8) | blocked |
| P8 | Mail round-trip | SMTP `smtp://mail.gs-infra.net:25` then webmail message list (check-t0.sh:155-159; port 25 **OBSERVED** there) | message appears in webmail API | blocked |
| P9 | NTP | `chronyd -Q -t 20 'server ntp.gs-infra.net iburst maxsamples 2'` (check-t0.sh:166) | time sample returned | blocked |
| P10 | Threat domains resolve and answer | `dig @198.18.0.53 update-cdn-sync.net A`; `curl http://<threat>/` (check-t0.sh:113-115) | 198.19.250.10 (fixture.py:55); threat page | blocked |
| P11 | NPC traffic present | webfarm access log contains `GreyspaceNPC/1` (check-t0.sh:172; profile `office-day`, template.yaml:67) | hits > 0 | blocked |
| P12 | Workstations log in | log in to ws01 / ws02 (Windows 11) | desktop session | blocked — credentials UNKNOWN (§3) |
| P13 | edge01 management login | sign in with the instructor-assigned operator account (up-ot-04:326-328) | session opens | blocked — account seeding UNKNOWN (§3) |
| P14 | dc01 admin session | open an administrative session on dc01 from ws01 (up-idm-06:227-229) | domain account list renders | blocked — credentials UNKNOWN |
| P15 | Signed-zone DNSSEC (up-net-05:54-55) | UNKNOWN — no DNSSEC implementation found in the sources read | authenticated (AD) response | blocked — **risk**: may be impossible on this stack today |
| P16 | DoT on tcp/853 (up-net-05:144-161) | UNKNOWN | encrypted query answered | blocked — **risk** |
| P17 | SPF/DKIM/DMARC results in webmail (up-net-05:237-254) | UNKNOWN | result headers present | blocked — **risk** |

## 6. Courses using this template

Grep `template: greyspace-internet` over `content/courses/*.yaml` matches exactly five
courses; in each, all six modules' `lab_range.template` name it. Line numbers below are
the six `template:` occurrences — **OBSERVED**. The "first lab step that would fail if
the template were wrong" cites the earliest step that needs a template-derived fact.

| Course | Modules using the template (lines) | First lab step that would fail if the template were wrong |
|---|---|---|
| up-ot-04-iot-device-security-testing.yaml | 47, 135, 227, 318, 405, 493 | m1 step 1 (lines 50-51): open the office-router vendor update page on the greyspace internet from ws01 — needs a vendor site the T0 fixture does not have (**UNKNOWN**, §4) |
| up-net-05-protocol-service-hardening-clinic.yaml | 48, 142, 235, 330, 424, 519 | m1 step 2 (lines 54-55): query the resolver for a name in the signed greyspace zone and check the authenticated flag — needs DNSSEC the stack has not shown (**UNKNOWN**, §4) |
| up-idm-06-federated-identity-b2b-access-design.yaml | 47, 139, 225, 311, 393, 478 | m1 step 2 (lines 52-53): browse the greyspace webmail site and record the sign-in host — sign-in behaviour **UNKNOWN** (no IdP in the stack sources) |
| up-gov-04-supply-chain-third-party-risk-management.yaml | 49, 139, 231, 321, 413, 505 | m1 step 3 (lines 56-57): open the resolver query log on edge01 — query logging on edge01 **UNKNOWN** |
| up-cti-05-disinformation-open-source-threat-monitoring.yaml | 51, 142, 233, 326, 421, 513 | m1 step 2 (lines 57-58): visit the news site and the social site and record three posts mentioning the organisation — T0 posts are static "Nothing to report" pages (fixture.py:100-102); the social/content depth the step assumes is **UNKNOWN** |

Instructor variants under `content/courses/instructor/` contain no `template:` lines and
are out of scope for this checklist (ticket glob) — **OBSERVED** (grep).

## 7. Known gaps and risks (UNKNOWN register)

1. **No DNSSEC / SPF / DKIM / DMARC in the stack** — nothing found under the Greyspace
   runtime sources read; up-net-05 modules 1, 3 and 4 assume all of them. Highest risk
   in this checklist.
2. **Search not done** (docs/greyspace-plan.md slice 3) and T0 "search" sites are static
   pages; up-cti-05 m1 step 3 (lines 60-61) expects search to return results.
3. **No vendor-update page and no downloadable package in the T0 fixture**;
   up-ot-04 m1 step 1 and up-gov-04 m3 step 1 (lines 233-238) depend on overlay content
   or a corpus change that no source here arranges.
4. **No accounts declared** in the template (ADR 0012/0013); several courses assume
   instructor-assigned accounts and a partner share whose seeding no source defines.
5. **edge01 capabilities unproven**: resolver query logging, packet capture, management
   service ports — plausible for VyOS but not citeable from the sources read.
6. **Simulator-proven only**: Greyspace is proven on vcsim and the T0 Docker stack
   (CI `greyspace`), not on a real vCenter (ADR 0007 Consequences; docs/current-state.md);
   the `greyspace-host` Packer image is written but has not been built. A real vSphere
   build is a stop condition for declaring any `pass` here.
