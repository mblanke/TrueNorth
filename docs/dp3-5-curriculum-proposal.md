# DP 3–5 Curriculum — Proposal

> **Status: PROPOSED · provenance: unsourced · not Standards-validated.**
>
> Nothing in §2 onward is drawn from a CAF source. The qualification codes, course
> codes, durations and objective structures below are a reviewable starting point for
> Standards, not an accreditation decision, and no part of this has been ingested into
> the spine. §1 is the exception: it records what is already in `crosswalk.csv`.
>
> This mirrors the rule the authored courses follow — content whose source cannot be
> cited is kept unpublished and unbound from the qualification spine. See
> `docs/development.md`, "Provenance rules".

---

## 1. What exists today (sourced)

From the ingested crosswalk and `qsp_paths.DP_PROGRESSION`:

| DP | Rank | QSP code(s) | NICE work roles covered | Status |
|----|------|-------------|-------------------------|--------|
| 1 | Pte | `ALJQ` | `PR-CDA-001`, `IN-FOR-002`, `PR-CIR-001` | Defined — basic occupation |
| 2 | Cpl | `TEMP67` (progression), `TEMP64`, `ALRA` (specialty) | `PR-CDA-001`; `AN-EXP-001`; `IN-FOR-002` | Defined — journeyman |
| 3 | — | — | — | Not defined |
| 4 | — | — | — | Not defined |
| 5 | — | — | — | Not defined |

`TEMP64` and `TEMP67` are placeholder identifiers for qualifications whose real codes
have not been issued. They are deliberately conspicuous.

**`qsp_paths.py` leaves DP 3–5 rank labels empty on purpose.** No CAF source on-box
establishes the developmental-period progression for this occupation, and a guessed
rank on a CAF-facing screen is worse than a blank one. Filling those rungs is a data
change, not a code change — see §6.

---

## 2. The design constraint that shapes everything below

**A Canadian cyber operator is triple-hatted or more.** A US Cyber Protection Team is
a 39-person construct — a headquarters section, two mission elements and a support
element — which can field a dedicated OT specialist, a dedicated intelligence analyst
and a dedicated exploitation operator concurrently. The Canadian equivalent is 8–12.
Comparable mission scope, three to four times fewer people.

This is already visible in the existing spine, though nothing states it: **`ALJQ`, the
DP 1 Private qualification, spans three NICE work roles** — Cyber Defense Analyst,
Forensics Analyst and Incident Responder. A US 17C does not carry all three at entry.

The consequence for DP 3–5 is structural:

- **Breadth is the requirement, not a career choice.** New parallel specialty streams
  (Mission Assurance, Cyber Intelligence, Offensive Cyber as separate ladders) encode
  the 39-person model. At 8–12 they fragment an already-small force. Those capabilities
  belong to every senior operator.
- **Depth comes from proficiency, not from a separate ladder.** One qualification per
  developmental period, each spanning more work roles than the last, with rising
  expected proficiency on competencies already held.
- **The trade has to be stated.** Breadth across defensive, OT, intelligence and
  offensive work at every rank means either more training hours or less depth per
  domain. Any hours figure that does not acknowledge this is not a plan.

---

## 3. Proposed qualifications

Two streams, forking after DP 3.

Codes use a `PROP-` prefix so they cannot be mistaken for issued designators.

| DP | Proposed code | Rank | Track | Working title |
|----|---------------|------|-------|---------------|
| 3 | `PROP-DP3` | Cpl (MCpl appointment) | `progression` | Cyber Operator — Technical Specialist |
| 4 | `PROP-DP4` | Sgt | `progression` | Cyber Operator — Team Lead |
| 4 | `PROP-DP4-T` | Cpl (MCpl appointment) | `specialty` | Cyber Operator — Master Technician |
| 5 | `PROP-DP5` | WO | `progression` | Cyber Operator — Senior Advisor |
| 5 | `PROP-DP5-T` | Sgt | `specialty` | Cyber Operator — Principal Technical Authority |

### 3.1 Why a technical stream

A single ladder makes every route past DP 3 a leadership route — Team Lead, then Senior
Advisor. An operator whose value is depth rather than command has three options under
that model: take a job they do not want, stagnate, or leave. In an 8–12 person team,
losing the one person who can actually reverse a sample is not a manageable loss.

The technical stream lets an operator gain **developmental depth without rank
progression**. This needs no schema change, because `dp_order` and `rank_level` are
already separate columns:

- `dp_order` — the depth of qualification reached
- `rank_level` — the rank actually held
- `track` — `progression` (command) or `specialty` (technical mastery)

So `PROP-DP4-T` sits in the DP 4 column at MCpl, and `PROP-DP5-T` in the DP 5 column at
Sgt. The operator advances in qualification and pay-relevant seniority without being
required to command. *(Note: `models.py:625` currently comments `dp_order` as "position
in the rank-based progression", which conflates the two. If this stream is adopted that
comment needs correcting — the column is depth, not rank.)*

