# ARC² house style for TrueNorth courses

For the coder model that drafts one course at a time on TAZ. You have never seen ARC², the
course generator that produced TrueNorth's best courses. This guide is what it does, so your
course reads, is built and is marked the same way. `AGENTS-courses.md` says what you may
change and when you are done; this file says what good looks like. The checker
(`scripts/check_course.py`) enforces the rules marked **[check]**; the rest are judged by
Claude on review, and a course that only satisfies the checker will be sent back.

Read in this order before you write a word:

1. This guide.
2. `tools/taz/courses/reference/exemplar-arc2-c105.course.yaml`: a whole ARC² course.
   Read modules 1 and 3 closely. It is all-theory, so it has no `lab`; yours does.
3. `tools/taz/courses/reference/exemplar-lab-module.yaml`: one module in the shape you
   deliver (ARC² voice plus a `lab:` and a `lab_range:`).
4. `tools/taz/courses/reference/exemplar-arc2-c105.instructor-pack.yaml`: a finished pack.

Those three files are exemplars: copy their voice and shape, never their wording, and never
copy a quiz stem from them into your course. They are not for publication and you cannot
edit them.

## 1. What ARC² is, and what you add

ARC² turns a course request into a blueprint, course text, a quiz, a lab on a range, a
rubric, instructor material and a cmi5 package, checking each stage with contract tests
(`.claude/commands/arc2.md`; the agents in `.claude/agents/arc2-*.md`). Its output is always
a **proposed draft**: unsourced, unpublished, not validated by Standards. Nothing it makes
reaches a Student until Standards and an SME sign it off.

You work on a course that already exists as a thin draft. Your job is to bring it to ARC²'s
standard: observable objectives, topics that teach, a lab a Student can follow on a real
range, a five-item quiz that tests judgement, and an instructor pack. You do not write
learner HTML pages, rubrics, validators or packages; ARC² does those later.

## 2. Voice, reading level and length

Plain, concrete, defensive. Write for a first- or second-year college Student in a
cyber-operator programme: capable and new to the topic.

- **Sentences.** Short, active, present tense. One idea each. Define a term once, in
  plain words, the first time a topic uses it. No filler ("very", "simply", "basically"),
  no hype, no marketing, no jokes, no rhetorical questions in objectives or topics.
- **Spelling.** Canadian/British, as the exemplars do: organisation, authorise, defence,
  artefact, behaviour. Keep one spelling inside a course.
- **No contractions and no second person** in objectives, topics, lab briefs or lab steps
  (a lab step is an imperative: "Record ...", not "You should record ...").
- **People.** "Student" for anyone taking a course, "instructor" for staff who teach it
  (`docs/GLOSSARY.md`). Never "Member", "Learner", "Trainee", "user". **[check]**
- **Defensive and observable.** Describe what an analyst can see, detect, contain or
  decide. Never supply payloads, encoded commands, credential dumpers or reverse shells
  (section 11). **[check]**

| Field | Count and length (from the exemplar) |
|---|---|
| objectives | 3 per module; one sentence, 10 to 20 words, no full stop; 10 characters is the floor **[check]** |
| topics | 4 to 5 per module; a noun phrase or short sentence, 6 to 15 words, ending in a full stop |
| lab brief (`lab`) | 50 to 120 words in the three-part form of section 4 |
| lab steps | 3 to 8 per module, aim for 5 or 6; one action each, 15 to 35 words **[check]** |
| quiz stem | 13 to 30 words; a short scenario, then one question |
| quiz option | 1 to 12 words; the four options of one question are close in length and grammar |
| refs | 1 to 4 keys per module |

## 3. How a module is built

A course is **six modules**, in this order of difficulty: the first defines and
distinguishes, the middle ones apply and analyse, the sixth integrates what the earlier
modules taught (the exemplar's sixth module, security operations, applies the earlier
vocabulary to triage and escalation). Keep each module's `title`, `ordinal`, `duration_minutes`, `content_type`,
`is_required` and `pass_threshold` as they are. Do not change a pass mark: it is a draft
default, not a standard, and it is not yours to set.

