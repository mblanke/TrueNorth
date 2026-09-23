---
title: "xAPI and cmi5: Consolidated Technical Reference"
doc_id: xapi-cmi5-reference
edition: 1.0
compiled: 2026-09-23
basis: >
  Official specification text (xAPI 1.0.3, cmi5 Quartz 1st Edition, xAPI Profiles 1.0),
  the ADL/USALearning technical report on xAPI 1.0.3 -> IEEE 9274.1.1 changes,
  IEEE SA project pages, ADL project pages, and vendor engineering notes.
  Status items verified by web research on 2026-09-23.
audience: "Program leads, architects, LRS/LMS integrators, content developers, and RAG/LLM pipelines"
companion_files:
  - examples/            # validated JSON statements, LMS.LaunchData, cmi5.xml
  - scripts/             # LRS smoke test, cmi5 session simulator, source fetcher, chunker
  - lab/                 # Docker Compose for a local SQL LRS on Postgres
  - au/                  # minimal cmi5 Assignable Unit (vanilla JS)
  - corpus/xapi-cmi5-chunks.jsonl   # this file, pre-chunked for retrieval
tags: [xAPI, Experience API, Tin Can, cmi5, LRS, LMS, IEEE 9274.1.1, P9274.3.1, xAPI Profiles, SCORM, ADL, TLA]
---

# xAPI and cmi5: Consolidated Technical Reference

## 0. How to use this file

Tag: META.USAGE

- Each numbered section is written to stand on its own, so it survives chunked retrieval. Every section carries a `Tag:` line (for example `XAPI.STATEMENT.ACTOR`, `CMI5.LAUNCH.FETCH`) that you can grep or filter on.
- Version markers: **[1.0.3]** means xAPI 1.0.3 behaviour, **[2.0]** means IEEE 9274.1.1-2023 behaviour, **[Quartz]** means cmi5 Quartz 1st Edition.
- Normative language (MUST/SHALL/SHOULD) is paraphrased. Before you build or accept something against a requirement, confirm it in the authoritative text. `scripts/fetch-sources.sh` clones the official spec repositories so you can keep the primary sources beside this file.
- Confidence and known gaps are listed in Section 19. Read that before quoting anything in a contract.

## 1. Bottom line

Tag: SUMMARY.EXECUTIVE

**Bottom line.** xAPI is the data model and REST API for recording learning and performance events ("statements") in a Learning Record Store (LRS). cmi5 is the xAPI profile that restores the LMS contract SCORM used to provide: packaging, launch, credential hand-off, session rules and completion/pass semantics. Use both: cmi5 for anything an LMS launches, plain xAPI (governed by an xAPI Profile) for everything else, such as simulations, ranges, on-the-job observation and mobile apps.

**Why it matters.** SCORM confines tracking to a browser frame talking to one LMS, with a fixed and shallow data model. xAPI lets any system (simulator, range controller, app, instructor tablet) record structured evidence to an LRS, and lets that evidence move between systems. cmi5 makes xAPI content portable between LMSs, which raw xAPI ("Tin Can packages") never achieved.

**Where the standards are (2026-09-23).**
- xAPI 2.0 is a published IEEE standard (IEEE 9274.1.1-2023). Most deployed content and many LRSs are still on 1.0.3.
- cmi5's current release is Quartz 1st Edition (2016). It references xAPI 1.0.3. IEEE project P9274.3.1, which will carry cmi5 forward and align it with xAPI 2.0, is an active project, not an approved standard.
- xAPI Profiles 1.0 is current; IEEE P9274.2.1 work continues.
- Authentication was removed from the xAPI 2.0 base standard. A separate IEEE recommended practice on xAPI cybersecurity (P9274.4.2) is in progress. Your contracts and architecture must specify authentication explicitly.

**Risk.**
- Version skew: cmi5 content speaks 1.0.3; an LRS that only accepts 2.0.0 headers breaks it. Require dual-version support.
- "xAPI-compliant" claims are cheap. Without conformance evidence (ADL LRS Conformance Test Suite; CATAPULT for cmi5) interoperability is not assured.
- Vocabulary drift: without a governing xAPI Profile, each vendor invents verbs and extensions and cross-system analytics fail.
- Privacy: statements are personal information about identifiable people. Actor identifiers, retention and access need the same treatment as HR or training records.

**Recommended action.**
1. Specify cmi5 (Quartz) for LMS-launched content and require xAPI 1.0.3 **and** 2.0.0 support in the LRS.
2. Publish an xAPI Profile for each non-LMS data source before integration starts.
3. Require conformance evidence at acceptance: ADL LRS Conformance Test Suite results for the LRS, CATAPULT LTS results for the LMS, CATAPULT CTS results for content.
4. Use opaque `account` identifiers for actors, never email addresses.
5. Track IEEE P9274.3.1 (cmi5) and P9274.4.2 (security) and plan a version-transition decision when they are approved.

## 2. Standards status and lineage

Tag: STATUS.STANDARDS

| Item | Current version | Body / steward | Status as of 2026-09-23 | Where the text lives |
|---|---|---|---|---|
| xAPI | 2.0 = IEEE 9274.1.1-2023 | IEEE Computer Society LTSC (C/LT), xAPI WG; ADL Initiative | Approved by IEEE SA board 2023-03-30; published 2023-10-10. First open-source specification to become an IEEE standard. | standards.ieee.org/ieee/9274.1.1/7321 ; open-source docs at opensource.ieee.org/xapi |
| xAPI (legacy baseline) | 1.0.3 (September 2016) | ADL Initiative | Stable; still the dominant deployed version | github.com/adlnet/xAPI-Spec |
| cmi5 | Quartz, 1st Edition (2016-06-01) | cmi5 Working Group (ADL-facilitated) | Current release; references xAPI 1.0.3 | github.com/AICC/CMI-5_Spec_Current (branch `quartz`) |
| cmi5 (IEEE) | P9274.3.1 "Standard for Packaging, Launch, and Run-time of Experience Application Programming Interface (xAPI) in Session-based Learning" | IEEE C/LT, xAPI-cmi5 WG | Active PAR (approved 2023-02-15). Not an approved standard. Work aims at xAPI 2.0 alignment with broad backward compatibility. | standards.ieee.org/ieee/9274.3.1/11183 ; opensource.ieee.org/xapi-cmi5/9274.3.1 |
| xAPI Profiles | 1.0 | ADL; IEEE P9274.2.1 WG | 1.0 current; IEEE revision work continuing | github.com/adlnet/xapi-profiles |
| xAPI cybersecurity | P9274.4.2 Recommended Practice for Cybersecurity in the Implementation of xAPI | IEEE C/LT, xAPI-C WG | Active PAR (approved 2019-11-07) | IEEE SA project page 9274.4.2 |

### 2.1 Timeline

Tag: STATUS.TIMELINE

| Date | Event |
|---|---|
| 2010 | AICC starts the CMI-5 project (a redesign of its CMI specifications; the "5" is not a version number). |
| 2011-2013 | Project Tin Can (Rustici Software under an ADL contract) produces the draft that becomes xAPI. Research versions 0.9 and 0.95. |
| Oct 2012 | AICC adopts xAPI as the transport for CMI-5. |
| Apr 2013 | xAPI 1.0.0 released by ADL. 1.0.1 and 1.0.2 follow. |
| Dec 2014 | AICC dissolves; CMI-5 and its archive transfer to ADL. |
| May 2015 | cmi5 "Sandstone" developer release. |
| 2016-06-01 | cmi5 Quartz 1st Edition. |
| Sep 2016 | xAPI 1.0.3 released (introduces the LRS / LRP / LRC role vocabulary). |
| 2017-10-05 | Revised DoDI 1322.26 "Distributed Learning": moves US DoD from mandatory SCORM to SCORM **or** xAPI and directs components to implement xAPI and LRS capabilities "as practical". |
| 2018-09-04 | IEEE PAR submitted to standardize xAPI (9274.1.1). |
| 2019-11-07 | IEEE PAR approved for P9274.4.2 (xAPI cybersecurity recommended practice). |
| 2021 | ADL/USALearning technical report catalogues every 1.0.3 -> 9274.1.1 change. ADL Project CATAPULT releases an open-source cmi5 player and conformance tools. |
| 2023-02-15 | IEEE PAR approved for P9274.3.1 (cmi5). |
| 2023-03-30 | IEEE SA board approves IEEE 9274.1.1-2023 (xAPI 2.0). |
| 2023 | ADL LRS and the ADL LRS Conformance Test Suite add xAPI 2.0. |
| 2023-10-10 | IEEE 9274.1.1-2023 published. |
| 2024-02 | Rustici Engine 23 adds xAPI 2.0 to its LRS; notes that 2.0 content cannot yet be launched interoperably because cmi5 is still on 1.0.x. |
| Aug 2026 | Independent check of primary sources: xAPI 2.0 current; xAPI Profiles 1.0 current with P9274.2.1 continuing; cmi5 still Quartz 1st Edition on xAPI 1.0.3; P9274.3.1 still an Active PAR. |

## 3. Glossary

Tag: GLOSSARY

| Term | Meaning |
|---|---|
| Activity | Anything an actor interacts with or does: a course, AU, question, scenario, inject, piece of equipment. Identified by an IRI. |
| Actor | Who did it: an Agent (person or system) or a Group. |
| ADL | Advanced Distributed Learning Initiative (US DoD). Steward of SCORM, xAPI 1.x, cmi5 and the TLA. |
| AU (Assignable Unit) | cmi5: the unit of launch and tracking, roughly the SCORM SCO. |
| Authority | The agent or group vouching for a statement; normally set by the LRS from the credentials used. |
| Block | cmi5: a grouping of AUs and/or nested blocks inside a course. |
| CATAPULT | ADL project delivering an open-source cmi5 player, a content test suite (CTS), an LMS test suite (LTS), course templates and a requirements file. |
| Client | Anything that talks to an LRS (an LRP, an LRC, or both). |
| Context template | cmi5: the `context` object an LMS writes into `LMS.LaunchData`; the AU must copy it into every statement it sends. |
| Extension | A map keyed by IRIs that carries domain data where the core model has no property. Allowed in activity definitions, `context` and `result`. |
| Fetch URL | cmi5: one-time URL on the LMS that returns the AU's LRS authorization token. |
| IFI (Inverse Functional Identifier) | The property that uniquely identifies an Agent or identified Group: `mbox`, `mbox_sha1sum`, `openid` or `account`. Exactly one per agent object. |
| IRI / IRL | Internationalized Resource Identifier (RFC 3987) / an IRI that resolves as a URL. xAPI identifiers are IRIs; they need not resolve. |
| Language map | Object keyed by RFC 5646 language tags, e.g. `{"en-CA":"completed","fr-CA":"terminé"}`. |
| LMS | Learning Management System. In cmi5 the "LMS" is always an integrated LMS plus LRS. |
| LRC | Learning Record Consumer: reads data from an LRS (dashboards, analytics). |
| LRP | Learning Record Provider: sends data to an LRS (formerly "Activity Provider"). |
| LRS | Learning Record Store: the server that implements the xAPI REST API and stores statements and documents. |
| moveOn | cmi5: per-AU criterion (Passed, Completed, CompletedAndPassed, CompletedOrPassed, NotApplicable) the LMS uses to decide the AU is satisfied. |
| Profile (xAPI Profile) | JSON-LD document defining vocabulary (verbs, activity types, extensions), statement templates and patterns for one domain. cmi5 is itself an xAPI profile. |
| Publisher ID | cmi5: the `id` the course author gave an AU, block or course in the course structure. The LMS must generate different runtime activity IDs. |
| Registration | UUID for one enrollment/attempt context; ties statements and state together. In cmi5, one learner's enrollment in one course. |
| Session | cmi5: one launch of one AU, from `launched` to `terminated` or `abandoned`, identified by a session ID extension. |
| Statement | The immutable record of one event: actor, verb, object, plus optional result, context, timestamp and attachments. |
| TLA | Total Learning Architecture: ADL's reference architecture for a data-centric learning ecosystem; xAPI/LRS is its activity-data layer. |
| Verb | What happened, identified by an IRI (e.g. `http://adlnet.gov/expapi/verbs/completed`) with a human-readable `display`. |
| Voiding | The only way to invalidate a statement: a new statement with verb `voided` referencing the original. |