This is also the shape the spine already uses at DP 2, where `TEMP64` and `ALRA` sit
beside `TEMP67` as specialty streams at the same rank.

### 3.2 How this squares with triple-hatting

§2 argues that breadth is mandatory, which appears to contradict a specialist track. It
does not, and the distinction is the axis:

- **The original draft's specialties were capability domains** — Mission Assurance,
  Cyber Intelligence, Offensive Cyber as separate ladders. That is wrong for an 8–12
  person team, because every operator needs all three.
- **A technical stream is career intent.** It carries the *same* breadth as the
  leadership stream at the same DP, and adds depth in one or two domains on top.

A Master Technician is not a narrower operator. They hold everything `PROP-DP4` holds,
minus the command competencies, plus mastery of a chosen domain. A small force needs
both properties at once: everyone broad enough to cover, and someone deep enough to
call when it matters.

**On the ranks.** The CAF NCM ladder is Pte → Cpl → **MCpl → Sgt → WO**, so the
mapping is consistent with the rank structure. Two caveats:

1. **Master Corporal is an appointment, not a rank.** Under QR&O the CDS appoints a
   corporal as master corporal; the substantive rank remains Corporal. `rank_level`
   for DP 3 therefore records an appointment where DP 1–2 record ranks. Either the
   field tolerates both and says so, or DP 3 reads "Cpl (MCpl)".
2. **Warrant Officer is not "Sergeant-Major".** Sergeant-Major is an appointment held
   at MWO (sub-unit) or CWO (regimental) level, one to two ranks above WO.

Neither of these makes the ladder *sourced*. It remains an inference from the general
CAF rank structure, not from a QSP for this occupation.

---

## 4. Competency coverage

Work-role IDs are NIST SP 800-181 (`SP800-181r1`, the version the crosswalk pins).
Every code below was verified against the framework; none are invented.

| DP | Qualification | Primary work roles | Supporting work roles | Cumulative |
|----|---------------|--------------------|-----------------------|------------|
| 1 | `ALJQ` | `PR-CDA-001` | `IN-FOR-002`, `PR-CIR-001` | 3 |
| 2 | `TEMP67` + specialties | `PR-CDA-001`, `AN-EXP-001`, `IN-FOR-002` | `PR-VAM-001` | 4 |
| 3 | `PROP-DP3` | `PR-CDA-001`, `IN-FOR-002` | `OM-ANA-001`, `AN-TWA-001`, `PR-VAM-001` | 5 |
| 4 | `PROP-DP4` (lead) | `PR-CIR-001`, `AN-TWA-001` | `CO-OPS-001`, `OV-TEA-002`, `OV-PMA-001` | 6 |
| 4 | `PROP-DP4-T` (technical) | `AN-EXP-001`, `IN-FOR-002`, `OM-ANA-001` | `PR-VAM-001`, `AN-TWA-001` | 6 |
| 5 | `PROP-DP5` (advisor) | `SP-ARC-002`, `OV-TEA-001` | `SP-ARC-001`, `OV-LGA-001`, `OV-PMA-001` | 6 |
| 5 | `PROP-DP5-T` (technical authority) | `AN-EXP-001`, `IN-FOR-002`, `SP-ARC-002` | `CO-OPS-001`, `OM-ANA-001`, `OV-TEA-002` | 7 |

The two streams carry the **same number** of work roles at each DP. They differ in
which — the leadership stream adds command, programme and instruction roles; the
technical stream adds exploitation, forensics and architecture depth. Neither is
narrower. `PROP-DP5-T` retains `OV-TEA-002` (Cyber Instructor) as supporting because a
principal technical authority who cannot teach is a single point of failure in a team
of eight.

The seven NICE categories are Securely Provision (SP), Operate and Maintain (OM),
Oversee and Govern (OV), Protect and Defend (PR), Analyze (AN), Collect and Operate
(CO) and Investigate (IN). There is no "Lead" or "Educate" category — instruction sits
under Oversee and Govern (`OV-TEA-*`).

**This requires a modelling change.** Today every performance objective maps to exactly
one work role, which is the specialist model. `ObjectiveCompetencyMap` is already
many-to-many with a `relation_type` of `primary`/`supporting`, so the schema supports
the table above — nothing currently uses it. Expressing triple-hatting means populating
supporting roles, not altering the model.

### NIST CSF 2.0

CSF 2.0 has **six** functions: **Govern**, Identify, Protect, Detect, Respond, Recover.
`content/catalogue/nist_csf_2_0_taxonomy.csv` carries `GV` already.