Field order inside a module, as the exemplar has it: `ordinal`, `title`, `content_type`,
`duration_minutes`, `is_required`, `pass_threshold`, `objectives`, `topics`, `lab`,
`lab_range`, `refs`, `quiz`.

- **Pass thresholds.** The module's `pass_threshold` equals its quiz's `pass_threshold`
  (70 means 4 of 5). **[check]** ARC² checks the same equality (`cmi5.mastery_score`).
- **Objectives.** Verb, object, condition: "Classify the confidentiality, integrity or
  availability impact of each event in a written incident scenario". Open with a verb you
  can watch happen. The exemplar uses Classify, Identify, Distinguish, Map, Enumerate,
  Select, Design, Trace, Sequence, Triage, Write. Never open with understand, be aware,
  appreciate, know or be familiar (`tools/arc2/check.py:71`). **[check]** No PO, EO, NICE,
  DCWF or CSF codes in objectives.
- **Topics.** Teaching order: definitions first, then the distinctions, then applying them.
  Every objective is covered by at least one topic, and every topic serves an objective.
  A topic is a label with content ("Rule order, first-match evaluation and the implicit
  default action."), not a bare noun ("Firewalls.").
- **Objective to quiz.** Five questions cover three objectives: two, two and one. A
  Student who passes has shown each objective at least once.
- **Time.** The lab fits inside the module's `duration_minutes` with the teaching and the
  quiz; a 60-minute module is about 15 minutes of teaching, 35 of lab and 10 of quiz. The
  instructor pack says so (section 7). Do not change `duration_minutes`.
- **Practice.** ARC² also writes a `practice:` list (three tasks, one per objective) and
  `minutes_breakdown:`. They are optional for you; add `practice:` only if the draft
  already has it.

## 4. Labs

A lab is something a Student does on the range and can show they did. ARC² writes the
exercise, the evidence and the pass criteria together, then a rubric marks each criterion
Pass or Fail with no partial credit (`.claude/agents/arc2-artifact-creator.md`, "Steps" 3;
the rubric columns are criterion, evidence, standard, P/F). In your file the `lab:` string
carries all three, in this form **[check]**:

```
Lab: <what the Student finds out or does, and on what>. Evidence: <exactly what they
record>. Pass: <two to four items that are each plainly yes or no>.
```

Rules for the brief and the steps:

- **Real hosts only.** Open `content/ranges/<template>/template.yaml` and use the node `id`s
  it lists. Never name a host the template lacks, in `hosts:` or in `action:` text
  (`dc01` and `dc-01` are different; the template decides). **[check]**
- **One template per module**, chosen so every host the lab needs exists. Say what each
  host is for in the step, by its role: "the domain controller dc-01".
- **Order of steps.** Connect, observe, record, then judge. The last step is the
  Student's judgement of what they recorded (classify it, rank it, propose a change), not
  another command.
- **Every step produces something.** Each action ends in a thing to record or a decision.
  "Check the firewall" is not a step; "List the rules on fw-01 in order and record each one
  that allows user-network traffic" is.
- **Evidence is observable.** Values, names, counts, error text, timestamps; not "confirm
  it works". Say what the range should show where the template makes that knowable; do
  not guess values the range might not hold, and do not invent accounts, passwords,
  addresses or tools. Refer to what the instructor supplies ("the standard user your
  instructor assigned").
- **Pass criteria are P/F.** Each is a yes or no an instructor can mark without opinion.
  Judgement items read "each finding marked X or Y with a one-sentence reason".
- **Defensive.** Labs detect, analyse, configure and contain. If an objective truly needs
  real attacker material that cannot be made safely, write the line `AUTHOR-REQUIRED:
  <what a cleared author supplies>` and move on (`tools/arc2/AUTHOR-ACTIONS.md`: make
  everything that can be made; only real, sanitised material is left to a person).
- **Scenario.** Optional. If you add `scenario:`, it must exist under
  `content/scenarios/` and run on the same template. **[check]**
- **Agree with the brief.** Every Evidence item is produced by some step; every step's
  record appears in Evidence.

## 5. Quizzes

Five questions per module, each worth the same. The questions test whether a Student can
**apply** the idea, not whether they remember a sentence.

**Stems.**
- A short scenario (a named kind of organisation or system, one concrete fact), then one
  question: "A library's computers run an operating system that no longer receives security
  updates. In risk vocabulary, the unsupported system is best labelled as:".
- Ask for the best answer to one thing. No double questions, no trick negatives. If a
  negative is unavoidable, put the word in capitals ("does NOT prove").
- Use a term from the module's objectives or topics, so the question belongs to the module.
  **[check, heuristic: `quiz_terms`]**
- Never repeat a stem within the course or copy one from another course file (the
  checker looks at every file in `content/courses/`). **[check]** ARC² does the same
  (`tools/arc2/qa.py`, `course.duplicate_stem`). Do not reuse the exemplars' stems.

**Options.** Exactly four, lettered `A) ` to `D) ` in order. **[check]**
- Each is a standalone statement of similar length and grammar. Nothing refers to another
  option: no "all of the above", "none of the above", "both of these", "A and B". **[check]**
- One is clearly best. The three distractors are plausible: each is a real misconception
  or a near miss (a neighbouring term, the right idea in the wrong place, a true statement
  that does not answer this question). Do not use absurd options or "jokes".
- No two options mean the same thing. **[check]**
- Do not make the best answer the longest. Pad the distractors to the same length; the
  checker flags a quiz where the keyed option is strictly longest in 4 or more of 5. **[check]**
  (A model that writes the right answer last and longest teaches Students to pick the longest.)

**The key.** `answer:` is one capital letter A to D, and the keyed option is correct on its
own merits; check each key against the topic text once more before you finish.
- Spread the keys inside a module: at least three different letters, none more than twice
  (the exemplar's modules key `BCADB`, `CDABC`, `BCDAB`). Move the right option, not the
  stem. **[check]**
- Do not run all modules on the same pattern.

**What a good item tests.** Classify an example, choose the control that fits, spot what is
missing, say what a log line shows, decide the next step, or tell two near terms apart.

## 6. References

- `refs` holds keys from `content/catalogue/references.yaml`, and only those. **[check]**
  Never invent a key, title, URL, author, year, standard number or section.
- Choose the one to four references that cover the module's topics, most relevant first.
  Keep the references the draft already cites unless they plainly do not fit.
- If a module needs a source the library lacks, do not add a key. List it in plain words
  under a module-level `source_gaps:` list ("the NIST guide dedicated to ABAC is not in the
  reference library") and teach from what is listed. The schema allows the key for this.
  The exemplar records its gaps in `source.notes`, which you may not edit. ARC² never fills
  a gap from memory.
- Never cite the withdrawn or miscited publications (`tools/arc2/qa.py:52`). **[check]**
- In running text you may name a publication only when it is in the library.

## 7. Instructor pack

`content/courses/instructor/<same file name>.yaml`, shape fixed by
`docs/interfaces/course-instructor-pack.schema.json`. In ARC² this is three documents,
`answer_key.md`, `facilitation.md` and `marking_examples.md`
(`.claude/agents/arc2-artifact-creator.md`, "Theory and practical runs"); in your file they
shrink to the five fields below. The exemplar pack shows each at full quality.

- **`overview`.** Two to four sentences: audience and level, the six modules and how the
  labs fit together, what Students hand in. Say "Student".
- **`timings`.** One per module. `minutes` equals the module's `duration_minutes`
  **[check]**. `plan` splits the time in the order it happens and sums to `minutes`:
  `"15 teach, 35 lab, 10 quiz"`.
- **`answer_key_notes`.** One per module, written as `Q1 <letter>: why it is right; what
  each distractor tests.` through `Q5`, with the letter the quiz keys. **[check]** Name the
  misconception each distractor stands for, so a Student's wrong answer tells the
  instructor what to teach again. "B is correct" is not a note.
- **`lab_setup`.** One per module: what the instructor does before the lab. Revert the
  range to its baseline, which hosts to confirm reachable (by node id), the accounts
  or files the lab assumes, and the **values the Student should find** so the instructor
  can mark Evidence (for example, the lockout threshold the baseline holds). Name only
  hosts the template has.
- **`common_faults`.** At least one overall and ideally two per module, each a
  `fault` (what goes wrong on the range or in a Student's answer) and a `remedy` (what to do
  in the room). Real failures: a host that needs a revert, a command that hides the output,
  a misconception the quiz catches.
- **Separation.** All of this is staff-only (`audience: staff_only`). Answer keys, expected
  values, lab setup and common faults never appear in the course file, whose text Students
  read. The pack never says "learner". **[check]**
- Keep the pack consistent with the course: same `course_code`, one entry per ordinal.
  **[check]**

## 8. Naming and codes

- `course_code`: `C103`, `RMC C201` (a space) exactly as the draft has it. Never change
  the file name, the code or the title.
- Ordinals are 1 to 6. Quiz titles are `Quiz N` and the lab prefix is `Lab:`.
- Module titles are Title Case noun phrases ("Access Control and Identity"). Keep the draft's.
- Objective ids (`M01-O01`), CE ids and `mod_001` are ARC²'s manifest ids. They do not
  appear in a course YAML. Do not put them in text.
- No framework codes (PO, EO, NICE, DCWF, CSF sub-categories) in module text; mappings
  live in the crosswalk. `source.notes` already says so. **[check]**

## 9. The draft banner and readiness

Every ARC² course says it is a draft. Keep all of it as you found it **[check]**:

- the leading `#` comment lines, including "Unsourced generated draft: not validated by
  Standards, not for CAF learners.";
- `provenance: unsourced`, `status: draft`, `is_published: false`;
- every entry in `source.notes`, including "Unpublished pending Standards review.";
- `readiness: draft` is yours to set; `technically_proven` and `sme_signed` are not
  (`AGENTS-courses.md`).

## 10. Before and after

Three short pairs. The "before" is typical of an unreworked draft; the "after" is how
ARC² would write it.

### Objectives, topics and a quiz item

Before:

```yaml
objectives:
- Understand firewalls.               # unobservable verb; nothing to mark
topics:
- Firewalls.                          # a label, teaches nothing
quiz:
  questions:
  - question: 'A firewall is used to:'     # recall of a definition, no scenario
    options:
    - A) Block traffic
    - B) Filter traffic by a rule set at a network boundary and log what it drops
    - C) Encrypt files
    - D) Store passwords
    answer: B                         # keyed option is the longest; C and D are absurd
```

After:

```yaml
objectives:
- Read a firewall rule set and identify the traffic it allows that the stated policy forbids
topics:
- Rule order, first-match evaluation and the implicit default action.
- Reading a rule set against a written policy to find over-broad allows.
quiz:
  questions:
  - question: A rule set ends with "allow any any" after four specific blocks, and a new web
      server is added with no rule of its own. What happens to traffic sent to that server?
    options:
    - A) It is blocked because no rule names the server
    - B) It is logged but not decided until an administrator reviews it
    - C) It is allowed only after the server has replied first
    - D) It is allowed by the final rule, so the default works against the policy
    answer: D        # an observable verb, a scenario, a misconception behind each distractor
```

### A lab

Before:

```yaml
lab: 'Lab: Configure the firewall.'
lab_range:
  template: small-enterprise
  steps:
  - hosts: [fw-01]
    action: Configure the firewall and test it.     # configure what? test how? record what?
  - hosts: [dc02]                                   # no such host: the checker fails it
    action: Check the domain controller.
```

After:

```yaml
lab: >-
  Lab: Find which traffic fw-01 allows from the user network to the server network and judge it
  against this policy: workstations may reach dc-01 for sign-in and name lookups only.
  Evidence: each fw-01 rule that allows user-to-server traffic, in order, with its service,
  and the result of one test connection from ws-01 to dc-01. Pass: every such rule is listed
  with its order number; each is marked "in policy" or "over-broad" with a one-sentence reason;
  one change is proposed for each over-broad rule.
lab_range:
  template: small-enterprise
  steps:
  - hosts: [jump-box]
    action: Log on to jump-box over SSH, open a terminal and note the start time.
  - hosts: [jump-box, fw-01]
    action: From jump-box, list the rules on fw-01 in order and record each rule that allows
      traffic from the user network to the server network.
  - hosts: [ws-01, dc-01]
    action: From ws-01, try one service on dc-01 that the policy does not allow and record
      whether it connects.
  - hosts: [jump-box]
    action: Mark each recorded rule "in policy" or "over-broad" with one sentence of reason,
      and propose one change for every over-broad rule.   # ends in a judgement
```

### An answer-key note

Before: `notes: "D is correct. The others are wrong."`

After: `notes: "Q1 D: the final allow-any rule decides everything no earlier rule matches, so
the new server is open. A assumes a default deny this rule set lacks; B confuses logging
with deciding; C invents a reply condition. Q2 ..."` (all five, in order, with the letter
the quiz keys).

## 11. What ARC² never does

- **No live import, publish or provisioning.** It never commits, applies Terraform,
  provisions a range, or imports into the API; nothing leaves its build directory until a
  human promotes it (`.claude/commands/arc2.md`, "Hard rules"). You change two files in a
  disposable clone and nothing else.
- **No invented sources.** No reference, standard number, section or URL that is not in
  `content/catalogue/references.yaml`; no invented critical events, pass standards,
  durations or PO criteria. When it cannot source something it names the gap
  (`.claude/agents/arc2-content-architect.md`, "Rules"; the reference library's own header
  explains why).
- **No framework mappings in course text.** NICE, DCWF, CSF sub-category and PO/EO codes
  live in the crosswalk and in the xAPI sidecar, never in objectives, topics, labs or the
  quiz (`tools/arc2/cmi5.py:52`; `.claude/agents/arc2-code-generator.md`, "Rules"). **[check]**
- **No offensive tradecraft.** No payloads, encoded or hex runs (an unbroken 60-character
  base64-like string is flagged), `Invoke-` cmdlets, credential dumpers, reverse shells,
  `msfvenom` (`tools/arc2/qa.py:62`). Teach detection, analysis and containment.
  A line carrying `AUTHOR-REQUIRED` is exempt. **[check]**
- **No answers in learner text.** Rationales, expected values and solutions live in the
  instructor pack only.
- **No silent substitutions.** If an objective needs a live system, the lab uses the
  range; it does not swap in a paper exercise and call it the same.
- **No padding.** It does not inflate hours with prose, add modules, or force a module to
  fill its time (`docs/arc2-44-course-programme.md`, section 4).
- **No weakening of the checks.** It never edits the oracle, the schema or the baseline to
  make a run pass; a failing check is fixed at its source or reported.

## 12. Finishing

Run `scripts/check_course.py` (the command is in `AGENTS-courses.md`). Each line names its
rule; fix the source, not the checker. The house-style rules are `objectives`, `wording`,
`quiz_style` and `lab_style`, and the instructor check now also compares the pack's
timings and `Q1`..`Q5` letters with the course. Then read your course once as a Student
would, and once as the instructor who has to mark it.

## 13. Where the rest of ARC² lives

What this guide leaves out, and where to read it:

| Topic | Source |
|---|---|
| The seven stages and their order | `.claude/commands/arc2.md`, "Steps" |
| Course YAML pattern and the rules the generator obeys | `.claude/agents/arc2-code-generator.md`, "Steps" 2 to 7 and "Checks that will fail you" |
| Objective, PO and critical-event rules | `.claude/agents/arc2-content-architect.md`, "Steps" 3 to 5 |
| Range choice, lab profile, inject timeline | `.claude/agents/arc2-range-engineer.md` |
| Rubric, deliverable, retest form, answer key, marking examples | `.claude/agents/arc2-artifact-creator.md` |
| Validators and scoring | `.claude/agents/arc2-sensor-gateway.md` |
| What QA runs | `.claude/agents/arc2-qa-tester.md` and `tools/arc2/qa.py:132` (`course_rules`) |
| Packaging and cmi5 identity | `.claude/agents/arc2-package-builder.md`, `docs/cmi5.md` |
| What a finished course contains | `docs/arc2-44-course-programme.md`, section 4 |
| The Studio, gates and release | `docs/arc2-course-studio.md`, `docs/arc2-plan-v3.md` |
| Asking a person only for what only a person can give | `tools/arc2/AUTHOR-ACTIONS.md` |
| Workflow terms | `docs/GLOSSARY.md` |