## 4. xAPI architecture and roles

Tag: XAPI.ARCHITECTURE

**What xAPI specifies.**
1. A JSON data model: statements, plus documents (state, activity profile, agent profile), plus supporting objects (Person, Activity, About).
2. A REST API exposed by an LRS for writing and reading those objects.
3. Rules for validation, immutability, voiding, versioning, concurrency and error handling.

**What xAPI does not specify.** Content launch, packaging, sequencing, the meaning of "complete" in an LMS, analytics or reporting, UI, and (in 2.0) authentication. cmi5 fills the first four for LMS-delivered content. xAPI Profiles fill vocabulary and statement-shape rules for everything else.

**Roles (introduced by 1.0.3; a role, not a product class).**

| Role | Responsibility | Examples |
|---|---|---|
| LRS | Accepts, validates, stores and serves statements and documents; sets `stored`, `authority`, and (if absent) `id` and `timestamp` | SQL LRS, ADL LRS, Rustici LRS, Watershed, Veracity |
| LRP | Generates statements | e-learning AU, simulator, range scoring engine, mobile app, instructor observation tool |
| LRC | Reads statements for use | dashboard, adaptive engine, competency service, data warehouse ETL |

A single product can play several roles. An LMS in cmi5 is both an LRS and an LRP (it writes `launched`, `abandoned`, `waived`, `satisfied`).

**Typical data flow.**
```
[AU / simulator / app] --(HTTPS POST /statements, JSON)--> [LRS] --(forwarding or ETL)--> [enterprise LRS / warehouse]
                                                             ^
                              [dashboards, adaptive engines] | (GET /statements, documents)
```

## 5. xAPI data model: the Statement

Tag: XAPI.STATEMENT

A statement records one event as actor + verb + object, optionally with result, context, time and attachments. Statements are immutable once stored.

### 5.1 Top-level statement properties

Tag: XAPI.STATEMENT.PROPERTIES

| Property | Type | Required | Set by | Notes |
|---|---|---|---|---|
| `id` | UUID | Recommended | LRP (LRS generates if absent) | Set it on the client so retries are idempotent. |
| `actor` | Agent or Group | Yes | LRP | Who. See 5.2. |
| `verb` | Verb | Yes | LRP | What happened. See 5.3. |
| `object` | Activity, Agent, Group, StatementRef or SubStatement | Yes | LRP | To what. See 5.4. |
| `result` | Result | No | LRP | Outcome. See 5.5. |
| `context` | Context | No | LRP | Surrounding circumstances. See 5.6. |
| `timestamp` | Timestamp | No | LRP | When the event happened. **[2.0]** LRS SHALL set it to `stored` if absent. |
| `stored` | Timestamp | Set by LRS | LRS | When the LRS stored it. Basis for `since`/`until` queries. |
| `authority` | Agent or Group | Set/validated by LRS | LRS | Who vouches for the statement (usually derived from credentials). |
| `version` | String | No | LRS | **[1.0.3]** LRPs should not set it; if set it must be `1.0.0`; LRS records `1.0.0` when absent. The patch version in use is carried by the `X-Experience-API-Version` header, not by this property. |
| `attachments` | Array of Attachment | No | LRP | Binary evidence. See 5.8. |

**[2.0]** An LRP SHALL NOT add properties that the standard does not define, except inside extensions, and an LRS SHALL reject statements that do. In 1.0.3 this was a strong recommendation (SHOULD*).

### 5.2 Actor: Agent and Group

Tag: XAPI.STATEMENT.ACTOR

**Agent**
| Property | Required | Notes |
|---|---|---|
| `objectType` | Optional for actor (value `"Agent"`) | Required when an Agent is the statement object. |
| `name` | Optional | Display only; never use for identity matching. |
| IFI (exactly one) | Yes | One of `mbox` (`"mailto:user@example.org"`), `mbox_sha1sum` (hex SHA-1 of the full `mailto:` IRI), `openid` (URI), or `account` `{ "homePage": IRL, "name": string }`. |

**Group**
- `objectType: "Group"` is required.
- **Anonymous group**: no IFI; `member` array of Agents is required. Identity is the membership list for that statement only.
- **Identified group**: has an IFI; `member` optional. Use for persistent teams (crews, cells, syndicates).

**Identity guidance.** cmi5 requires `account`. Use `account` everywhere: `homePage` is the IRL of the identity system, `name` is an opaque, stable, non-PII key (not a service number, not an email). Keep the mapping to real identity in the source-of-record system, not in the LRS.

```json
{"objectType":"Agent","account":{"homePage":"https://id.example.org","name":"a7f3c2e9-5d1b-4a0e-9c1f-2b8e6d4a1c07"}}
```

### 5.3 Verb

Tag: XAPI.STATEMENT.VERB

| Property | Required | Notes |
|---|---|---|
| `id` | Yes | IRI. The meaning of the verb is defined by whoever controls the IRI (ideally in an xAPI Profile). |
| `display` | Recommended | Language map. Non-normative: systems must never infer meaning from `display`. |

Verbs are written in past tense ("completed", "answered"). Reuse existing vocabularies before minting new verbs (ADL verbs in Section 16; community profiles). **[2.0]** LRPs defining new IRIs should use IRIs they control or have permission to use.

### 5.4 Object

Tag: XAPI.STATEMENT.OBJECT

`objectType` selects the kind of object. Default when omitted is `Activity`.

**Activity**
| Property | Required | Notes |
|---|---|---|
| `objectType` | Optional (`"Activity"`) | |
| `id` | Yes | IRI. Stable over time; same thing, same ID. |
| `definition` | Optional | Metadata below. The LRS may keep a canonical definition. |

`definition` properties: `name` (language map), `description` (language map), `type` (activity type IRI), `moreInfo` (IRL to human-readable info), `extensions`, and the interaction properties:

| Interaction property | Notes |
|---|---|
| `interactionType` | `true-false`, `choice`, `fill-in`, `long-fill-in`, `matching`, `performance`, `sequencing`, `likert`, `numeric`, `other` |
| `correctResponsesPattern` | Array of response-pattern strings (format depends on `interactionType`, e.g. `"a[,]c"` for multi-select choice, `"1[:]5"` for numeric range). **[2.0]** No LRS limit on pattern length or array length. |
| `choices`, `scale`, `source`, `target`, `steps` | Arrays of interaction components `{ "id": string, "description": language map }`; which apply depends on `interactionType` (`choices` for choice/sequencing, `scale` for likert, `source`/`target` for matching, `steps` for performance). |

Interactions conventionally use activity type `http://adlnet.gov/expapi/activities/cmi.interaction` and verb `answered`.

**Agent or Group as object**: e.g. "instructor *mentored* trainee". `objectType` required.

**StatementRef**: `{ "objectType": "StatementRef", "id": "<uuid>" }`. Used for voiding and for commenting on or grading another statement.

**SubStatement**: an embedded statement (`objectType: "SubStatement"`) with its own actor, verb and object, used for statements about potential or planned events ("Bob *planned* 'Alice *completed* X'"). It must not contain `id`, `stored`, `version` or `authority`, and its object must not itself be a SubStatement.

### 5.5 Result

Tag: XAPI.STATEMENT.RESULT

| Property | Type | Notes |
|---|---|---|
| `score` | Object | `scaled` (decimal -1..1), `raw` (between `min` and `max`), `min`, `max`. |
| `success` | Boolean | Passed/attained? |
| `completion` | Boolean | Finished? |
| `response` | String | Learner's response (format per `interactionType` for interactions). |
| `duration` | ISO 8601 duration | e.g. `PT1H2M3.45S`. Precision beyond 0.01 s is not compared and may be truncated. |
| `extensions` | Map | Domain outcomes (e.g. time-to-detect, flags captured). |

### 5.6 Context

Tag: XAPI.STATEMENT.CONTEXT

| Property | Type | Notes |
|---|---|---|
| `registration` | UUID | Groups statements belonging to one enrollment/attempt/run. Also scopes State documents. |
| `instructor` | Agent or Group | **[2.0] deprecated** in favour of `contextAgents`. Still valid. |
| `team` | Group | **[2.0] deprecated** in favour of `contextGroups`. Still valid. |
| `contextActivities` | Object | Four arrays of Activities: `parent` (direct parent), `grouping` (broader grouping, e.g. course, programme), `category` (tags such as a profile ID or cmi5 category), `other`. A single object may be sent; LRSs return arrays. |
| `revision` | String | Revision of the learning activity. Only when object is an Activity. |
| `platform` | String | Platform used. Only when object is an Activity. |
| `language` | RFC 5646 tag | Language in which the experience occurred. |
| `statement` | StatementRef | Another statement that provides context. |
| `extensions` | Map | Domain context (e.g. scenario ID, range environment, session ID). |
| `contextAgents` | Array | **[2.0] new.** Each entry: `{ "objectType": "contextAgent", "agent": Agent, "relevantTypes": [IRI, ...] }`. |
| `contextGroups` | Array | **[2.0] new.** Each entry: `{ "objectType": "contextGroup", "group": Group, "relevantTypes": [IRI, ...] }`. |

`relevantTypes` IRIs classify the relationship (instructor, observer, opposing force, white cell, peer). The relationship is scoped to that single statement. Define your relevant-type IRIs in your xAPI Profile.

### 5.7 Complete example (1.0.3-compatible)

Tag: XAPI.STATEMENT.EXAMPLE

```json
{
  "id": "6b8e1f0c-3a52-4d8e-9f0a-1c2d3e4f5a6b",
  "actor": {"objectType": "Agent", "account": {"homePage": "https://id.example.org", "name": "a7f3c2e9-5d1b-4a0e-9c1f-2b8e6d4a1c07"}},
  "verb": {"id": "http://adlnet.gov/expapi/verbs/answered", "display": {"en-CA": "answered", "fr-CA": "a répondu"}},
  "object": {
    "objectType": "Activity",
    "id": "https://content.example.org/xapi/activities/net-defence-101/q/07",
    "definition": {
      "type": "http://adlnet.gov/expapi/activities/cmi.interaction",
      "name": {"en-CA": "Q7: Identify the lateral movement indicator"},
      "interactionType": "choice",
      "correctResponsesPattern": ["b"],
      "choices": [
        {"id": "a", "description": {"en-CA": "Single failed logon"}},
        {"id": "b", "description": {"en-CA": "Service creation on a remote host by a user account"}},
        {"id": "c", "description": {"en-CA": "DNS query to a CDN"}}
      ]
    }
  },
  "result": {"success": true, "response": "b", "duration": "PT42S"},
  "context": {
    "registration": "0e5a7b3c-1d2f-4a6b-8c9d-0e1f2a3b4c5d",
    "contextActivities": {
      "parent":   [{"id": "https://content.example.org/xapi/activities/net-defence-101/quiz-2"}],
      "grouping": [{"id": "https://content.example.org/xapi/activities/net-defence-101"}]
    },
    "language": "en-CA",
    "platform": "Example Authoring Runtime"
  },
  "timestamp": "2026-09-23T14:05:12.345Z"
}
```

**[2.0] example of the new context properties** (full file in `examples/xapi-2.0-context-agents.json`):
```json
"context": {
  "registration": "0e5a7b3c-1d2f-4a6b-8c9d-0e1f2a3b4c5d",
  "contextAgents": [
    {"objectType": "contextAgent",
     "agent": {"objectType": "Agent", "account": {"homePage": "https://id.example.org", "name": "instr-0042"}},
     "relevantTypes": ["https://vocab.example.org/xapi/relevant-types/instructor"]}
  ],
  "contextGroups": [
    {"objectType": "contextGroup",
     "group": {"objectType": "Group", "account": {"homePage": "https://id.example.org", "name": "team-blue-3"}},
     "relevantTypes": ["https://vocab.example.org/xapi/relevant-types/defending-team"]}
  ]
}
```
A 1.0.3-only LRS will reject this (unknown properties). Send it only with `X-Experience-API-Version: 2.0.0` to an LRS that supports 2.0. (Tested: SQL LRS accepts it with the 2.0.0 header and returns `400 Invalid Statement Data` for the same statement sent with a 1.0.3 header.)

