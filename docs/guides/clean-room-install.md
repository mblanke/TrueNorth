# Clean-room install: acceptance protocol

Version: 1.1 · Applies to: v1.2.0-rc22 · Last reviewed: 2026-11-18

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

## Two phases

The install has two halves that need different things, so the clean-room runs in two
phases. **Phase A needs no vCenter and no golden templates**; you can finish it alone, on one
Ubuntu VM and an Active Directory. **Phase B** needs a real vCenter and templates the owner
has ported (06 §4).

| | Phase A: mock provisioner | Phase B: real vCenter |
|---|---|---|
| What runs | The whole platform: sign-in, classes, roll call, booking, the Student's page | The same, with ranges built on real VMs |
| Ranges | Simulated: nothing is cloned and no VM exists. The console shows a stub screen | Real clones of promoted golden templates |
| Installer setting | `tn_provisioner_backend: mock` in `site.yml` | `tn_provisioner_backend: vsphere_api` (the default) |
| Stages | 1 to 3 (vCenter parts skipped), 4 to 8, 10, 11 | All of Phase A, plus 3 in full, 9, 12 to 14 |
| Needs from the owner | Nothing | The ported template kit and the two template build passwords (below) |

Phase A proves the documents, the installer and the application. It does **not** prove that
a Student's range builds or that the console shows a real machine; only Phase B does.

### Phase A: what to set differently

Everything else in the guides is as written. Phase A differs in four places:

1. 02 §5.3: add `tn_provisioner_backend: mock` under the host in `site.yml`. The installer
   then skips every vCenter check and `90-vsphere` does nothing (`install/README.md`).
2. 02 §5.2: `vault_vsphere_password` may stay the placeholder. Leave
   `vault_template_windows_admin_password` and `vault_template_linux_password` as shipped:
   mock ranges clone nothing, so nothing checks them.
3. Skip 02 §3 (prepare vCenter) and 06 §4 (port templates). Skip the `govc` install.
4. 03: the **Golden images** step has nothing to promote. Note it as skipped, and note that
   the range-booking step (04) builds a simulated range.

To go on to Phase B, change `tn_provisioner_backend` back to `vsphere_api`, fill the vSphere
values, set the two template passwords in the vault, and re-run the installer (02 §6.2).

### Phase B prerequisites (the owner provides, before Phase B starts)

All of these, in hand, or Phase B waits:

- [ ] A vCenter that guide 01 §2 describes, the `TrueNorth-Provision` role and folders
      (02 §3), and the range hosts' `<range-host-N>-local` datastores named as guide 01
      §2.2 says.
- [ ] The template kit from a site where the templates were built and proven: per template,
      the exported OVF, its `<id>.build.json` build record and `SHA256SUMS` (06 §4.1).
- [ ] The **template build passwords** (`vault_template_windows_admin_password`,
      `vault_template_linux_password`), handed over by the custodians of that build record,
      outside the kit and never in the log. These are the only secrets Phase B needs that
      you cannot generate.
- [ ] At minimum the templates a first small range uses (the owner names which catalogue
      ids), ported and promoted (06 §4.2).

## What you need before you start

Everything in the [site prerequisites checklist](01-site-prerequisites.md) is ticked or has a
recorded exception, and the site values sheet (`<angle-bracket>` values) and the Active
Directory accounts the guides name are in hand. For Phase B, also the vCenter accounts and
the prerequisites above.

## Run order

Time each stage (start / end / elapsed). Tick only when the guide's own check passed. Stages
marked **B** are Phase B only.

| # | Stage | Guide | Done when |
|---|---|---|---|
| 1 | Site prerequisites verified | [01](01-site-prerequisites.md) | its checklist is ticked |
| 2 | Ubuntu VM built | [02 §1](02-install-guide.md) | `dig` forward and reverse answer; chrony synchronised |
| 3 | Control node prepared; **B:** vCenter prepared | [02 §2–3](02-install-guide.md) | `ansible --version` runs; **B:** `TrueNorth-Provision` role and the `TrueNorth/Ranges` and `TrueNorth/Templates` folders exist |
| 4 | Release fetched and verified | [02 §4](02-install-guide.md) | cosign `Verified OK` twice; `sha256sum -c` OK |
| 5 | Installer configured | [02 §5](02-install-guide.md) | vault encrypted; LDAPS verifies |
| 6 | Installer run, then run again | [02 §6](02-install-guide.md) | run 1 and run 2 both pass |
| 7 | First sign-in | [02 §7](02-install-guide.md) | break-glass and first AD administrator in; System Health green |
| 8 | Admin first day | [03](03-admin-first-day.md) | its checklist is ticked |
| 9 | **B:** Backup and restore drill | [06 §3.6](06-operations-runbook.md) | drill restores to the second management host |
| 10 | A class is set up | [04](04-instructor-guide.md) | roll call sent, Students registered, range booked and approved (Phase A: a simulated range) |
| 11 | A Student takes it | [05](05-student-quick-start.md) | a Student signs in, joins, opens the console (Phase A: the stub screen) |
| 12 | **B:** Templates ported and promoted | [06 §4](06-operations-runbook.md) | each needed image is Promoted, with a copy on every range host |
| 13 | **B:** A real range is built | [04](04-instructor-guide.md) | its Certification card shows the promoted build on every VM |
| 14 | **B:** A Student opens a real machine | [05](05-student-quick-start.md) | the console shows the machine's own screen, not the stub |

Stages 10 and 11 use a throwaway class of at least three test Students. The 12 to 15 Student
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

- [ ] Phase A (stages 1 to 8, 10, 11) completed with no help from the owner.
- [ ] Phase B (stages 9, 12 to 14, and 3 in full) completed, with help from the owner only for
      the Phase B prerequisites above.
- [ ] Every `missing` and `wrong` row has a merged fix.
- [ ] The backup and restore drill restored a working system.
- [ ] An upgrade from the previous release candidate ran by 06 §2.
- [ ] A real class of 12 to 15 Students ran end to end (roll call, rules of use, booking
      approval, individual and collective ranges, attendance, close-out).

Record: installer ________  date ________  observer ________  total elapsed ________
