# Cyber Operator Curriculum Calendar (DP 1 & DP 2) — DRAFT PROPOSAL

> **Status: unsourced draft. Not authoritative. Do not cite.**
>
> The course codes (`C101`–`C404`, `RMC C201`–`RMC C212`), the course titles, and the
> Sep 2026 – Apr 2030 date grid in this document were **generated, not sourced**. No
> Algonquin College or RMC programme outline backs them. They are a structural
> proposal for discussion only.
>
> Nothing here has been validated by Standards, and none of it may be presented to
> CAF users, used in an accreditation submission, or treated as a record of authority.
> Replace this file wholesale once a real programme outline is obtained; do not
> incrementally "correct" invented data into apparent legitimacy.
>
> Machine-readable counterpart: `content/catalogue/cyber_operator_programme.csv`,
> where every row carries `provenance=unsourced`. Courses loaded from it are created
> **unpublished** and **unbound** to any qualification.

## How "DP 1" and "DP 2" are used here

TrueNorth already defines DP1/DP2 in `control-plane/api/app/qsp_paths.py:44-72`, and
that definition is the authoritative one:

| | `dp_order` | Rank | Qualifications (QSP) | Track |
|---|---|---|---|---|
| **DP 1** | 1 | Pte | `ALJQ` | progression |
| **DP 2** | 2 | Cpl | `TEMP67` (progression), `TEMP64` (Red, specialty), `ALRA` (Malware, specialty) | progression + specialty |

This document uses **the same DP1/DP2** — it describes the *academic delivery schedule*
for those developmental periods (DP 1 at Algonquin College, DP 2 at RMC), not a second,
competing meaning. DP1/DP2 here are **not** new tags: no course produced from this
calendar introduces a `dp1`/`dp2` tag namespace, because `dp_order` on the existing
`Qualification` rows already carries it.

Note that `qsp_paths.py:57-64` deliberately leaves DP 3–5 rank labels blank because no
CAF source on-box establishes them — *"fabricated rank data must never reach CAF
users."* This document is held to the same standard, which is why the header above
exists.

**Scope** – 3-year DP 1 (Algonquin College) followed by 8-month DP 2 (Royal Military
College). All semesters assume a 15-week, 37.5 h / week schedule (5 days x 7.5 h).

---

## Weekly Layout (applied every semester)
| Day | 07:30‑09:00 | 09:00‑10:30 | 10:45‑12:15 | 12:15‑13:15 (Lunch) | 13:15‑14:45 | 15:00‑16:30 |
|-----|-------------|-------------|-------------|----------------------|-------------|-------------|
| **Mon** | Lecture A (2 h) | Q&A / Demo (1 h) | **Lab A** (1.5 h) | – | Lecture B (2 h) | **Lab B** (1.5 h) |
| **Tue** | Lecture C (2 h) | Guest case‑study (1 h) | **Lab C** (1.5 h) | – | Independent lab / tutoring (3 h) | – |
| **Wed** | Lecture D (2 h) | Quiz / short assessment (1.5 h) | Lecture E (2 h) | – | Review session (1 h) + **Lab D** (2 h) | – |
| **Thu** | Lecture F (2 h) | Group work (1 h) | **Lab E** (1.5 h) | – | Project sprint (3 h) | – |
| **Fri** | Review & office hours (3 h) | Assignment briefing (1.5 h) | **Capstone sprint** / project work (3 h) | – | – | – |

*Each 3‑credit course occupies roughly 21 h/week (lecture + lab + assessment). Two concurrent courses fill the 37.5‑hour week.*

---

## Semester‑by‑Semester Course Mapping

### DP 1 – Year 1 – Fall (Foundations Ⅰ) – Sep 2026 → Dec 2026
| Course A | Course B |
|----------|----------|
| **C101 – Computer Architecture & Systems** | **C102 – Operating‑System Fundamentals** |
| **C103 – Intro to Networking** | **C104 – Intro to Programming (Python)** |
| **C105 – Foundations of Cybersecurity** | – (Weeks 6‑15 become **C106 – Cryptography Basics**) |

### DP 1 – Year 1 – Spring (Foundations Ⅱ) – Jan 2027 → Apr 2027
| Course A | Course B |
|----------|----------|
| **C107 – Secure Software Development** | **C108 – Linux System Administration** |
| **C109 – Network Services & Protocols** | **C110 – Intro to Risk Management** |

