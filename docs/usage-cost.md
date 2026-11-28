# Usage and cost per Student (showback)

What each Student's ranges cost to run: **allocated size × time held × rate card**.
Admins only. Instructors, Observers and Students never see it. Code: `control-plane/api/app/usage/`.

## Where the figures come from (S1, derived, read-only)

| Source | Interval | Charged to |
|---|---|---|
| Self-serve lab | `provisioning_at` → destroyed (else `ended_at`, else now) | the lab's Student. **Exact**: labs are never stopped and started. |
| Class seat range | `class_ranges.built_at` → `torn_down_at` / now | the roll-call entry's Student |
| Class shared range | the same | split evenly over who was on the roll call at each moment (`joined_at` → `removed_at`; pending entries never count) |
| Any other range | `ranges.created_at` → deleted / destroyed / now | **Unattributed**, its own line per tenant, never dropped |

Sizes:
- **Labs:** the lab's own `vcpu`, `ram_mb` and `disk_gb`. The VM count comes from the range's template.
- **Other ranges:** the template's sizes (`range_topology.template_demand`). Every clone is a full copy today, so disk is the full template size.

**Power history (S2, ADR 0021).** Every change of a range's state appends a row to `range_state_events`. Three writers add rows:
- the worker's `update_range_state`;
- the API's flush hook (`app/usage/ledger.py`);
- an hourly reconciler (`worker/usage_reconcile.py`), which records anything the other two missed.

Class ranges and other ranges are cut by those events:

| State | Pays for |
|---|---|
| provisioning, ready, running, stopping, starting, destroying | compute and disk |
| stopped | disk only |
| created, destroyed | nothing |
| failed | what it paid before the operation that failed (a failed start: disk; a failed stop: compute; a failed build: nothing) |

Some time is still flagged `approximate`:
- time before a range's first event (history from before the ledger), charged all on as in S1;
- time ending at a reconcile event (the missed change happened somewhere in it);
- a range with no events at all.

Labs are not cut by events; their own timestamps are already exact.

**Rebuilt class ranges.** A class range that was rebuilt keeps only its newest `range_id`. The torn-down ranges before it report as unattributed.

## Rate cards

- **Rates.** Each card sets a price per vCPU-hour, per GB-RAM-hour, per GB-disk-hour (shown as GB-month = ×730) and per VM-hour (licences). It also has a currency and an `effective_from`.
- **Scope.** A card is either one tenant's or the platform default (`tenant_id` null). A tenant with no card of its own in force uses the platform default.
- **Changing a price.** Add a new card with a later `effective_from`. Cards are never edited, so a past month keeps the price it ran at. An interval that crosses a price change is split there.
- **No card in force.** Time with no card in force is counted but not costed. It shows as `unpriced_hours`.
- **Who can add cards.** Adding a card needs `usage:admin` and is audit-logged. A platform-wide card needs the platform administrator (`PLATFORM_TENANT_ID`).

Working a rate out from hardware, as an example:
1. A $400k cluster over 5 years is $80k a year.
2. Add power and licences: $20k a year, for $100k a year in total.
3. At 60% target use of 2,000 vCPU that is about 10,500 vCPU-hours a year per $1k, which works out to about $0.0095 per vCPU-hour.

Split RAM and disk out the same way if you want them priced separately.

## API

| Endpoint | Permission |
|---|---|
| `GET /usage/cost?from&to&group_by=student\|course\|class\|tenant[&tenant_id]` | `usage:read` |
| `GET /usage/cost.csv` (same query) | `usage:read` |
| `GET /usage/cost/students/{user_id}` (interval drill-down) | `usage:read` |
| `GET /usage/rate-cards`, `POST /usage/rate-cards` | `usage:read`, `usage:admin` |

- **Defaults.** The period defaults to the start of this month (UTC) up to now.
- **Who sees what.** A tenant admin sees their own tenant. The platform administrator sees every tenant, or one tenant with `tenant_id`.
- **Deleted Students.** A deleted Student's history stays in the report, labelled "Deleted Student".

## The page (S3)

**Admin → Usage and cost** (`/usage-cost`). Only roles holding `usage:read` see the menu item, which today means administrators. Mockup: `docs/mockups/usage-cost.html`.

- **Cost tab.**
  - The top shows the period's total, split into compute and disk. Grouped by Student, it also shows the unattributed amount and the highest-cost Student.
  - The table groups by Student, course or class; the platform administrator also gets Organisation and can pick one organisation.
  - Periods: this month, last month, or the last 30 days. Months follow the browser's time zone.
  - Rows are sortable. Named rows come first; unattributed, self-serve and "no class" rows come last.
  - Clicking a Student opens their drill-down: one block per range, with running, stopped (disk only) and approximate hours, and every stretch on demand.
  - "Download CSV" fetches the same rows through the API client, so the sign-in token goes with it.
- **Rate cards tab.**
  - Lists the platform default and each organisation's cards, newest first, marked "in force" or "scheduled".
  - "New rate card" needs `usage:admin`. Only the platform administrator can choose the platform default.
  - The disk rate is entered per GB-month and stored per GB-hour (÷ 730, 6 decimal places).
  - A helper works out rates from hardware cost, the years it is paid off over, monthly running cost, expected use and capacity. It stores nothing.

## Next

- Sizes from what was actually provisioned, once linked clones land. Today every clone is a full copy.
- A rule for charging booking and exercise ranges to Students. Today they are unattributed.