| DP | CSF functions |
|----|---------------|
| 1–2 | Identify, Protect, Detect |
| 3 | Identify, Protect, Detect, Respond |
| 4 | Identify, Protect, Detect, Respond, Recover |
| 5 | All six, **Govern** being the addition that distinguishes DP 5 |

Govern is what separates a senior advisor from a senior operator, so a DP 5 that omits
it has not described the rank.

---

## 5. Indicative course structure

Hours are **estimates**, not costed figures. One code set, used consistently.

### DP 3 — Technical Specialist (Cpl / MCpl)

| Code | Title | Hrs | Work roles | CSF |
|------|-------|-----|-----------|-----|
| `PROP-DP3-01` | Advanced Detection & Log Analysis | 150 | `PR-CDA-001` | Detect |
| `PROP-DP3-02` | Host & Memory Forensics | 140 | `IN-FOR-002` | Detect, Respond |
| `PROP-DP3-03` | OT & IoT Security Foundations | 150 | `OM-ANA-001` | Identify, Protect |
| `PROP-DP3-04` | Threat Intelligence Fundamentals | 110 | `AN-TWA-001` | Identify, Detect |
| **Total** | | **550** | | |

### DP 4 — both streams (Sgt / MCpl)

Three shared courses, then one course that differs by stream. Shared delivery is the
point: in a team of 8–12 the two streams train together for most of the period.

| Code | Title | Hrs | Stream | Work roles | CSF |
|------|-------|-----|--------|-----------|-----|
| `PROP-DP4-01` | Incident Command & Recovery | 180 | both | `PR-CIR-001` | Respond, Recover |
| `PROP-DP4-02` | Adversary Emulation & Exploitation | 170 | both | `AN-EXP-001`, `CO-OPS-001` | Identify, Detect |
| `PROP-DP4-03` | Operational Threat Modelling (IT/OT) | 160 | both | `AN-TWA-001` | Identify |
| `PROP-DP4-04` | Instructing & Leading Operators | 100 | lead | `OV-TEA-002`, `OV-PMA-001` | Protect |
| `PROP-DP4-T4` | Advanced Reverse Engineering & Deep Forensics | 140 | technical | `IN-FOR-002`, `OM-ANA-001` | Detect, Respond |
| **Lead total** | | **610** | | | |
| **Technical total** | | **650** | | | |

### DP 5 — both streams (WO / Sgt)

| Code | Title | Hrs | Stream | Work roles | CSF |
|------|-------|-----|--------|-----------|-----|
| `PROP-DP5-01` | Enterprise Security Architecture | 220 | both | `SP-ARC-002`, `SP-ARC-001` | Identify, Protect |
| `PROP-DP5-02` | Cyber Governance, Policy & Legal | 190 | lead | `OV-LGA-001`, `OV-PMA-001` | **Govern** |
| `PROP-DP5-03` | Curriculum Design & Programme Assurance | 200 | lead | `OV-TEA-001` | Govern, Protect |
| `PROP-DP5-04` | Resilience for Critical Infrastructure | 190 | both | `OM-ANA-001` | Respond, Recover |
| `PROP-DP5-T2` | Principal Technical Authority — Tooling & Tradecraft | 210 | technical | `AN-EXP-001`, `CO-OPS-001` | Identify, Detect |
| `PROP-DP5-T3` | Technical Mentorship & Standards Authorship | 160 | technical | `OV-TEA-002` | Govern |
| **Lead total** | | **800** | | | |
| **Technical total** | | **780** | | | |

**Totals by route**, against DP 1's 2,770 h and DP 2's 820 h already in the catalogue:

| Route | DP 3 | DP 4 | DP 5 | Total |
|-------|------|------|------|-------|
| Leadership | 550 | 610 | 800 | **1,960 h** |
| Technical | 550 | 650 | 780 | **1,980 h** |

The two routes cost within 1% of each other. That is deliberate: a technical stream
that is visibly cheaper reads as the lesser path, and would be treated as one.

Standards referenced by the OT content — IEC 62443, NIST SP 800-82 — and by the
architecture content — NIST SP 800-53 — are real and current. Adversary technique
mapping is **ATT&CK for ICS**; there is no framework called "ATT&CK-OT" (the related
MITRE work is *Defending OT with ATT&CK*).

---

## 6. What ingesting this would actually require

Corrected against the code as it stands.

1. **Add rows to `crosswalk.csv`** for `PROP-DP3/4/5` and their performance objectives,
   matching the existing column contract.
2. **`POST /qsp/import-crosswalk`** creates the `Qualification`, `PerformanceObjective`
   and `EnablingObjective` rows. It does **not** set `dp_order`, `track` or
   `rank_level` — those stay at 0 / `progression` / empty.
3. **`DP_PROGRESSION` in `qsp_paths.py` must gain entries** for the new codes, because
   `import-competency-crosswalk` stamps `dp_order`/`track`/`rank_level` from that dict.
   *This is the step that makes ingest a code change rather than a data change, and it
   is the thing worth fixing first* — see §7.