### 5.8 Attachments and signed statements

Tag: XAPI.STATEMENT.ATTACHMENTS

Attachment object: `usageType` (IRI, required), `display` (language map, required), `description` (language map), `contentType` (MIME, required), `length` (bytes, required), `sha2` (SHA-2 hash of the content, required), `fileUrl` (IRL, optional; if the content is not sent inline).

- Inline attachments are sent as `multipart/mixed`: first part is the statement JSON, each following part carries the raw bytes with headers `Content-Type`, `Content-Transfer-Encoding: binary` and `X-Experience-API-Hash: <sha2>`.
- **[2.0]** An LRS SHALL accept `multipart/mixed` batches that contain no attachments, and batches whose attachments all use `fileUrl`.
- **Signed statements**: a JWS over the statement, carried as an attachment with `usageType` `http://adlnet.gov/expapi/attachments/signature` and `contentType` `application/octet-stream`. RS256, RS384 or RS512. **[2.0]** JWS Compact Serialization SHALL be used.
- Attachments are not part of the statement's identity; an LRS may omit them on retrieval unless `attachments=true` is requested.
- Keep large artefacts (packet captures, recordings) outside the LRS and reference them with `fileUrl` or an extension. LRSs are not artefact stores.

### 5.9 Special data types

Tag: XAPI.DATATYPES

| Type | Rule |
|---|---|
| IRI | Fully qualified, RFC 3987. **[2.0]** LRS compares using simple string comparison and syntax-based normalization only. Case and trailing slashes matter; pick one form and never vary it. |
| UUID | Standard hyphenated string form; random (version 4) in practice. |
| Timestamp | RFC 3339 profile of ISO 8601. **[2.0]** Must be RFC 3339; LRS converts to UTC rather than rejecting non-UTC input; LRS must not reject future timestamps within an (unspecified) margin. cmi5 requires UTC. Keep at least millisecond precision. |
| Duration | ISO 8601 duration. 0.01 s comparison precision. |
| Language map | Keys are RFC 5646 tags; use `und` when the language is undetermined. **[2.0]** When `format=canonical`, the LRS returns one language per map. |
| Extensions | Object whose keys are IRIs and values any JSON. Allowed in activity `definition`, `context`, `result`. |

### 5.10 Immutability and voiding

Tag: XAPI.STATEMENT.VOIDING

- Statements cannot be changed. Changes to an activity definition or a verb display elsewhere do not change the statement.
- To retract, send a new statement: verb `http://adlnet.gov/expapi/verbs/voided`, object `{ "objectType": "StatementRef", "id": "<target id>" }`.
- A voiding statement cannot target another voiding statement. Voided statements drop out of normal queries and can only be fetched by `voidedStatementId`.
- **[2.0]** The LRS SHALL NOT reject a voiding statement because it cannot find the target.
- Restrict who may void. cmi5 forbids giving AUs credentials that allow voiding.

## 6. xAPI REST API (LRS resources)

Tag: XAPI.API

All resources hang off a base endpoint, e.g. `https://lrs.example.org/xapi/`.

### 6.1 Headers, versioning and content types

Tag: XAPI.API.HEADERS

| Header | Direction | Rule |
|---|---|---|
| `X-Experience-API-Version` | Request and response | Required on every request and response. `1.0.3` for 1.0.3 clients, `2.0.0` for 2.0. An LRS may support several; the header selects behaviour. |
| `Authorization` | Request | Scheme defined by the deployment (see 6.6). cmi5 AUs send `Basic <auth-token>` exactly as returned by the fetch URL. |
| `Content-Type` | Request | `application/json`, or `multipart/mixed` for attachments. Documents may be any type. |
| `X-Experience-API-Consistent-Through` | Response to GET /statements | Time up to which the LRS guarantees all statements are queryable. Use it to set safe `since` watermarks for incremental pulls. |
| `ETag`, `If-Match`, `If-None-Match` | Documents | Optimistic concurrency (6.3). |
| `Last-Modified` | Response | **[2.0]** Required on single-document GETs and on statement GETs (max `stored` of returned statements). |

The LRS must support `HEAD` on every GET-able resource. **[2.0]** The 1.0.3 "Alternate Request Syntax" (tunnelling any method through a form-encoded POST for IE8/IE9 cross-domain limits) is removed. Clients must use normal HTTP methods and CORS.

### 6.2 Statement Resource: `/statements`

Tag: XAPI.API.STATEMENTS

| Method | Use | Success | Notes |
|---|---|---|---|
| `PUT ?statementId=<uuid>` | Store one statement with a known id | `204 No Content` | Same id + same content: `204`. Same id + different content: `409 Conflict`. |
| `POST` | Store one statement or an array (batch) | `200 OK` with array of ids | Batches are atomic: one invalid statement rejects the batch. **[2.0]** Duplicate ids inside a batch SHALL be rejected with `400`. |
| `GET ?statementId=` / `?voidedStatementId=` | Fetch one | `200` with the statement | |
| `GET` with filters | Query | `200` with `StatementResult` `{ "statements": [...], "more": "<relative IRL or empty>" }` | Follow `more` until empty. |

Query parameters: `agent` (JSON Agent or identified Group), `verb` (IRI), `activity` (IRI), `registration` (UUID), `related_activities` (bool: also match context activities and SubStatement objects), `related_agents` (bool: also match authority, instructor, team and SubStatement actors/objects; confirm how your 2.0 LRS treats `contextAgents`/`contextGroups`), `since` / `until` (compared to `stored`), `limit` (0 = server maximum), `format` (`ids`, `exact`, `canonical`), `attachments` (bool; response becomes `multipart/mixed`), `ascending` (bool).

`format=ids` returns only identifiers (**[2.0]** verb `display` SHALL be omitted); `exact` returns what was stored; `canonical` substitutes the LRS's canonical activity definitions and language maps.

The statement query API is designed for synchronisation and targeted retrieval, not for analytics. Aggregate in a warehouse (Section 13.8).

### 6.3 Document resources and concurrency

Tag: XAPI.API.DOCUMENTS

| Resource | Path | Key parameters | Typical use |
|---|---|---|---|
| State | `/activities/state` | `activityId`, `agent`, `registration` (optional), `stateId` (or `since` to list ids) | Per-learner, per-activity, per-registration scratch data: bookmarks, suspend data, cmi5 `LMS.LaunchData`. |
| Activity Profile | `/activities/profile` | `activityId`, `profileId` (or `since`) | Data about an activity shared across learners (e.g. leaderboard, config). |
| Agent Profile | `/agents/profile` | `agent`, `profileId` (or `since`) | Data about a learner across activities (preferences; cmi5 `cmi5LearnerPreferences`). |

Methods: `PUT` replaces; `POST` merges when both the stored and posted documents are JSON objects (top-level properties are overwritten or added; otherwise `400`); `GET` returns the document with `ETag`; `DELETE` removes (on State, omitting `stateId` deletes all states for that activity/agent/registration).

Concurrency:
- **[1.0.3]** Clients MUST send `If-Match` (with the current ETag) or `If-None-Match: *` on `PUT` to Agent Profile and Activity Profile. A `PUT` without either header to an existing document returns `409 Conflict`. Failed preconditions return `412 Precondition Failed`. POST/DELETE handling was SHOULD*. State documents were not covered.
- **[2.0]** Concurrency headers become mandatory on POST and PUT to the profile resources and on DELETE to State, Agent Profile and Activity Profile; the LRS SHALL honour `If-Match` on PUT/POST/DELETE and SHALL return `412` and make no change on a failed precondition. Clients SHALL use the LRS-provided ETag; the 1.0.3 SHA-1 ETag algorithm is gone, so never compute ETags yourself.

Safe pattern: `GET` (read ETag) -> modify -> `PUT` with `If-Match: "<etag>"`; create with `If-None-Match: *`; on `412`, re-read and retry.

Observed behaviour (SQL LRS v0.9.8, tested 2026-09-23): a `PUT` to an **existing State** document without `If-Match`/`If-None-Match` returned `409` even with a `1.0.3` header, although 1.0.3 only required the headers on the profile resources. Send concurrency headers on every document write, State included; it costs nothing and is forward-compatible with 2.0.

### 6.4 Agents, Activities and About

Tag: XAPI.API.OTHER

- `GET /agents?agent=<json>` returns a **Person** object: `{ "objectType": "Person", "name": [], "mbox": [], "mbox_sha1sum": [], "openid": [], "account": [] }`, combining identifiers the LRS believes belong to the same person.
- `GET /activities?activityId=<iri>` returns the LRS's canonical Activity. **[2.0]** The LRS SHALL return an Activity object (at least `id`) even if it has no stored definition.
- `GET /about` returns `{ "version": ["1.0.3", "2.0.0", ...], "extensions": {...} }`. Use it for health checks and version negotiation.

### 6.5 Error codes

Tag: XAPI.API.ERRORS

| Code | Meaning in xAPI |
|---|---|
| 400 Bad Request | Invalid JSON, schema violation, unknown property **[2.0]**, bad parameter, duplicate id in batch **[2.0]** |
| 401 Unauthorized | Missing/invalid credentials |
| 403 Forbidden | Authenticated but not permitted (cmi5: LMS may refuse learner-preference writes with 403; AU must not treat it as an error) |
| 404 Not Found | Document or statement id not found |
| 409 Conflict | Statement id exists with different content; document PUT without concurrency header **[1.0.3]** |
| 412 Precondition Failed | ETag mismatch |
| 413 Request Entity Too Large | Size limit exceeded |
| 429 Too Many Requests | Rate limit; back off and retry |
| 500 Internal Server Error | LRS fault; retry with the same statement ids |

### 6.6 Authentication and security

Tag: XAPI.API.AUTH

- **[1.0.3]** The spec described OAuth 1.0 (with scopes `statements/write`, `statements/read/mine`, `statements/read`, `state`, `define`, `profile`, `all/read`, `all`; default `statements/write` + `statements/read/mine`) and HTTP Basic. In practice almost every LRS uses HTTP Basic with a key/secret pair per client.
- **[2.0]** Authentication and security details were removed from the base standard as deployment-specific. IEEE P9274.4.2 (recommended practice for xAPI cybersecurity) is the intended home. Consequence: "xAPI 2.0 conformant" says nothing about how clients authenticate. Specify it.
- **cmi5** adds a per-session, one-time-fetch authorization token for browser content (Section 9.5), so no long-lived secret ever ships inside a content package.
- Server-to-server patterns in current products include per-client Basic credentials with scopes, and OAuth 2.0/OIDC bearer tokens (e.g. SQL LRS supports OIDC).

## 7. xAPI 2.0 (IEEE 9274.1.1-2023) versus 1.0.3

Tag: XAPI.VERSION.CHANGES

Source: ADL/USALearning technical report (July 2021) cataloguing changes from 1.0.3 to the then-final draft of 9274.1.1, cross-checked with vendor migration notes. Verify against the published standard before contracting (Section 19).

**Headline.** 2.0 is mostly 1.0.3 with the "SHOULD*" best practices hardened into requirements, two new context properties, authentication removed, and the IE-era alternate request syntax removed. Well-behaved 1.0.3 clients need little change beyond the version header.

