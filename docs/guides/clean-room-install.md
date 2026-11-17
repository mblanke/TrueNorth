# Clean-room install: acceptance protocol

For the colleague doing the clean-room install, and for whoever observes it. This is not
another guide. It is the test of the guides: **can someone other than the owner go from the
signed release to a running class using only the documents?**

## Rules

1. Use only [`docs/guides/`](README.md) and what they link to. Do not ask the owner. If you
   would have asked, that is a finding: write it in the stuck log (below) and then look it up
   or ask the observer to unblock you.
2. Follow the steps as written. Do not "fix" a step silently. Record what you did instead.
3. One person types. The observer only writes the log and answers *"is this a doc gap?"*,
   never *"how do I do this?"* (answers go in the log as the missing text).
4. Do not paste passwords, keys or tokens into the log. Say where they were stored.
5. Install the **release under test** (`<vX.Y.Z-rcN>`: ________), not `main`.

## What you need before you start

Everything in the [site prerequisites checklist](01-site-prerequisites.md) is ticked or has a
recorded exception, and these are in hand: the site values sheet (`<angle-bracket>` values),
the vCenter and Active Directory accounts the guides name, and a Windows and a Linux golden
template that the owner has ported and promoted (06 §4).

## Run order

Time each stage (start / end / elapsed). Tick only when the guide's own check passed.

| # | Stage | Guide | Done when |
|---|---|---|---|
| 1 | Site prerequisites verified | [01](01-site-prerequisites.md) | its checklist is ticked |
| 2 | Ubuntu VM built | [02 §1](02-install-guide.md) | `dig` forward and reverse answer; chrony synchronised |
| 3 | Control node and vCenter prepared | [02 §2–3](02-install-guide.md) | `TrueNorth-Provision` role and the `TrueNorth/Ranges` and `TrueNorth/Templates` folders exist |
| 4 | Release fetched and verified | [02 §4](02-install-guide.md) | cosign `Verified OK` twice; `sha256sum -c` OK |
| 5 | Installer configured | [02 §5](02-install-guide.md) | vault encrypted; LDAPS verifies |
| 6 | Installer run, then run again | [02 §6](02-install-guide.md) | run 1 and run 2 both pass |
| 7 | First sign-in | [02 §7](02-install-guide.md) | break-glass and first AD administrator in; System Health green |
| 8 | Admin first day | [03](03-admin-first-day.md) | its checklist is ticked |
| 9 | Backup and restore drill | [06 §3.6](06-operations-runbook.md) | drill restores to the second management host |
| 10 | A class is set up | [04](04-instructor-guide.md) | roll call sent, Students registered, range booked and approved |
| 11 | A Student takes it | [05](05-student-quick-start.md) | a Student signs in, joins, opens the console |

Stage 10 and 11 use a throwaway class of at least three test Students. The 12 to 15 Student
class is a separate acceptance step (see Pass criteria).

## Stuck log

One row per time you stopped, guessed, left the document, or the document was wrong. Copy
this table into the working copy of the log.

| # | Stage / section | What happened (1 line) | Type | Time lost | What would have prevented it |
|---|---|---|---|---|---|
| 1 | | | | | |

**Type** is one of: `missing` (step not written), `wrong` (written, does not work),
`unclear` (worked after guessing), `assumed` (relied on knowledge the reader may lack),
`product` (the software misbehaved; the doc is fine), `site` (this site's environment).

## After the run

1. File one GitHub issue per log row of type `missing`, `wrong`, `unclear` or `assumed`,
   titled `docs: <guide> §<n> <one line>`, labelled `R0`; link the log row.
2. File `product` rows as bugs with the usual labels.
3. A guide that needed a fix is not "checked against" the release until the fix is merged
   and the affected stage is re-read by someone who did not write it.
4. Run [`tests/contracts/test_docs_claims.py`](../../tests/contracts/test_docs_claims.py)
   after the fixes; it checks that six named UI features exist. A pass here does **not**
   mean the guides were re-read end to end.

## Pass criteria

The clean-room install passes when all of these hold:

- [ ] Stages 1 to 11 completed with no help from the owner.
- [ ] Every `missing` and `wrong` row has a merged fix.
- [ ] The backup and restore drill restored a working system.
- [ ] An upgrade from the previous release candidate ran by 06 §2.
- [ ] A real class of 12 to 15 Students ran end to end (roll call, rules of use, booking
      approval, individual and collective ranges, attendance, close-out).

Record: installer ________  date ________  observer ________  total elapsed ________
