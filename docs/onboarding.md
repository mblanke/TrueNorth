# Onboarding — how someone becomes a TrueNorth user

> The trainee journey, and the instructor runbook for admitting a cohort.
> For the identity plumbing underneath it, see [identity.md](identity.md).

---

## The short version

Identity comes from Active Directory. TrueNorth never holds a password.

```
AD credentials → Keycloak (LDAPS federation)
   → authenticated, but no TrueNorth account
   → /register            what AD does not know: rank, unit, callsign, cohort
   → awaiting approval
   → an instructor approves, assigning role and tenant
   → account created, enrolled on a qualification
   → first-run: confirm profile → confirm path → short tour
   → Learning hub
```

**Approving is what creates the account.** Nothing else in the product does.
AD group membership *suggests* a role on the approval screen and decides who is
eligible to apply — it never grants anything on its own.

---

## For a trainee

### 1. Sign in

Go to `https://<your-range>/` and sign in with your **normal domain
credentials** — the same username and password you use for everything else.
TrueNorth has no separate password to forget.

### 2. Request access

The first time, you land on a short form instead of the dashboard.

Your name, email and directory details are already filled in and cannot be
edited — they come from the directory, and changing them here would only put
the two out of step. What the form asks for is what the directory does not know:

| Field | Why |
|---|---|
| Rank, service/branch | Appears on your transcript and in exercise rosters |
| Unit | Used to group cohorts |
| Callsign | How you are identified during an exercise |
| Nation | Coalition and auth-zone handling |
| Time zone | Exercise scheduling |
| Qualification | The programme you are joining, e.g. Cyber Operator DP1 |
| Cohort / serial | Lets your instructor approve the whole course at once |

Submit, and you are on the waiting screen.

### 3. Wait for approval

The page checks for you every 30 seconds, so you can leave it open — or sign
out and come back. An instructor has to admit you.

If your request is **declined**, you see the reason and can submit a new one.

### 4. First run

Once approved, you get three short steps: confirm the details you already gave,
confirm the training path you have been enrolled on, and a quick tour of where
things live. Then you are in.

You will not be asked to do this again.

---

## For an instructor: admitting a cohort

**Users → Approvals.**

The queue shows everyone waiting, with the AD groups they belong to and the role
those groups *suggest*. The suggestion is pre-selected in the role picker so the
common case is one click — but it is visible and changeable, because a group can
propose `admin` and it is still a person who decides.

### One at a time

Pick the role, press **Approve**. The account is created, and if the applicant
asked for a qualification they are enrolled on its courses in the same
transaction.

### A whole course

1. Filter by **Cohort** — this is why the registration form asks for a serial.
2. Tick the applicants (or the header box for all of them).
3. Choose the role once.
4. **Approve N**.

Everyone in the selection gets the same role. Each request is applied inside its
own savepoint, so one bad row in a class of thirty does not cost the other
twenty-nine their accounts — the result panel names any that failed and why.

### Declining

**Decline** asks for a reason, and the applicant sees it. Write something they
can act on: "wrong serial — you are on 0042, not 0041" is useful; "no" is not.

---

## For an administrator

### Before the first trainee

A fresh install has exactly one account: the bootstrap administrator, created by
the installer from a **named AD account** (`tn_bootstrap_admin_upn`). There is
no `admin@truenorth.local` in production — that account exists only in
development, and the installer sets `SEED_DEV_DATA=false` to keep it out.

If nobody is nominated, the install finishes with an approval queue nobody can
drain. The installer refuses to proceed rather than let that happen.

### Roles

| Role | Wire value | For |
|---|---|---|
| Administrator | `admin` | Everything |
| Instructor | `instructor` | Authoring, exercises, scoring, **approvals** |
| Trainee | `student` | Consume ranges, run exercises, own progress |
| Observer | `observer` | Read-only plus telemetry |
| Range operator | `range_ops` | Infrastructure; no exercise or scenario authoring |

The UI says "Trainee"; the value on the wire is `student`. The backend enum is
the source of truth and the frontend follows it.

### Pre-loading a roster

Two paths, and they converge:

- **AD group sync** — the normal case. Keycloak federates the group; each person
  self-registers on first login and arrives in the queue with their role already
  suggested.
- **CSV import** — for a cohort that must exist before anyone logs in. Imported
  rows carry `source='csv_import'` and a placeholder Keycloak id. When that
  person later signs in and is approved, TrueNorth **adopts** the existing row
  rather than creating a duplicate.

Adoption is deliberately restricted to `local` and `csv_import` rows. Linking an
identity-provider subject to an existing account by email address would be an
account-takeover vector with a self-registration IdP; it is safe here only
because the IdP is read-only-federated Active Directory.

---

## Troubleshooting

**"I sign in and get sent straight back to the login page."**
The token is fine but the API is unreachable — check `/api/health`. A genuine
auth failure sends you to Keycloak, not to a loop.

**"A trainee says they were approved but still sees the waiting screen."**
The page polls every 30s; **Check now** forces it. If it persists, confirm the
approval actually created a user (the queue row should show `approved` with a
`created_user_id`).

**"Everyone in the queue shows no suggested role."**
The access token is not carrying a `groups` claim, so the group→role mapping has
nothing to read. This is a Keycloak configuration problem, not an application
one — see [identity.md](identity.md#the-claim-contract) and re-run
`ansible-playbook playbooks/60-keycloak.yml`.

Registration still works; every applicant simply arrives with a blank
suggestion, and group-based eligibility silently permits everyone. Nothing
errors, which is exactly why the installer asserts on it.

**"An approval failed with 'a user with email … already exists from source ad'."**
Someone else already has an account with that email address, from a different
identity-provider subject. That is a genuine collision and needs a human to look
at it — TrueNorth refuses to silently re-point an existing account.