4. **`POST /qsp/import-competency-crosswalk`** then stamps the DP metadata and links
   competencies. There are no `Qualification.nist_functions` or `nice_codes` columns;
   framework mapping goes through `ObjectiveCompetencyMap`.
5. **Add catalogue rows** to `cyber_operator_programme.csv` for the courses. Tags
   (`DP3`/`DP4`/`DP5`, institution, term, `delivers:*`) are derived at import by
   `programme_ingest.catalogue_tags` — never authored.
6. **Author course YAML** under `content/courses/`, with module `po:` bindings where a
   module genuinely satisfies an objective. `CourseModule.po_id` is the only link
   `qsp_progress` walks.
7. **`POST /qsp/generate-exercises`**, then **`POST /qsp/generate-learning-paths`**, in
   that order.

Everything imports unpublished. Nothing here is learner-facing.

---

## 7. Recommended code change, independent of this proposal

**`DP_PROGRESSION` should be data, not a hardcoded dict.** `qsp_paths.py:45` holds
`dp_order`, `track`, `rank_level` and `title` for exactly four qualifications. Any new
developmental period requires editing Python — which contradicts the module docstring's
claim that ingesting a real DP 3 QSP fills the rung in "with no code or UI change".

Moving those four fields into `crosswalk.csv` columns would make that claim true, and
would let Standards add a period without a developer. It would also let `rank_level`
stay blank for a qualification whose rank is genuinely unknown, rather than forcing a
choice between a guess and a code change.

---

## 8. Open questions for Standards

1. **Is the DP 3–5 rank mapping right?** MCpl / Sgt / WO follows the CAF NCM ladder but
   is not sourced from a QSP for this occupation. If it is wrong, everything keyed to it
   is wrong.
2. **Does DP 3 key to a rank or an appointment?** Master Corporal is an appointment held
   by a Corporal.
3. **Are the DP 2 specialties cumulative or exclusive?** The career map draws `branch`
   edges into `TEMP64` and `ALRA`, which reads as "choose one". If a triple-hatted Cpl
   holds all three DP 2 qualifications, the map is misrepresenting the occupation and
   the `locked`/`available` logic needs revisiting.
4. **Where is the breadth/depth line?** Triple-hatting forces a choice between more
   hours and less depth per domain. §5's hours assume the current programme's pace; that
   assumption needs testing against real course design.
5. **Does the technical stream carry rank and pay incentive?** §3.1 lets an operator
   gain DP 4–5 depth while holding MCpl or Sgt. That only retains people if the
   qualification carries real seniority. If a Master Technician is paid as a Corporal
   with no advancement, the stream is a label on a dead end and will not solve the
   retention problem it exists for. This is a compensation and career-management
   question, not a curriculum one, and it has to be answered outside this document.
6. **Can the technical stream re-enter the leadership stream?** An operator who takes
   `PROP-DP4-T` and later wants command needs a defined bridge, or the fork becomes a
   one-way door and people will decline it for that reason alone.

---

## 9. Corrections applied to the earlier draft

Recorded so the same errors are not reintroduced.

| Earlier claim | Correction |
|---|---|
| WO "often called Sergeant-Major" | Sergeant-Major is an appointment held at MWO/CWO |
| MCpl treated as a rank | Appointment; substantive rank remains Cpl |
| "All five CSF functions" | CSF 2.0 has six — Govern was added |
| "Operate & Maintain — Cyber Defense Analyst" | `PR-CDA-001` is **Protect and Defend** |
| "Lead" / "Educate" NICE categories | Not categories; instruction is `OV-TEA-*` under Oversee and Govern |
| "ATT&CK-OT" | **ATT&CK for ICS** |
| 18X / 18C / 18A as US cyber | 18-series is **Special Forces**; cyber is **17C** (enlisted), **17A** (officer) |
| 1B4X2 / 1B4X3 as officer AFSCs | No evidence they exist; `1B4X1` is real and **enlisted** |
| `Qualification.nist_functions` / `nice_codes` | Neither column exists |
| `import-crosswalk` sets `rank_level` | `import-competency-crosswalk` does, from `DP_PROGRESSION` |
| Two code sets for the same qualifications | One `PROP-*` set throughout |
| 3,380 h total | Double-counted two course sets; restated as 1,960 h estimated |
| 1:1 Canadian↔US role mapping | Dropped — a Canadian qualification spans what a US team distributes across several roles |

**On the US mapping.** It was offered as a pointer to reference material, which is
reasonable — but a 1:1 equivalence is the wrong shape for it, because the whole point of
§2 is that the ratio is not 1:1. If US material is useful for course authoring, cite it
per course against the specific work role (`17C` for defensive operator content, `17A`
for planning content), not as a qualification-level equivalence.