| ID (report) | Change | LRS impact | Client impact |
|---|---|---|---|
| g1 | Reorganized into separate books: Overview, LRS requirements, Content (LRP/LRC) requirements | None | None |
| g2 | Requirement keyword MUST becomes SHALL (same force) | None | None |
| g4 | Version header value `2.0.0` | Minimal | Minimal: send `X-Experience-API-Version: 2.0.0` |
| s1-s4 | Additional (undefined) properties: LRP SHALL NOT add; LRS SHALL reject (except in extensions) | Minimal | Minimal |
| s5 | `format=ids` omits verb display | Minimal | Request `exact`/`canonical` if you need display |
| s7-s8 | Canonical language maps: LRS may keep canonical versions; returns one language per map for `canonical` | Minimal | Expect one language |
| s9-s10 | No limits on response-pattern length or `correctResponsesPattern` array length | Minimal | None |
| s11 | LRS SHALL set `timestamp` = `stored` when absent | Minimal | None |
| s12 | LRS SHALL NOT reject future timestamps (within an unspecified margin) | Minimal | None |
| s13-s14 | JWS Compact Serialization SHALL be used for signatures | Minimal | Minimal |
| s15 | LRPs defining IRIs should use IRIs they control | None | Minimal |
| s17-s18 | IRI comparison limited to RFC 3987 simple string + syntax-based normalization | Minimal | None |
| s19-s22 | Timestamps SHALL be RFC 3339; formatted to UTC; LRS converts non-UTC to UTC instead of rejecting; LRS no longer echoes original offsets | Minimal | Expect UTC back |
| s23-s25 | Durations: LRS SHALL accept >0.01 s precision (may truncate); comparisons ignore beyond 0.01 s | Minimal | Minimal |
| s26-s33 | **Alternate Request Syntax removed** | Minimal | **Major** only for clients that relied on it |
| s34-s35 | LRS SHALL accept `multipart/mixed` batches without attachments or with only `fileUrl` attachments | Minimal | None |
| s36-s37 | Batch with duplicate statement ids SHALL be rejected (400) | Minimal | Ensure unique ids per batch |
| s38-s42 | `Last-Modified` required (statements: max `stored`; each single document) | Minimal | None |
| s43 | `/activities` SHALL return an Activity object even when unknown | Minimal | Minimal |
| s44-s54 | Concurrency hardened (see 6.3); SHA-1 ETag algorithm removed; clients SHALL use LRS ETags; 412 on failed precondition | Minimal | Minimal |
| s55 | Content-type mismatch rule (tied to alternate syntax) removed | None | None |
| s56 | Conformance-testing configurability folded into error-handling requirements | None | None |
| s57, c1 | Basic-auth header details and the whole authentication section removed from the standard | None | None (but deployments must now specify auth) |
| s58 | LRS SHALL NOT reject a voiding statement whose target it cannot find | Minimal | None |
| x1 | **`contextAgents`** added; `context.instructor` deprecated | **Major** | Optional adoption |
| x2 | **`contextGroups`** added; `context.team` deprecated | **Major** | Optional adoption |

Other differences noted by the cmi5 working group: statement signature handling differs between 1.0.x and 2.0; 1.0.x statements generally work against a 2.0 LRS except for the version header; 2.0 statements that use new properties will be rejected by strict 1.0.3 LRSs (the 1.0.3 conformance suite rejects extra properties such as `contextAgents`).

**Migration posture (recommended).**
1. LRS: accept both `1.0.3` and `2.0.0` headers, pass the ADL conformance suite in both modes, and store statements with the version they arrived with.
2. Content: stay on 1.0.3 for cmi5 until the IEEE cmi5 standard defines 2.0 launch behaviour.
3. Non-LMS producers (simulators, ranges, apps) that need multiple contextual actors: use 2.0 with `contextAgents`/`contextGroups` against a 2.0 LRS, or use extensions under 1.0.3 and plan a mapping.
4. Downstream consumers: treat `instructor`/`team` and `contextAgents`/`contextGroups` as two encodings of the same relationship.

## 8. xAPI Profiles

Tag: XAPI.PROFILES

**Purpose.** A profile is the human- and machine-readable contract for one domain: which verbs, activity types, extensions and attachment usage types to use, what each statement must look like (Statement Templates), and in what order statements occur (Patterns). Without a profile, xAPI data from different producers is syntactically valid and semantically incompatible. cmi5 is an xAPI profile with additional runtime rules that JSON-LD cannot express.

**Format.** JSON-LD 1.1 (valid as plain JSON if you follow the authoring rules). Uses SKOS for concept relationships and PROV for versioning. Specification 1.0 is split into three documents: About, Structure, Communication.

**Top-level profile properties.** `id` (IRI), `@context` (`https://w3id.org/xapi/profiles/context`), `type: "Profile"`, `conformsTo` (`https://w3id.org/xapi/profiles#1.0`), `prefLabel`, `definition`, `seeAlso`, `versions` (each with `id`, `wasRevisionOf`, `generatedAtTime`), `author`, `concepts`, `templates`, `patterns`.

**Concepts.**
- `Verb`, `ActivityType`, `AttachmentUsageType` (with `prefLabel`, `definition`, SKOS relations such as `broader`, `narrower`, `related`, `broadMatch`, `narrowMatch`, `relatedMatch`, `exactMatch`, and `deprecated`).
- Extensions: `ContextExtension`, `ResultExtension`, `ActivityExtension`, with `recommendedVerbs` or `recommendedActivityTypes`, and a JSON Schema (`schema` IRI or `inlineSchema`).
- Document resources: `StateResource`, `AgentProfileResource`, `ActivityProfileResource` with `contentType` and schema.
- `Activity` concepts that fix a canonical activity definition.

**Statement Templates.** Determining properties (`verb`, `objectActivityType`, `contextGroupingActivityType`, `contextParentActivityType`, `contextOtherActivityType`, `contextCategoryActivityType`, `attachmentUsageType`, `objectStatementRefTemplate`, `contextStatementRefTemplate`) decide which statements a template applies to. `rules` then constrain them: each rule has a JSONPath `location`, optional `selector`, and one or more of `presence` (`included`, `excluded`, `recommended`), `any`, `all`, `none`.

**Patterns.** Each pattern has exactly one of `sequence`, `alternates`, `optional`, `oneOrMore`, `zeroOrMore`, built from templates and other patterns. `primary: true` marks patterns that a registration's statements must follow. Pattern matching is evaluated per `registration`; the profile spec defines a `subregistration` extension for when one registration contains several pattern runs.

**Using a profile in statements.** Put the profile **version** IRI in `context.contextActivities.category`. That declares conformance: statements carrying it must match the profile's applicable templates and patterns.

**Tooling.** Validation of profiles and of statements against profiles (Yet Analytics `pan` and `persephone`), synthetic data generation from profiles (DATASIM), profile-governed forwarding (`xapipe`), and profile servers (JSON-LD with SPARQL query). See Section 11.

**Practice.**
- Write the profile before building producers; review it like an interface control document.
- Keep IRIs under a domain you control and will keep (a `w3id.org` permanent identifier is common for public profiles). IRIs are identifiers; they do not need to resolve, which matters for disconnected networks.
- Reuse ADL and community concepts via `exactMatch`/`relatedMatch` rather than cloning them.
- Profiles 1.0 predates xAPI 2.0, so it has no concept type for `contextAgents`/`contextGroups` `relevantTypes`. Until the IEEE profiles work covers them, list your relevant-type IRIs and their definitions in the profile's human-readable documentation (and, if useful, in a statement-template rule on `$.context.contextAgents[*].relevantTypes`).

## 9. cmi5

Tag: CMI5

### 9.1 What cmi5 is (and is not)

Tag: CMI5.OVERVIEW

cmi5 ("cmi5" is a name, not an acronym or version) defines interoperable runtime communication between an LMS and Assignable Units (AUs), using xAPI as the transport and data layer. Scope: launch of AUs by an LMS; the launch/runtime environment; runtime data and transport; the parts of course definition that runtime needs; course structure import/export; LMS reporting requirements.

It is not a sequencing standard (no SCORM 2004 sequencing and navigation), not an analytics standard, and not limited to browsers in principle, although the Quartz launch mechanism is defined only for web launch (Section 8.3 of the spec: other environments are not yet specified).

Core idea in one line: the LMS authenticates the learner, writes launch data to its LRS, and launches the AU with a URL carrying the LRS endpoint, a one-time token fetch URL, the actor, the registration and the AU's runtime activity ID. The AU then talks xAPI directly to the LMS's LRS.

In cmi5 the "LMS" always means an LMS with an integrated LRS; the LMS must pass as an xAPI LRS and must be able to read all data about any learner across sessions.

### 9.2 Structural model

Tag: CMI5.MODEL

- **Course**: collection of AUs and blocks, described by a course structure (XML). Has a publisher `id` (IRI), `title`, `description`.
- **Block**: grouping of AUs and nested blocks; publisher `id`, `title`, `description`, optional objective references.
- **AU**: launchable unit; publisher `id`, `title`, `description`, `url`, and launch/tracking attributes (Section 9.13).
- **Objectives**: optional course-level list, referenced by blocks and AUs via `idref`.
- **Registration**: learner's enrollment in a course (UUID). Persists through completion and review. New enrollment (retake, recurrent training) means a new registration.
- **Session**: one AU launch (`launched` to `terminated`/`abandoned`), identified by the session ID extension.
- **Publisher ID vs runtime IDs**: the LMS MUST generate a runtime activity ID for each AU (and block and course) that differs from the publisher ID, reuse it for every launch in the registration, and SHOULD reuse it across registrations. The publisher ID travels in `context.contextActivities.grouping`. This lets the same package be imported into several LMSs or course offerings without ID collisions, while still allowing reporting by publisher ID.

### 9.3 Launch sequence

Tag: CMI5.LAUNCH.SEQUENCE

```mermaid
sequenceDiagram
    participant L as Learner (browser)
    participant M as LMS (with LRS)
    participant A as AU
    Note over M: Registration exists (created at enrollment; NotApplicable AUs evaluated)
    L->>M: Select AU
    M->>M: Abandon any active session in this registration (issue "abandoned")
    M->>M: Generate session ID; PUT State "LMS.LaunchData" (contextTemplate, launchMode, moveOn, masteryScore, launchParameters, returnURL, entitlementKey)
    M->>M: Record "launched" statement (launchmode, launchurl, moveon, masteryscore, launchparameters extensions)
    M->>A: Open AU URL ?endpoint&fetch&actor&registration&activityId
    A->>M: POST fetch URL (once)
    M-->>A: 200 {"auth-token": "..."}
    A->>M: GET State LMS.LaunchData (Authorization: Basic <token>)
    A->>M: GET Agent Profile cmi5LearnerPreferences
    A->>M: POST "initialized"
    A->>M: cmi5-allowed statements (progress, interactions, domain events)
    A->>M: "completed" / "passed" / "failed" (Normal mode only, once per registration where required)
    A->>M: POST "terminated" (with duration)
    A->>L: Redirect to returnURL (if provided)
    M->>M: Evaluate moveOn; issue "satisfied" for blocks/course as they become satisfied
```

### 9.4 Launch URL parameters

Tag: CMI5.LAUNCH.URL

