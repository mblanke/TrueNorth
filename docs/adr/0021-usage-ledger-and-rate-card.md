# ADR 0021: Usage ledger and rate card

- Status: accepted 2026-11-27 (owner: cost "in dollars via a rate card"; "admin: per
  Student, rolled up"; plan approved, "start S2")
- Amends: ADR 0006 (CapacityService). CapacityService is "now"; this ledger is "history".

## Context

The owner wants to know what each Student costs to run on the range. Before this, nothing
recorded it:
- CapacityService (ADR 0006) knows only the present.
- `ranges` has no owner and no power history.
- `audit_logs` are kept hot for 90 days only (ADR 0011).

Usage S1 (PR #545) derives a report from the timestamps that already exist:
- **Labs** are exact, because they are never stopped and started.
- **Class ranges and other ranges** are charged for their whole life, even while
  powered off.

## Decision

1. **The cost basis is allocated size, not measured use.**
   - vCPU, RAM and VMs are charged while the range's VMs run.
   - Disk is charged while they exist.
   - Measured quickStats are not billed. They may later appear beside the cost as
     utilisation.
2. **Prices are applied when the report is read.**
   - The ledger records time, never money.
   - `rate_cards` are versioned by `effective_from` and never edited.
   - An interval that crosses a price change is split there, so changing a price never
     rewrites a past month.
   - A tenant's card beats the platform default (`tenant_id` null).
3. **`range_state_events` is append-only and records every change of `ranges.state`,**
   in the same transaction as the change. Each row holds `from_state`, `to_state`, `at`
   and `source`.
   - **worker:** `worker/db_ops.py` `update_range_state`. Every worker write goes through
     it, including fenced writes via `guarded_range_update`. It locks the range row and
     reads the state it replaces.
   - **api:** a SQLAlchemy `before_flush` hook (`app/usage/ledger.py`) sees every `Range`
     whose `state` the ORM writes, wherever in the API it was set. If the session has
     expired the old value, the hook reads the stored state. A Core `UPDATE` is
     invisible to the hook. The one that exists, `discard_unbuilt`, moves a range from
     created to destroyed; neither state holds anything.
   - **reconcile:** an hourly worker task (`worker/usage_reconcile.py`) handles any range
     whose state its last event does not show (a missed path, a soft delete, a hand
     edit). It appends the missing change, stamped when it was noticed. The change
     happened somewhere in the time before that event, so the report marks that time
     approximate. It re-reads the range's ledger under the range's row lock, so a change
     committed meanwhile is never recorded twice.
   - The table has no foreign keys: the history outlives the range and the Student.
   - Ranges older than the ledger get their first event from the reconciler.
4. **What each state pays for:**

   | State | Pays for |
   |---|---|
   | provisioning, ready, running, stopping, starting, destroying | compute and disk |
   | stopped | disk only |
   | created, destroyed | nothing |
   | failed | what it paid in its last settled state, before the operation that failed: a failed stop leaves the VMs running (compute), a failed start leaves them off (disk). Nothing if it failed out of a build. |

5. **Who a range is charged to is decided when the report is read, not recorded.**
   - **Lab:** its Student.
   - **Class seat range:** the roll-call entry's Student.
   - **Class shared range:** split evenly over the roll call at each moment.
   - **Anything else:** an "unattributed" line per tenant, never dropped.
   - Labs are not cut by events. Their own timestamps are already exact.
6. **Before the ledger:** time before a range's first event is charged as in S1 (all on)
   and marked approximate.
7. **Access is for admins only** (`usage:read`, `usage:admin`).
   - A tenant admin sees their own tenant. The platform administrator sees every tenant.
   - A platform-wide rate card needs the platform administrator.
   - Adding a card is audit-logged.

## Consequences

- **Class and other ranges become exact once the ledger runs.** A class range stopped
  overnight pays disk only for the night.
- **Disk is the template's full size, because every clone is a full copy.** When linked
  clones land (faster range builds S1), sizes should come from what was actually
  provisioned. A size snapshot per event is the natural place for it; it is not built
  yet.
- **Each worker state write costs one more `SELECT ... FOR UPDATE`.** It takes the same
  row lock the `UPDATE` takes, in the same order, so it adds no new deadlock.
- **The reconciler is not cheap.** It scans every range and the newest event of each, every
  hour, and holds the row locks it takes until it commits. That is fine at today's size. If
  it grows, batch the commits and skip ranges whose last event is destroyed.
- **API writes don't lock the range.** The `lab_sessions` and `range_ops` paths write
  without locking it first, so a worker change at the same moment can leave an API event
  naming a stale `from_state`. The reconciler then puts the ledger right, as an
  approximate stretch.
- **Rebuilt class ranges are only partly attributed.** A rebuilt class range keeps only
  its newest `range_id` on `class_ranges`. The earlier, torn-down ranges are no longer
  linked to the class, so they report as unattributed.
- **Booking and exercise ranges are unattributed until the owner picks a rule.** One
  option is splitting by the participants worked out in `app/exercise_participants.py`.
