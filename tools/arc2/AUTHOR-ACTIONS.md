# ARC² author actions: make it; ask a person only for what only a person can give

Every `arc2-*` agent follows this. The course author reads the result as a to-do list, so
each item has to say exactly who acts, what they do, where, and why a model could not.

## 1. Default: make it

If an agent can produce something honestly, it produces it. That includes, and is not
limited to:

- page text, quizzes, lab steps, answer keys, rubrics, validators, configs;
- names and values that follow from the chosen range template (interface names, host
  names, addresses in the template), stated as such;
- draft procedures and policies (for example "how to approve and store a capture"),
  written as drafts for approval;
- **synthetic teaching captures** of ordinary network traffic, rendered by
  `tools/arc2/pcapgen.py` from a spec the agent writes (range-engineer; see its rules).

Something made this way that a person should approve is a `confirm` action. Do not leave
an `AUTHOR-REQUIRED` placeholder for anything in this list.

## 2. Ask a person only for these

| `ask` | What it is | Typical `who` |
|---|---|---|
| `decide` | A judgement reserved to an authority: PO or qualification binding, the summative verdict, a security sign-off, publication | Standards; Security; Range ops |
| `supply` | Material that must be real and cannot be synthesised honestly or safely: sanitised telemetry of real attacker technique, local facts that are not in the repo (unit procedures, the delivery environment's real host names, contacts), credentials | Cleared author; Unit; Instructor |
| `confirm` | Something an agent made that a person approves before promotion | Instructor; Curriculum review; Standards |

Only `supply` items may leave an `AUTHOR-REQUIRED` placeholder in learner-facing content.
A `confirm` item never does: the draft is written, and the note that it awaits approval
belongs in instructor material, not on a learner page.

## 3. How every human action is written

Set `ask` and `who` on every `human_actions[]` entry, and write `text` as:

> **<Who>**: <the exact thing to do> in `<file or place>`, because <why a person>.

`blocks_promotion` is true only when the course cannot safely or honestly be promoted
without it (a `decide`, a `supply` the course depends on, a security `confirm`). Style,
wording and nice-to-have confirmations do not block.