Format (values URL-encoded; any order; must not collide with the AU's own query parameters):
```
<AU url>?endpoint=<LRS endpoint>&fetch=<fetch URL>&actor=<JSON Agent>&registration=<UUID>&activityId=<IRI>
```

| Parameter | LMS obligation | AU obligation |
|---|---|---|
| `endpoint` | MUST supply the xAPI base endpoint | MUST use it as the base for all xAPI requests |
| `fetch` | MUST supply a fetch URL | MUST POST to it to obtain the token (Section 9.5) |
| `actor` | MUST supply an Agent with an `account` IFI for the authenticated learner; SHOULD avoid sensitive PII | MUST use it as `actor` and as the `agent` parameter |
| `registration` | MUST supply the learner's registration for the course | MUST use it wherever a registration is required |
| `activityId` | MUST generate a unique runtime ID (not the publisher ID); same ID for all launches of that AU in the registration | MUST use it as `object.id` in all cmi5-defined statements |

`launchMethod` in the course structure: `OwnWindow` (new window or redirect of the current window) or `AnyWindow` (default; LMS chooses, frames allowed).

### 9.5 Authorization token fetch

Tag: CMI5.LAUNCH.FETCH

- The AU makes an HTTP **POST** (GET is not allowed, to defeat caching) to the fetch URL.
- Response: HTTP `200`, `Content-Type: application/json`, body `{"auth-token": "<token>"}`. Errors also return HTTP 200 with a JSON error body.
- The AU places the token in the `Authorization` header as HTTP Basic credentials (the token is already the Basic credential string) on every xAPI request: `Authorization: Basic <auth-token>`.
- The token is limited to the session. The fetch URL is one-time: it MUST NOT return a token twice; a second call SHOULD return an error.

| `error-code` | Meaning |
|---|---|
| `"1"` | Already in use or expired (token already returned, or session expired) |
| `"2"` | General security error (invalid token etc.) |
| `"3"` | General application error |

Example error body: `{"error-code": "1", "error-text": "The authorization token has already been returned."}`

Implementation note: because the fetch URL is one-time, an AU that reloads (browser refresh) loses its ability to authenticate. Cache the token in `sessionStorage` for the life of the session, or design the LMS to tolerate a refresh within the session policy.

### 9.6 LMS.LaunchData (State document)

Tag: CMI5.STATE.LAUNCHDATA

Before launch the LMS creates or updates a JSON State document with `stateId=LMS.LaunchData`, `activityId`=runtime AU ID, `agent`=launch actor, `registration`=launch registration. The AU MUST read it and MUST NOT modify or delete it.

| Property | LMS | AU | Notes |
|---|---|---|---|
| `contextTemplate` | Required | Required | xAPI `context` object containing at least the session ID extension and the publisher-ID activity in `contextActivities.grouping`. The AU uses it as the base `context` of every statement; may add values, must not overwrite provided ones. |
| `launchMode` | Required | Required | `Normal`, `Browse` or `Review` (Section 9.10). |
| `launchParameters` | Required if defined in course structure (value may be overridden by LMS) | SHOULD read | Opaque string for the AU. |
| `masteryScore` | Required if defined (may be overridden) | Required if present | Decimal 0-1, up to 4 decimal places. Drives passed/failed. |
| `moveOn` | Required | May read | Value from course structure or LMS override. |
| `returnURL` | Optional | MUST redirect current window/frame on termination if present | Not URL-encoded. |
| `entitlementKey` | Required if present in course structure | SHOULD use for entitlement checks | `{ "courseStructure": "...", "alternate": "..." }` |

Example in `examples/cmi5-LMS.LaunchData.json`.

### 9.7 Learner preferences (Agent Profile)

Tag: CMI5.PROFILE.PREFERENCES

- Document `profileId=cmi5LearnerPreferences` in the Agent Profile resource, agent = launch actor.
- Content: `{"languagePreference": "en-CA,fr-CA", "audioPreference": "on"}`. `languagePreference` is a comma-separated RFC 5646 list in order of preference; `audioPreference` is `on` or `off`.
- The AU MUST retrieve it at startup, SHOULD honour language order, MUST set audio on/off accordingly (or its own default if absent).
- Both LMS and AU may write it; the LMS may refuse AU writes with `403`, which the AU must not treat as an error.
- For bilingual audiences this is the standard way for an LMS to tell content which official language to present first.

### 9.8 Statement categories

Tag: CMI5.STATEMENTS.TYPES

| Category | Definition | Rules |
|---|---|---|
| cmi5 defined | Uses a cmi5 verb, the cmi5 category activity `https://w3id.org/xapi/cmi5/context/categories/cmi5`, and the context template | All cmi5 usage, ordering and result rules apply; drives satisfaction |
| cmi5 allowed | Any verb, with the context template, **without** the cmi5 category activity | Must occur between `initialized` and `terminated`; ignored by cmi5 session/satisfaction logic; the LMS must record and report them |
| cmi5 not-allowed | Anything not conforming | LMS SHOULD reject; if accepted, LMS MUST void |

### 9.9 Verbs

Tag: CMI5.VERBS

| Verb | IRI | Issued by | Key rules |
|---|---|---|---|
| launched | `http://adlnet.gov/expapi/verbs/launched` | LMS | Recorded before launching the AU; once per session. Context extensions: `launchmode`, `launchurl`, `moveon`, plus `launchparameters` and `masteryscore` when present in launch data. |
| initialized | `http://adlnet.gov/expapi/verbs/initialized` | AU | First statement of any kind in the session; once per session; should follow `launched` within a reasonable time. |
| completed | `http://adlnet.gov/expapi/verbs/completed` | AU | Learner experienced all relevant material; at most once per registration; `result.completion: true`; `result.duration` required. Signifies progress 100%. |
| passed | `http://adlnet.gov/expapi/verbs/passed` | AU | Attempted and succeeded in the judged activity; at most once per registration; `result.success: true`; if a scaled score is present it must be >= masteryScore; `duration` required; include `masteryscore` extension when judged against it. |
| failed | `http://adlnet.gov/expapi/verbs/failed` | AU | Attempted and failed; `result.success: false`; scaled score (if present) < masteryScore; `duration` required; must not follow a `passed` in the registration; may recur in later sessions. |
| abandoned | `https://w3id.org/xapi/adl/verbs/abandoned` | LMS | Session ended abnormally (no `terminated`); issued e.g. when a new launch occurs in the same registration while a session is active; at most one per session; `duration` required; no statements accepted for the session afterwards. |
| waived | `https://w3id.org/xapi/adl/verbs/waived` | LMS | AU requirements met by other means; `result.success: true`, `result.completion: true`, `reason` extension required; own unique session ID; once per AU per registration. |
| terminated | `http://adlnet.gov/expapi/verbs/terminated` | AU | Last statement of any kind in the session; `duration` required (normally time since `initialized`). LMS rejects statements for the session after an LMS-defined grace period. |
| satisfied | `https://w3id.org/xapi/adl/verbs/satisfied` | LMS | Learner met moveOn for all AUs in a **block** or the **course**; object is the block or course runtime activity with activity type `https://w3id.org/xapi/cmi5/activitytype/block` or `https://w3id.org/xapi/cmi5/activitytype/course` (the spec wording says "objectType ... in the Object's Definition"; in xAPI terms that is `object.definition.type`); uses the triggering AU session ID, or a new one if triggered outside a session; should be issued once per block/course per registration. |

Waived `reason` values (SHOULD): `Tested Out`, `Equivalent AU`, `Equivalent Outside Activity`, `Administrative`.

### 9.10 Ordering rules and launch modes

Tag: CMI5.RULES.ORDERING

Order is determined by `timestamp` (so timestamps are mandatory and must be UTC).

Within an AU session:
- No cmi5-defined verb is duplicated.
- At most one of `passed` / `failed`.
- `initialized` is the first statement (defined or allowed); `terminated` is the last.

Within a registration (per AU):
- Zero or one `completed`.
- Zero or one `passed`.
- No `failed` after a `passed`.

LMS: may issue several `satisfied` in a session; should not repeat `satisfied` for the same block/course in a registration; at most one `abandoned` per session; at most one `waived` per session and per AU per registration.

**launchMode**
| Mode | AU must send | AU must not send | Intended UX |
|---|---|---|---|
| Normal | `initialized`, `terminated` | nothing extra forbidden; may send completed/passed/failed per rules | Tracked attempt |
| Browse | `initialized`, `terminated` | `completed`, `passed`, `failed` (any other cmi5-defined statement) | Look around without judgement |
| Review | `initialized`, `terminated` | `completed`, `passed`, `failed` | Revisit completed material |

### 9.11 Result rules matrix

Tag: CMI5.RULES.RESULT

| Verb | `score` | `success` | `completion` | `duration` |
|---|---|---|---|---|
| launched | Not allowed | Not allowed | Not allowed | Optional |
| initialized | Not allowed | Not allowed | Not allowed | Optional |
| completed | Not allowed | Not allowed | **true** | **Required** |
| passed | Optional | **true** | Not allowed | **Required** |
| failed | Optional | **false** | Not allowed | **Required** |
| terminated | Not allowed | Not allowed | Not allowed | **Required** |
| abandoned | Not allowed | Not allowed | Not allowed | **Required** (LMS computes) |
| waived | Not allowed | **true** | **true** | Optional |
| satisfied | Not allowed | Not allowed | Not allowed | Optional |

Score detail: `scaled` 0 to 1 in cmi5; if `raw` is given, `min` and `max` are required. Progress: result extension `https://w3id.org/xapi/cmi5/result/extensions/progress` (integer 0-100) may be used in statements before completion; not in `completed` or after it.

### 9.12 Context rules

Tag: CMI5.RULES.CONTEXT

- `context.registration`: the LMS-provided registration.
- `contextActivities.category`: `https://w3id.org/xapi/cmi5/context/categories/cmi5` on every cmi5-defined statement. Add `https://w3id.org/xapi/cmi5/context/categories/moveon` on cmi5-defined statements whose result includes `success` or `completion` (passed, failed, completed, waived) and on no others.
- `contextActivities.grouping`: the unaltered AU publisher ID, supplied through the context template; the LMS includes it on its own statements too.
- Context extensions:

| Extension IRI | Set by | Use |
|---|---|---|
| `https://w3id.org/xapi/cmi5/context/extensions/sessionid` | LMS generates; AU and LMS include on all defined and allowed statements | Session correlation |
| `https://w3id.org/xapi/cmi5/context/extensions/masteryscore` | LMS on `launched` when in launch data; AU on passed/failed judged against it | Pass mark |
| `https://w3id.org/xapi/cmi5/context/extensions/launchmode` | LMS on `launched` | Normal/Browse/Review |
| `https://w3id.org/xapi/cmi5/context/extensions/launchurl` | LMS on `launched` | Launch URL without cmi5 parameters |
| `https://w3id.org/xapi/cmi5/context/extensions/moveon` | LMS on `launched` | moveOn value |
| `https://w3id.org/xapi/cmi5/context/extensions/launchparameters` | LMS on `launched` when present | Launch parameters |

Result extensions: `https://w3id.org/xapi/cmi5/result/extensions/progress` (AU, optional), `https://w3id.org/xapi/cmi5/result/extensions/reason` (LMS, required on waived).

- Actor on cmi5-defined statements: `objectType: "Agent"` with an `account` IFI.
- Statement `id`: the AU MUST assign a UUID to every statement it sends.
- `timestamp`: required on all statements, UTC; need not be unique; should reflect when the condition occurred.

### 9.13 moveOn and satisfaction

Tag: CMI5.MOVEON

| moveOn | AU is satisfied when |
|---|---|
| `Passed` | a `passed` statement exists in the registration |
| `Completed` | a `completed` statement exists |
| `CompletedAndPassed` | both exist |
| `CompletedOrPassed` | either exists |
| `NotApplicable` (default) | immediately; the LMS evaluates this at registration time and may issue `satisfied` for blocks/course then |

A `waived` AU counts as having met its moveOn. When every AU in a block has met moveOn, the LMS issues `satisfied` for the block; when every AU in the course has, it issues `satisfied` for the course. cmi5 defines no roll-up of scores, weights or partial credit; derive those in reporting if needed.

### 9.14 Course structure XML

Tag: CMI5.COURSESTRUCTURE

Namespace and schema: `https://w3id.org/xapi/profiles/cmi5/v1/CourseStructure.xsd` (also `v1/CourseStructure.xsd` in the spec repository). The LMS must trim leading/trailing whitespace on import, must support structures with more than 1000 AUs, and must accept anything conforming to the XSD.

Element order under `<courseStructure>`: `<course>` (1), `<objectives>` (0..1), then one or more `<au>` / `<block>` in any mix, then optional foreign-namespace extension elements.

| Element | Attributes | Children (in order) |
|---|---|---|
| `course` | `id` (IRI, required) | `title`, `description` (each one or more `<langstring lang="...">`), extension elements |
| `objectives` | | `objective` (1..n) with `id` (IRI), children `title`, `description` |
| `block` | `id` (IRI, required, unique in structure) | `title`, `description`, `objectives` (refs, optional), then `au`/`block` (1..n) |
| `au` | `id` (IRI, required); `moveOn` (`NotApplicable` default, `Passed`, `Completed`, `CompletedAndPassed`, `CompletedOrPassed`); `masteryScore` (decimal 0-1, optional); `launchMethod` (`AnyWindow` default, `OwnWindow`); `activityType` (optional) | `title`, `description`, `objectives` (`<objective idref="..."/>`, optional), `url` (required; relative to package root or absolute), `launchParameters` (optional), `entitlementKey` (optional) |

Vendor-specific metadata goes in elements and attributes from your own namespace (see `v1/examples/extended-cmi5.xml` in the spec repo). A schema-validated sample is in `examples/cmi5.xml`.

### 9.15 Course package formats

Tag: CMI5.PACKAGE

An LMS MUST import at least:
1. A standalone course structure XML file (AU URLs absolute; content hosted elsewhere).
2. A ZIP (32-bit) containing `cmi5.xml` at the root plus content.
3. A ZIP64 (64-bit) with the same layout.

The XML inside a ZIP MUST be named `cmi5.xml`. Content may live anywhere reachable by the learner's browser, which is a major practical difference from SCORM: an AU can be hosted on a content server or CDN separate from the LMS (with CORS configured on the LRS endpoint).

### 9.16 Conformance checklists

Tag: CMI5.CONFORMANCE

**LMS must:**
- Be a conformant xAPI LRS (pass the ADL LRS test suite for the xAPI version it serves) and have an account able to read all data for any learner across sessions.
- Import the course structure (XSD) and all three package formats; support more than 1000 AUs; should export and should allow editing structures.
- Generate registrations, runtime activity IDs (not publisher IDs), session IDs, one-time fetch URLs and session-scoped tokens.
- Write `LMS.LaunchData` before launch and record `launched`.
- Never give AUs credentials that can void statements.
- Reject (should) or void (must, if accepted) statements that violate cmi5 rules.
- Abandon an active session when a new launch starts in the same registration; stop accepting statements after `abandoned` and after the post-`terminated` grace period.
- Evaluate moveOn at registration and after each relevant statement; issue `satisfied` for blocks and course; issue `waived` with a reason when administratively waiving.
- Record and report all cmi5-defined and cmi5-allowed statements it accepts.

**AU must:**
- Parse launch parameters in any order; use `endpoint`, `actor`, `registration`, `activityId` exactly as given.
- POST to the fetch URL once; send the token as Basic auth on every request.
- Read `LMS.LaunchData` and `cmi5LearnerPreferences` at startup; never modify `LMS.LaunchData`.
- Send `initialized` first and `terminated` last, each once per session; use the context template on every statement; UUID ids; UTC timestamps.
- Respect `launchMode`, `masteryScore`, once-per-registration rules, and result/duration rules.
- Redirect to `returnURL` on exit when provided.

Conformance tooling: ADL CATAPULT provides the LMS Test Suite (LTS), the Content Test Suite (CTS), and a machine-readable `requirements.json` mapping requirement IDs (e.g. `9.3.0.0-1`) to spec text.

### 9.17 cmi5 and xAPI 2.0

Tag: CMI5.XAPI2

- Quartz references xAPI 1.0.3; cmi5 AUs send `X-Experience-API-Version: 1.0.3`. CATAPULT does not check xAPI versions.
- Against a dual-version LRS, Quartz content works unchanged. Against a 2.0-only LRS it fails on the version header.
- Working-group notes (2023) record the approach: keep the Quartz profile version-agnostic where possible, address 1.0.x vs 2.0 in the IEEE cmi5 standard (P9274.3.1) and in best-practice guidance, and aim for broad backward compatibility. An LMS mixing versions may need to transform AU statements.
- Rustici has stated its products will not accept xAPI 2.0 traffic from legacy "Tin Can" packages; cmi5 is the supported launch path going forward.

## 10. SCORM to cmi5 mapping

Tag: MIGRATION.SCORM

| SCORM concept | cmi5 / xAPI equivalent | Notes |
|---|---|---|
| `imsmanifest.xml` | `cmi5.xml` course structure | Much simpler; no resource/dependency graph. |
| SCO | AU | |
| Organization item / aggregation | Block | No sequencing rules attached. |
| JavaScript API adapter found by walking frames (`LMSInitialize` / `Initialize`) | REST calls to `endpoint` with the fetch-URL token; `initialized` statement | Removes the same-origin/frame constraint; content can be hosted anywhere. |
| `LMSFinish` / `Terminate` | `terminated` statement | Duration required. |
| `cmi.core.lesson_status` / `cmi.completion_status` + `cmi.success_status` | `completed`, `passed`, `failed` statements | One-time per registration (completed, passed). |
| `cmi.core.score.*` / `cmi.score.scaled` | `result.score` on `passed`/`failed` | Scaled 0-1 in cmi5. |
| Mastery score (`adlcp:masteryscore`, `cmi.scaled_passing_score`) | `masteryScore` attribute, delivered in `LMS.LaunchData` | LMS may override. |
| `cmi.launch_data` / `adlcp:dataFromLMS` | `launchParameters` | |
| `cmi.core.lesson_mode` (browse/normal/review) | `launchMode` | |
| `cmi.suspend_data`, `cmi.core.lesson_location`, `cmi.entry` | AU-managed State documents (AU-chosen `stateId`s under the same activity/agent/registration) | Not defined by cmi5; AU owns it. Never write to `LMS.LaunchData`. |
| `cmi.core.session_time` / `cmi.session_time` | `result.duration` on `terminated` | |
| `cmi.interactions.n.*` | cmi5-allowed statements, typically verb `answered` with `cmi.interaction` activity definitions | Far richer and queryable. |
| `cmi.student_preference.*` (audio, language) | `cmi5LearnerPreferences` Agent Profile | |
| `cmi.core.student_id` | `actor.account` | |
| SCORM 2004 Sequencing and Navigation | None | Only moveOn + blocks + `satisfied`. Adaptive sequencing belongs in the LMS or an external engine. |
| "Tin Can package" (`tincan.xml`) | cmi5 package | Tin Can launch was never standardized; migrate to cmi5. |

**Migration routes.** (1) Re-publish from an authoring tool that exports cmi5. (2) Wrap existing SCORM content with a SCORM-to-cmi5 bridge; ADL CATAPULT published course templates for this, and commercial players offer conversion. (3) Leave legacy SCORM in place and run it through a player that emits xAPI into the same LRS, accepting the shallower data.

## 11. Ecosystem and tooling

Tag: ECOSYSTEM.TOOLS

### 11.1 Reference implementations and conformance

| Tool | What it is | Use it for |
|---|---|---|
| ADL LRS (`adlnet/ADL_LRS`) | Reference LRS (Python/Django). 2.0 support added 2023 (contextAgents/contextGroups, concurrency updates); 1.0.3 snapshot kept on a separate branch. | Reference behaviour when LRSs disagree; not positioned as a production product. |
| ADL LRS Conformance Test Suite (`adlnet/lrs-conformance-test-suite`) | Node.js test runner. Release v2.0.0.0 added IEEE 9274.1.1 tests; 1.0.3 tests unchanged. Options: `-e` endpoint, `-x` xAPI version, `-a` Basic auth with `-u`/`-p`, `-o` OAuth 1, `-g` grep, `-b` bail, `-z` failures only. | Acceptance testing of any LRS. |
| ADL CATAPULT (`adlnet/CATAPULT`) | cmi5 **player** prototype (a launch service to integrate with an LMS, backed by an external LRS), **CTS** (content test suite with web UI), **LTS** (LMS test suite, automatable in CI), `requirements/requirements.json` (305 requirement IDs mapped to spec text), course examples and templates. Apache-2.0. | cmi5 conformance for content and LMS; a working cmi5 launch environment for labs. |
| cmi5 spec repo (`AICC/CMI-5_Spec_Current`, branch `quartz`) | Spec text, `v1/CourseStructure.xsd`, sample course structures; wiki with working-group minutes and conformance requirement drafts. | Authoritative cmi5 text. |
| xAPI spec repo (`adlnet/xAPI-Spec`) | xAPI 1.0.3 text (About, Data, Communication). | Authoritative 1.0.3 text. |
| IEEE open source (`opensource.ieee.org/xapi`, `opensource.ieee.org/xapi-cmi5/9274.3.1`) | 9274.1.1 base-standard documentation and examples; cmi5 IEEE working repo. | 2.0 text and the cmi5 standard in progress. |
| xAPI Profiles repo (`adlnet/xapi-profiles`) | Profiles spec 1.0 (About, Structure, Communication), JSON-LD context, ontology. | Authoring and validating profiles. |

### 11.2 Learning Record Stores

Recommendation for a self-hosted evaluation or sovereign lab: **SQL LRS on PostgreSQL**. It is open source (Apache-2.0), container-ready, runs on SQLite for quick tests and Postgres for anything durable, has an admin UI, credential scopes and OIDC support, accepts both xAPI 1.0.3 and 2.0.0 by default (selected by request header, configurable with `LRSQL_SUPPORTED_VERSIONS`, with an optional strict mode that downgrades 2.0.0 statements when read with a 1.0.3 header), and its maker also publishes profile validation and data-simulation tools. Keep the ADL LRS available as a behavioural reference. For production at scale or with cmi5 launching built in, evaluate commercial platforms against conformance evidence rather than feature lists.

| LRS | Type | Stack / storage | Notes |
|---|---|---|---|
| SQL LRS (Yet Analytics) | Open source, Apache-2.0 | Clojure/JVM; SQLite or PostgreSQL | Docker image `yetanalytics/lrsql`; admin UI; API keys with scopes; OIDC; 1.0.3 and 2.0.0 both enabled by default (`LRSQL_SUPPORTED_VERSIONS`), optional strict downgrade (`LRSQL_ENABLE_STRICT_VERSION`); CORS via `LRSQL_ALLOW_ALL_ORIGINS` / `LRSQL_ALLOWED_ORIGINS`. **Recommended for labs.** |
| ADL LRS | Open source, reference | Python/Django | Reference implementation; 2.0 support since 2023. |
| Ralph (France Université Numérique) | Open source | Python/FastAPI; pluggable backends (e.g. Elasticsearch, MongoDB, ClickHouse) | LRS plus CLI and library for learning-analytics pipelines. |
| TRAX LRS | Open source (Starter Edition GPL-3.0) and commercial | PHP/Laravel; MySQL/MariaDB/PostgreSQL | Lean, core-LRS focus. |
| Learning Locker | Open source (historic), Learning Pool | Node.js; MongoDB | Widely deployed historically; check current maintenance status before adopting. |
| lxHive | Open source | PHP; MongoDB | Supports xAPI up to 1.0.3 only. |
| `xapi-rs` ("LaRS") | Open source, GPL-3.0 | Rust; PostgreSQL | Claims 2.0.0 conformance; young project. |
| Rustici Engine / Rustici LRS / SCORM Cloud | Commercial | | Engine 23 (Feb 2024) added xAPI 2.0 to its LRS; players for SCORM, AICC, xAPI, cmi5. |
| Veracity Learning | Commercial | | States it was the first xAPI 2.0-conformant LRS (2023); chaired the IEEE xAPI effort. |
| Watershed, Yet Analytics, Learning Pool and others | Commercial | | Analytics-oriented platforms. |

ADL maintains an adopter registry that includes a list of conformant LRSs; use it as a starting point and still demand current test output.

### 11.3 Client libraries and data tools

| Tool | Language | Purpose |
|---|---|---|
| `@rusticisoftware/cmi5` (cmi5.js) | JavaScript | AU-side cmi5 runtime library (used by CATAPULT samples). |
| TinCanJS, TinCanPHP, TinCanPython, TinCanJava | Various | General xAPI clients (Rustici). |
| xAPIWrapper | JavaScript | ADL xAPI client. |
| xAPI-Java, xAPI.js | Java, JS | Community clients. |
| `java-xapi-tools` | Java | Yet Analytics statement model and LRS client. |
| `xapi-schema` | Clojure(Script) | Statement validation. |
| DATASIM | Clojure/Docker | Generate realistic synthetic statements from an xAPI Profile and personae; load-test LRSs and dashboards. |
| `pan`, `persephone` | Clojure | Validate profiles; validate statements against profile templates and patterns. |
| `xapipe` (LRSPipe) | Clojure/Docker | Profile-governed statement forwarding between LRSs with resumable jobs. |
| Moodle `logstore_xapi` | PHP | Emits xAPI from Moodle event logs. |

Newer community libraries (for example headless clients that target both 1.0.3 and 2.0) appear regularly; vet them against the conformance suite and your profile before use.

## 12. Hands-on runbook: local LRS, smoke test, cmi5 session simulation

Tag: RUNBOOK.LAB

**Purpose.** Stand up a disposable LRS, prove the xAPI API works, then drive a complete cmi5 session through it with curl so you can inspect every artefact.

**Where to run.** Any Linux host or VM with Docker Engine + Compose v2, `curl`, `jq` and `uuidgen` (package `uuid-runtime` on Debian/Ubuntu). Ports bind to `127.0.0.1` only.

**Prerequisites.**
```bash
docker --version && docker compose version && jq --version && uuidgen
```
Expected: version strings and one UUID.

### Step 1: configure secrets

```bash
cd lab/
cp .env.example .env
# Generate values instead of typing them:
sed -i "s/^LRS_KEY=.*/LRS_KEY=$(openssl rand -hex 12)/" .env
sed -i "s/^LRS_SECRET=.*/LRS_SECRET=$(openssl rand -hex 24)/" .env
sed -i "s/^LRS_ADMIN_PASS=.*/LRS_ADMIN_PASS=$(openssl rand -hex 16)/" .env
sed -i "s/^PG_PASSWORD=.*/PG_PASSWORD=$(openssl rand -hex 16)/" .env
chmod 600 .env
```
Validation: `grep -c REPLACE_ME .env` returns `0`.

### Step 2: start SQL LRS on PostgreSQL

```bash
docker compose up -d
docker compose ps
docker compose logs -f lrs    # wait for the startup banner, then Ctrl-C
```
Expected: `db` healthy, `lrs` running. If Postgres is slow on first start and the LRS exits, `docker compose up -d` again (the compose file already sets `LRSQL_POOL_INITIALIZATION_FAIL_TIMEOUT`).

### Step 3: smoke-test the xAPI API

```bash
set -a; . ./.env; set +a
export LRS_ENDPOINT=http://127.0.0.1:8080/xapi
../scripts/lrs-smoke-test.sh
```
The script checks `/about`, POSTs a statement with a client-generated UUID, GETs it back, re-POSTs it to prove idempotency, PUTs and GETs a State document, exercises ETag concurrency on an Activity Profile document (create with `If-None-Match: *`, update with `If-Match`, then confirm a stale ETag returns `412`), and runs a filtered query. Expected: every line `PASS`, exit code 0.

Manual equivalent for the core check:
```bash
AUTH=$(printf '%s:%s' "$LRS_KEY" "$LRS_SECRET" | base64 -w0)
curl -s -H "Authorization: Basic $AUTH" -H 'X-Experience-API-Version: 1.0.3' "$LRS_ENDPOINT/about" | jq .
```
Expected: JSON with a `version` array that includes `1.0.3` and, with default SQL LRS settings, `2.0.0`.

### Step 4: simulate a full cmi5 session

```bash
../scripts/cmi5-session-sim.sh            # live against the LRS
DRY_RUN=1 ../scripts/cmi5-session-sim.sh  # print every request without sending
```
The script plays both roles: as the LMS it writes `LMS.LaunchData`, records `launched` and builds the launch URL; as the AU it reads the launch data, sends `initialized`, a cmi5-allowed progress statement, `completed`, `passed` (with mastery score) and `terminated`; as the LMS again it sends `satisfied` for the course. It then queries by registration and prints the verb sequence. Expected tail:
```
launched -> initialized -> progressed -> completed -> passed -> terminated -> satisfied
```

### Step 4b: run the real AU code (Node 18+)

```bash
LMS_ONLY=1 FETCH_URL=http://127.0.0.1:8099/fetch LMS_ONLY_OUT=/tmp/launch.env ../scripts/cmi5-session-sim.sh >/dev/null
set -a; . /tmp/launch.env; set +a
node ../au/test-au-node.mjs
```
The simulator performs only the LMS launch steps and writes the launch query to `/tmp/launch.env`. The harness serves the one-time fetch URL on `127.0.0.1:8099`, runs `au/cmi5-au.js` (the same file the browser AU uses), and checks the cmi5 rules. Expected: nine `PASS` lines, including "second fetch POST returns error-code 1", and the sequence `launched -> initialized -> progressed -> completed -> passed -> terminated`.

`au/index.html` + `au/cmi5-au.js` form a deployable AU. To try it in a real cmi5 LMS (or the CATAPULT player), package it with a `cmi5.xml` whose AU `url` is `index.html`, and allow the content origin in the LRS CORS settings (`LRS_ALLOWED_ORIGINS` in `lab/.env`).

### Step 5 (optional): conformance suite

```bash
git clone --depth 1 https://github.com/adlnet/lrs-conformance-test-suite.git
cd lrs-conformance-test-suite && npm install
node bin/console_runner.js -e "$LRS_ENDPOINT" -x 1.0.3 -a -u "$LRS_KEY" -p "$LRS_SECRET" -z
node bin/console_runner.js -e "$LRS_ENDPOINT" -x 2.0.0 -a -u "$LRS_KEY" -p "$LRS_SECRET" -z
```
Expected: a summary of passed/failed tests; `-z` limits the log to failures. Run it against a throwaway LRS; it writes many statements. Check `node bin/console_runner.js --help` for the current option set.

### Step 6 (optional): real cmi5 launching with CATAPULT

Clone `adlnet/CATAPULT`, follow `player/README.md` (Docker Compose with a MySQL container; `.env` values for host port, content URL, API key/secret, token secret and the LRS endpoint/credentials, which here would point at the SQL LRS), then use the CTS to import a package and launch it. As of 2026-09-23 the upstream `player/docker-compose.yml` passes variables named `LRS_XAPI_VERSIONs` and `DB_PASSWOD`; check those names against what the service reads before trusting the configuration.

### Step 7 (optional): pull the primary sources and rebuild the chunks

```bash
../scripts/fetch-sources.sh ../sources          # clones spec repos, builds ../sources/corpus + MANIFEST.tsv
cd .. && python3 scripts/build-chunks.py        # regenerates corpus/xapi-cmi5-chunks.jsonl
```
`fetch-sources.sh` also tries `opensource.ieee.org` for the xAPI 2.0 and IEEE cmi5 working texts (`IEEE=0` skips it); it reports anything it could not fetch and exits non-zero.

### Rollback

```bash
cd lab/
docker compose down          # stop, keep data volume
docker compose down -v       # stop and delete all LRS data (irreversible)
docker image rm yetanalytics/lrsql:latest postgres:16   # optional
```
Validation: `docker compose ps` shows nothing; with `-v`, `docker volume ls | grep lrs` shows nothing.

**Hardening before anything beyond a lab:** pin image tags or digests, put TLS in front (reverse proxy), restrict CORS origins to your content hosts, rotate the default credentials into per-client keys with least-privilege scopes, back up Postgres, and ship logs.

## 13. Design and implementation guidance

Tag: GUIDANCE.DESIGN

### 13.1 Identity and privacy

- Use `account` IFIs with an opaque, stable key. Emails change, leak PII and are forbidden as the cmi5 actor. `mbox_sha1sum` of an email is trivially reversible by dictionary and is not anonymisation.
- One `homePage` per identity authority. If people have accounts in several systems, resolve identity before statements are written, or record the mapping in the source-of-record system; do not rely on the LRS's Person object for identity resolution.
- Treat the LRS as a personal-information holding: performance, failures and timestamps about identifiable people. In a Canadian federal setting that means the Privacy Act and Treasury Board privacy policy instruments apply (privacy impact assessment, personal information bank description, retention and disposition schedule, access controls). Set retention in the LRS configuration, not by convention.
- Minimise: do not put names, service numbers or free-text learner input in statements unless there is a defined use. `actor.name` is optional; leave it out if reporting can join names from the HR system.

### 13.2 Identifiers (activities, verbs, extensions)

- Mint IRIs under a domain you control and will keep for the life of the data (for example `https://id.example.org/xapi/...`). IRIs are compared as strings: fix case, scheme and trailing-slash conventions once.
- Activity IDs identify things, not events or attempts. No timestamps, session IDs or user IDs inside activity IDs. Put attempts in `registration`, sessions in extensions.
- Versioned content: keep the activity ID stable across minor revisions and use `context.revision`; mint a new ID only when the meaning changes.
- Reuse ADL verbs and established profile concepts before inventing. Every new verb or extension goes into your profile with a definition.
- Extension values should be small, typed JSON with a JSON Schema in the profile.

### 13.3 Registrations and sessions

- cmi5 registration = learner x course enrollment. Outside cmi5, choose deliberately: one registration per exercise run, per attempt, or per enrollment. Write it down in the profile; pattern validation depends on it.
- For team events, give every participant's statements the same registration for the run, and identify the team as an identified Group.

### 13.4 Time

- Generate timestamps at the moment the event happens, in UTC, with milliseconds. cmi5 ordering rules use `timestamp`, so clock skew on client machines can break ordering checks; synchronise clocks on simulators and range hosts (NTP).
- Use `stored` (LRS time) for incremental extraction; use `timestamp` for analysis.

### 13.5 Reliability

- Generate statement UUIDs on the client, queue locally, and retry with the same IDs; the LRS treats identical re-submissions as no-ops. Back off on `429` and `5xx`.
- Batch moderately (tens to low hundreds of statements) and keep ids unique per batch.
- Disconnected or low-bandwidth sites: store-and-forward to a local LRS, then forward upstream (statement forwarding or `xapipe`). Statement IDs make de-duplication safe.

### 13.6 Security

- Never embed long-lived LRS credentials in browser content. Use cmi5's per-session token, or a backend proxy that signs requests.
- Issue per-client credentials with least-privilege scopes (write-only for producers, read-only for dashboards). Only a small admin set may void.
- TLS everywhere; restrict CORS to known content origins; set size limits on statements and attachments; rate-limit per credential.
- Validate statements at ingestion against your profile (reject or quarantine), because the LRS only enforces xAPI syntax.
- Log and audit administrative actions (credential issue, voiding, deletion, export).
- Because xAPI 2.0 left authentication out of the base standard, write the authentication scheme, token lifetime and scope model into the interface specification.

### 13.7 Classified and disconnected environments

- xAPI does not require internet access. IRIs such as `https://w3id.org/...` are identifiers, not dependencies; host profiles and vocabularies locally for human reference.
- Stand up the LRS inside the accredited boundary. Treat cross-domain forwarding as a data transfer subject to the boundary's release rules; forward only what the receiving side is cleared for (profile-governed forwarding helps filter).
- Content packages and AUs are web applications; they fall under the same software assurance process as any other web code on that network.

### 13.8 Analytics architecture

- The statement query API is for synchronisation, not analytics. Pull incrementally with `since` (use the `X-Experience-API-Consistent-Through` header as the safe watermark) and page through `more`, or use the LRS's native forwarding, then land statements in a warehouse.
- A practical warehouse model: one wide table keyed by statement id with typed columns for actor key, verb, object id and type, registration, timestamp, stored, success, completion, scaled score, duration seconds, plus the raw JSON; separate tables for context activities and extensions.
- Voided statements must be removed or flagged downstream when the voiding statement arrives.
- Compute completion, pass rates, time on task and item analysis in the warehouse; do not ask the LRS to aggregate.

## 14. Common pitfalls and fixes

Tag: TROUBLESHOOTING

| Symptom | Likely cause | Fix |
|---|---|---|
| `400` on every request from a 2.0 client | LRS accepts only 1.0.x | Send `X-Experience-API-Version: 1.0.3` or upgrade the LRS. |
| `400` on statements with `contextAgents` | 1.0.3 LRS or 1.0.3 header | Use 2.0.0 header against a 2.0 LRS, or move the data to extensions. |
| `409 Conflict` on POST | Re-sent id with different content | Never reuse a UUID for a different statement; generate a new id. |
| `409` on document PUT | Missing `If-Match`/`If-None-Match` on an existing document (profiles per spec; some LRSs also enforce it on State) | GET the ETag, then PUT with `If-Match`; create with `If-None-Match: *`. |
| `412` on document write | Stale ETag | Re-read, merge, retry. |
| cmi5 AU cannot authenticate after page refresh | Fetch URL is one-time | Keep the token in session storage for the session; LMS session policy for refresh. |
| cmi5 statements rejected or voided by LMS | Missing context template values, missing cmi5 category, wrong object id (publisher ID instead of `activityId`), duplicate `completed`/`passed`, `completed` sent in Browse/Review mode, missing `duration`, non-UTC timestamp | Rebuild context from `contextTemplate`; use `activityId` from the URL; enforce once-per-registration and launchMode logic. |
| Session shows `abandoned` | AU never sent `terminated` (window closed, crash) or learner relaunched | Send `terminated` from a visible Exit control, and on `pagehide` with `fetch(..., {keepalive: true})` (`navigator.sendBeacon` cannot set the `Authorization` header, so it is not usable against an LRS directly). Unload delivery is best effort; the LMS must handle abandonment. |
| CORS errors in browser | LRS not allowing the content origin, or missing headers in `Access-Control-Allow-Headers` (`Authorization`, `Content-Type`, `X-Experience-API-Version`, `If-Match`, `If-None-Match`) | Configure the LRS or reverse proxy CORS policy. |
| Dashboards double-count | Retries with new UUIDs, or voided statements not handled | Client-side UUIDs; process voids in ETL. |
| Same course counted as two activities | IRI variations (http vs https, trailing slash, case) | Normalise at the source; fix in the profile. |
| Learner appears as several people | Mixed IFIs (email in one system, account in another) | Single identity authority; account IFIs only. |
| Score rejected | `raw` without `min`/`max`, `scaled` outside range, cmi5 score on a verb other than passed/failed | Follow 5.5 and 9.11. |

## 15. Requirements language for statements of work

Tag: PROCUREMENT.REQUIREMENTS

Sample requirement statements (adapt to your requirement numbering and verification methods):

1. The LRS SHALL conform to xAPI 1.0.3 and to IEEE 9274.1.1-2023 (xAPI 2.0), selecting behaviour by the `X-Experience-API-Version` request header. *Verification: ADL LRS Conformance Test Suite results for both versions, run by the Crown or witnessed, against the delivered build.*
2. The LMS SHALL conform to cmi5 Quartz as an LMS. *Verification: ADL CATAPULT LMS Test Suite results.*
3. Delivered e-learning content SHALL be cmi5 course packages that pass the ADL CATAPULT Content Test Suite with no failures.
4. All xAPI statements generated by delivered systems SHALL conform to the project's published xAPI Profile(s), including statement templates and patterns. *Verification: automated validation of a representative statement sample.*
5. Actors SHALL be identified only by `account` IFIs issued by the project's identity authority; statements SHALL NOT contain email addresses, names or service numbers unless listed in the profile.
6. The LRS SHALL support statement forwarding (or equivalent export) to a Crown-designated LRS or data platform, in xAPI JSON, without proprietary transformation.
7. The contractor SHALL deliver the interface specification for LRS authentication (scheme, token lifetime, scopes, credential rotation), since IEEE 9274.1.1 does not define authentication.
8. The LRS SHALL provide per-credential scopes that separate write, read and administrative (including voiding) permissions.
9. All data SHALL be stored and processed in Canada (adjust to the applicable residency and security requirements), with backups, retention and disposition configurable per record class.
10. The contractor SHALL NOT make core completion, pass or score reporting depend on vendor-specific extensions.

## 16. IRI quick reference

Tag: REFERENCE.IRIS

**cmi5 verbs**
```
http://adlnet.gov/expapi/verbs/launched
http://adlnet.gov/expapi/verbs/initialized
http://adlnet.gov/expapi/verbs/completed
http://adlnet.gov/expapi/verbs/passed
http://adlnet.gov/expapi/verbs/failed
https://w3id.org/xapi/adl/verbs/abandoned
https://w3id.org/xapi/adl/verbs/waived
http://adlnet.gov/expapi/verbs/terminated
https://w3id.org/xapi/adl/verbs/satisfied
```
**cmi5 context and result**
```
https://w3id.org/xapi/cmi5/context/categories/cmi5
https://w3id.org/xapi/cmi5/context/categories/moveon
https://w3id.org/xapi/cmi5/context/extensions/sessionid
https://w3id.org/xapi/cmi5/context/extensions/masteryscore
https://w3id.org/xapi/cmi5/context/extensions/launchmode
https://w3id.org/xapi/cmi5/context/extensions/launchurl
https://w3id.org/xapi/cmi5/context/extensions/moveon
https://w3id.org/xapi/cmi5/context/extensions/launchparameters
https://w3id.org/xapi/cmi5/result/extensions/progress
https://w3id.org/xapi/cmi5/result/extensions/reason
https://w3id.org/xapi/cmi5/activitytype/block
https://w3id.org/xapi/cmi5/activitytype/course
https://w3id.org/xapi/profiles/cmi5/v1/CourseStructure.xsd   (XML namespace)
```
**xAPI core**
```
http://adlnet.gov/expapi/verbs/voided                      (voiding)
http://adlnet.gov/expapi/attachments/signature             (signed statement usageType)
http://adlnet.gov/expapi/activities/cmi.interaction        (interaction activity type)
https://w3id.org/xapi/profiles/context                     (xAPI Profiles JSON-LD context)
https://w3id.org/xapi/profiles#1.0                         (profile conformsTo)
```
**ADL verbs** (prefix `http://adlnet.gov/expapi/verbs/`): answered, asked, attempted, attended, commented, completed, exited, experienced, failed, imported, initialized, interacted, launched, mastered, passed, preferred, progressed, registered, responded, resumed, scored, shared, suspended, terminated, voided.

**ADL activity types** (prefix `http://adlnet.gov/expapi/activities/`): assessment, attempt, course, file, interaction, lesson, link, media, meeting, module, objective, performance, profile, question, simulation, cmi.interaction.

## 17. xAPI beyond the LMS: simulations, ranges and exercises

Tag: GUIDANCE.SIMULATION

xAPI's main advantage over SCORM is recording evidence from systems that are not courses. Patterns that hold up:

- **Server-side producers.** The simulator, range orchestrator or scoring engine is the LRP, not the trainee's browser. It holds its own credential and signs nothing on the trainee's behalf beyond what the profile defines.
- **Meaningful events, not telemetry.** Record decisions, detections, objectives met, injects handled, errors and outcomes. Raw telemetry (packets, keystrokes, tick-level state) stays in its own store; statements reference it through an extension or an attachment `fileUrl`.
- **Actors and teams.** Individual trainee as `actor`; team as an identified Group. Under 2.0, put the team in `contextGroups` and instructors, assessors, observers or opposing-force role players in `contextAgents` with profile-defined `relevantTypes`. Under 1.0.3, use `context.team`, `context.instructor` and profile extensions for the rest.
- **Objects.** Scenario, phase, inject and objective activities with activity types defined in the profile. Use `contextActivities.parent` (phase or scenario) and `grouping` (exercise, course, programme) so every statement can be rolled up.
- **Results.** `success`, `score`, `duration`, and result extensions for domain measures (time-to-detect, time-to-contain, false positives). Define units in the extension's schema, in SI units.
- **Registrations.** One registration per trainee (or team) per exercise run. It scopes State documents and pattern validation.
- **Linking to an LMS.** When a scenario must count toward LMS completion, make a small cmi5 AU the launch point: the LMS launches the AU, the AU starts the scenario, the range backend reports outcomes, and the AU (holding the session token) sends `completed`/`passed`. Range-side statements can carry the cmi5 context template as cmi5-allowed statements, keeping them tied to the LMS session.
- **Assessment evidence.** Observer rubrics captured on a tablet become statements with the observer in `contextAgents` (2.0) or `instructor` (1.0.3) and rubric criteria as activities or result extensions.
- **Test with synthetic data first.** Generate statements from the profile with DATASIM to size the LRS, test dashboards and validate the profile before the first live exercise.

## 18. Sources

Tag: SOURCES

Primary specifications and standards bodies:
- IEEE 9274.1.1-2023 (xAPI 2.0) project page: https://standards.ieee.org/ieee/9274.1.1/7321/
- IEEE 9274.1.1 open-source documentation: https://opensource.ieee.org/xapi/xapi-base-standard-documentation
- IEEE 9274.1.1 examples: https://opensource.ieee.org/xapi/xapi-base-standard-examples
- IEEE P9274.3.1 (cmi5) project page: https://standards.ieee.org/ieee/9274.3.1/11183/ ; repo: https://opensource.ieee.org/xapi-cmi5/9274.3.1
- IEEE P9274.4.2 (xAPI cybersecurity) project page: https://saforms.ieee.org/ieee/9274.4.2/7721
- xAPI 1.0.3: https://github.com/adlnet/xAPI-Spec
- cmi5 Quartz: https://github.com/AICC/CMI-5_Spec_Current/blob/quartz/cmi5_spec.md ; FAQ and flows: https://aicc.github.io/CMI-5_Spec_Current/
- xAPI Profiles: https://github.com/adlnet/xapi-profiles
- ADL technical report, xAPI 1.0.3 to IEEE 9274.1.1 changes (2021): https://apps.dtic.mil/sti/trecms/pdf/AD1147698.pdf
- DoDI 1322.26 implementation references (ADL): https://adlnet.gov/policy/references

Tools:
- ADL LRS: https://github.com/adlnet/ADL_LRS
- LRS Conformance Test Suite: https://github.com/adlnet/lrs-conformance-test-suite
- CATAPULT: https://github.com/adlnet/CATAPULT ; project page https://adlnet.gov/projects/cmi5-CATAPULT
- SQL LRS: https://github.com/yetanalytics/lrsql (Docker usage: doc/docker.md)
- Ralph: https://github.com/openfun/ralph

Secondary (vendor and community notes used for status and migration context):
- Rustici/xAPI.com: "What vendors can do to prepare for xAPI 2.0" (2023-10), "Reflecting on 10 years of xAPI" (2023-04), "What happened in 2023 and what's next for eLearning standards" (2024-02); Engine 23 release notes (2024-02).
- cmi5 working-group minutes, 2023-09-15 (xAPI version compatibility): AICC/CMI-5_Spec_Current wiki.

## 19. Confidence and known gaps

Tag: META.CONFIDENCE

| Area | Confidence | Basis / caveat |
|---|---|---|
| cmi5 Quartz rules (Sections 9.1-9.16) | High | Read from the spec text on 2026-09-23; XSD details checked against `v1/CourseStructure.xsd`. |
| xAPI 1.0.3 model and API (Sections 5-6) | High | Checked against the 1.0.3 spec text where noted; some table cells are summaries. |
| xAPI 2.0 change list (Section 7) | Medium-high | From the 2021 ADL technical report written against the final draft. The published standard may differ in detail. The report's own summary of the MUST/SHALL change is internally inconsistent; its change-log table and vendor notes agree that MUST became SHALL. Exact JSON property names for `contextAgents`/`contextGroups` entries (`agent`, `group`, `relevantTypes`, `objectType` values) and `relevantTypes` cardinality: verify in the 9274.1.1 text. |
| Standards status (Section 2) | Medium-high | IEEE project pages plus an independent check dated 2026-08-19. IEEE status can change without notice; re-check before citing. |
| Product capabilities (Section 11) | Medium | Vendor-reported or repository-reported. Require current conformance output. |
| Scripts, AU and examples (Section 12, companion files) | High for tested behaviour | Tested 2026-09-23 against SQL LRS v0.9.8 (ephemeral SQLite, run from the release JAR): smoke test 14/14 with both `1.0.3` and `2.0.0` headers; session simulator end to end; AU harness 9/9; every example statement accepted with the matching version header; the 2.0 example rejected (`400`) under a `1.0.3` header; voided statement returns `404` by `statementId` and `200` by `voidedStatementId`. `fetch-sources.sh` fetched all GitHub sources; the `opensource.ieee.org` part could not be tested from the build environment. The Docker Compose file was not executed (no Docker there); it follows the upstream compose and documented environment variables. |
| Privacy and policy notes (13.1, 15) | Medium | General; confirm with your privacy and ATIP office and security authority. |
