# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the "Label in our tracker" column to match whatever vocabulary you actually use.

## Category labels

Category labels say what an issue is about. They sit alongside the triage role.

| Label                      | Meaning                                |
| -------------------------- | -------------------------------------- |
| `R0`                       | v1.2.0 release work                    |
| `R1`                       | Teach a class (F1, F2)                 |
| `R2`                       | Explain outcomes (F3, F4, M3)          |
| `R3`                       | Improve the next run (F5, F6)          |
| `H`                        | Needs the work lab / real hardware     |
| `security`                 | Auth, tenancy, secrets                 |
| `owner-decision`           | Only the owner can decide              |
| `blocked-on-site`          | Needs the owner on the work network    |
| `needs-adversarial-review` | Adversarial review before merge        |

Rule: an issue carries exactly one triage role label and any number of category labels.