### DP 1 – Year 2 – Fall (Core Ⅰ) – Sep 2027 → Dec 2027
| Course A | Course B |
|----------|----------|
| **C201 – Network Defense & Firewalls** | **C202 – Secure Communications (TLS/VPN)** |
| **C203 – Identity & Access Management** | **C204 – Security Monitoring & SIEM** |
| **C205 – Lab: Hardened Lab Network** | – (Weeks 13‑15 become **C206 – Incident Response Foundations**) |

### DP 1 – Year 2 – Spring (Core Ⅱ) – Jan 2028 → Apr 2028
| Course A | Course B |
|----------|----------|
| **C207 – Incident Response Foundations** | **C208 – Windows Forensics** |
| **C209 – Malware Analysis 101** | **C210 – Threat Modeling & ATT&CK** |
| **C211 – Lab: Simulated Incident Exercise** | – (Weeks 13‑15 become **C212 – Advanced Incident Response**) |

### DP 1 – Year 3 – Fall (Core III) – Sep 2028 → Dec 2028
| Course A | Course B |
|----------|----------|
| **C301 – Advanced Threat Hunting** | **C302 – Red‑Team Fundamentals** |
| **C303 – Cloud Security (AWS/Azure)** | **C304 – IoT & Embedded Device Security** |
| **C305 – Advanced Malware Reverse‑Engineering** | **C306 – Lab: Full‑Scale Adversary Emulation** |

### DP 1 – Year 3 – Spring (Capstone) – Jan 2029 → Apr 2029
| Course A | Course B |
|----------|----------|
| **C401 – Professional Ethics & Legal Frameworks** | **C402 – Capstone Project Planning** |
| **C403 – Capstone Implementation (6 weeks)** | **C404 – Capstone Assessment & Presentation (9 weeks)** |

---

### DP 2 – RMC – Advanced Technical (8 months)
#### Fall 2029 – Sep 2029 → Dec 2029 (Advanced Technical Ⅰ)
| Course A | Course B |
|----------|----------|
| **RMC C201 – Advanced Network Defense** | **RMC C202 – Threat Hunting – Advanced** |
| **RMC C203 – Cloud Security – Advanced** | **RMC C204 – IoT Firmware Reverse‑Engineering** |
| **RMC C205 – Red‑Team Operations** | – (Weeks 13‑15 become **RMC C206 – Capstone Planning**) |

#### Spring 2030 – Jan 2030 → Apr 2030 (Advanced Technical Ⅱ)
| Course A | Course B |
|----------|----------|
| **RMC C207 – Advanced Malware Analysis** | **RMC C208 – Incident Response – End‑to‑End** |
| **RMC C209 – Cyber Threat Intelligence** | **RMC C210 – Cloud‑Native Defense** |
| **RMC C211 – Advanced IoT Security** | **RMC C212 – Capstone Execution & Assessment** |

---

## Month‑by‑Month Calendar Summary
| Month / Year | Activity |
|--------------|----------|
| Sep 2026 – Dec 2026 | DP 1 Fall – Foundations Ⅰ (Weeks 1‑15) |
| Jan 2027 – Apr 2027 | DP 1 Spring – Foundations Ⅱ |
| May‑Aug 2027 | Summer pause |
| Sep 2027 – Dec 2027 | DP 1 Fall – Core Ⅰ |
| Jan 2028 – Apr 2028 | DP 1 Spring – Core Ⅱ |
| May‑Aug 2028 | Summer pause |
| Sep 2028 – Dec 2028 | DP 1 Fall – Core III |
| Jan 2029 – Apr 2029 | DP 1 Spring – Capstone |
| May‑Aug 2029 | Summer pause |
| Sep 2029 – Dec 2029 | DP 2 Fall – Advanced Technical Ⅰ |
| Jan 2030 – Apr 2030 | DP 2 Spring – Advanced Technical Ⅱ |
| Apr 2030 (final week) | Program completion |

**Holidays** – Thanksgiving (mid‑Oct), Christmas break (mid‑Dec to early Jan), and a 1‑week Spring break (late Apr) are marked as “no‑class” days within the 15‑week semester count.


---

*No course definitions from this calendar exist in the seed data. `seed.py` seeds
reference data only (nations, coalitions, auth zones, infrastructure, AI backends)
and deliberately seeds no courses. To load this proposal into a dev database, use
the programme catalogue CSV and its importer — see
`content/catalogue/cyber_operator_programme.csv` and
`POST /api/v1/courses/import-programme`. Everything it creates is unpublished.*
