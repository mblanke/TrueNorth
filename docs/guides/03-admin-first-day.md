# 03 — Administrator: your first day

For the administrator of a freshly installed TrueNorth Range site. It starts after the
installer has finished ([02-install-guide.md](02-install-guide.md)) and ends when instructors
can run classes. Day-to-day upkeep (backups, upgrades, porting golden images, alerts) is in
[06-operations-runbook.md](06-operations-runbook.md).

Checked against `main` at `fd4f79a6` (2026-10-27, v1.2.0); reconciled with `main` at `353339c2`
(2026-11-02) for site setup, the platform tenant, time zone, classes, roll-call domains,
users, email help, unit readiness and content port. Sections 5 to 8 and 11 were not re-read
end to end. Words follow
[GLOSSARY.md](../GLOSSARY.md). UI paths are written as **sidebar section → item → tab or
card → button**.

> **Things on this page that still have no screen** are marked **API only**. You do them with
> a bearer token (see [Using the API](#using-the-api-for-the-api-only-steps)). They are
> listed again in [Known gaps](#known-gaps).

---

## 0. Roles, and the one you need to be

| Role | What they do | Who grants it |
|---|---|---|
| **Admin** | Everything, inside their own tenant | An admin |
| **Instructor** | Classes, roll calls, bookings, exercises, courses, close-out | An admin |
| **Range Ops** | Infrastructure, provisioning, **approving class sessions** | An admin |
| **Observer** | Read-only: classes, calendar, Live Class, results; never range passwords | An admin or instructor |
| **Student** | Takes classes and self-serve labs | An instructor's roll call, or an approved access request |

The **platform administrator** is an Admin of the platform tenant. Only they can publish the
Rules of use, create tenants, set the over-capacity policy, promote golden images, import
feed bundles, manage the result-transfer trust keys, send a test email, and edit the shared
content themes and review cycles.

> **Check this first on a multi-tenant site.** Who counts as platform administrator comes
> from `PLATFORM_TENANT_ID`. The installer now passes it to the API from
> `tn_platform_tenant_id`; empty means the bootstrap tenant, which the seed records in
> `/srv/truenorth/state/platform_tenant_id` on the first run and never re-picks
> ([02-install-guide.md](02-install-guide.md)). While the site has **one** tenant, every
> Admin of it is the platform administrator. An id that is not a UUID, or names no tenant,
> makes **nobody** the platform administrator, and the API logs an error at startup that
> names the installer variable. A Kubernetes (Helm) install is not run by the installer: set
> `config.platformTenantId` there before a second tenant exists.

---

## 1. Sign in as the bootstrap administrator

The installer created one account, the **bootstrap administrator**, from
`tn_bootstrap_admin_upn`. It won't install without one. There is no `admin@truenorth.local`
in production.

- **With AD** (`tn_ldap_enabled: true`), you are that AD account and sign in with your
  domain password. With `tn_keycloak_local_admin: true` the installer also keeps a local
  **break-glass** account of the same name in Keycloak, for when AD is down.
- **Without AD**, it is a local Keycloak account. Its first password is the generated
  secret `bootstrap_admin_password` under `/srv/truenorth/config/secrets/` on TN-MGMT01.

Steps:

1. Browse to `https://<site>/`. Press **Sign in with SSO**.
2. Sign in on the Keycloak page.
3. **MFA.** Admins, instructors, range ops and observers need a second factor by default
   (`tn_mfa_roles`). On the first sign-in, Keycloak shows **Mobile Authenticator Setup**:
   enrol an authenticator app. If the app later says **Multi-factor sign-in required**, sign
   out and back in with the code.
4. You land on the **Dashboard**. On a fresh install, nobody is asked to sign the Rules of
   use until you publish them (step 2).

If you get no admin menu, you are not the bootstrap account: check the UPN in the installer
variables.

---

## 2. Publish the Rules of use

Everyone signs the **Rules of use** before doing anything else: on first sign-in, after
every new version, and every 365 days. Until they sign, the API refuses everything except
reading and signing them. The rules include the notice that sessions may be observed,
including live screens and consoles.

1. **Admin → Rules of use**.
2. **Versions** card. On a fresh site it already lists one unpublished draft holding the
   shipped default text: select it. **New draft** starts another one from the current
   version's text.
3. Edit the text in **Text (Markdown)**; the preview is beside it. **Save draft**.
   - Keep the **"Sessions may be observed"** section. ADR 0014 §3 requires it, but the
     editor won't stop you deleting it.
4. **Publish** → confirm in **Publish the rules of use**.
   - A published version never changes. A correction is a new version, and everyone signs
     again.
5. You are taken to the signing page yourself. Read to the end, then press **I agree**.

**Signatures.** **Admin → Rules of use → Signatures → Export signatures** downloads a CSV:
your tenant's signatures, or everyone's for the platform administrator. Each export is
audited.

Only the platform administrator sees the Versions card. Other admins see: "Only the platform
administrator edits and publishes the rules of use."

---

## 3. Set the roll-call email domains

An instructor adds Students to a roll call by email. TrueNorth accepts only addresses in the
tenant's **roll-call domains**. Which domains apply:

| Setting | Effect |
|---|---|
| Not set (fresh install) | The domains of the tenant's active AD accounts. With no AD accounts yet, **every add is refused**. |
| A list, e.g. `forces.gc.ca` | Only those domains. Up to 500 addresses per add. |
| **Any domain** | Any domain. At most 50 addresses per add. |

**Prepare & Operate → Classes**, **Roll-call domains** card (shown to everyone who can create a
class; only an admin sees **Change**). Its subtitle says what applies now, for example "Not
set by an admin: roll calls accept the domains of your organisation's AD accounts (…)".

1. Press **Change**.
2. Either tick **Any domain (roll calls then add at most 50 at a time)**, or type the
   **Allowed email domains**, one per line or separated by commas.
3. **Save**. It refuses an empty list with "Enter at least one domain, or tick "Any domain"."
   Each change is audited (`roll_call_domains_set`).

The error an instructor sees when nothing is set points to "Classes, roll-call domains": this
card.

Instructors may also send at most 100 onboarding and reminder emails per day each.

---

## 4. Capacity policy and maintenance windows

### Over-capacity policy

**Prepare & Operate → Schedule → Over-capacity policy** (admins):

- **Block** (the default): a booking or approval that doesn't fit is refused, for everyone.
- **Warn**: it goes through, with the reasons shown and logged.

Only the platform administrator can change it; other admins see the choices greyed out.
Self-serve labs ignore **Warn**: a lab that doesn't fit is always refused.

### Site time zone

Times the app says in words ("Labs are busy, next free at 14:00") and the day-before
roll-call reminder use the site's time zone: `tn_site_timezone` in the installer (an IANA
name such as `America/Toronto`). Left empty, it is the host's own zone, else UTC. A change
needs an installer re-run. (The API shows it as `site_timezone` in `GET /schedule/settings`.)

### Maintenance windows

**Admin → Maintenance windows** (admins) → **New window**:

- **Reason (shown in every refusal)**
- **Starts**, **Ends**
- **Covers**: **All ranges**, **Specific ranges** or **Ranges with tags**
- **Every organisation (platform-wide)**: platform administrator only

Then **Save**. The table offers **Edit** and **Delete**.

During a window:

| Refused (409) | Deferred until it ends | Always runs |
|---|---|---|
| Someone's provision, reset, restore or normal destroy | Scheduled builds; class range teardowns | Emergency teardown; a booking ending; the canary; Student lab cleanup |

Booking still works, and the window shows as **grey stripes** on **Calendar → Events**. An
approver cannot propose a time inside a window.

---

## 5. Golden images: promote, pin, port

A range clones golden templates. A build of a golden template is used only after the
platform administrator **promotes** it.

1. Build and replicate the template with the template kit
   (`infra/vsphere/packer/README.md`). Register-back records the build in TrueNorth.
2. **Admin → Infrastructure → Golden images** tab. The table shows **Image**, **Status**,
   **Promoted build** and **Checksum**.
3. Open the image. It lists its builds and the hosts that have no copy of each one.
4. Type the **Reason for promotion (kept in the audit log)**, then press **Promote**. A build
   with no host copy can't be promoted.

**Pinning is automatic** (ADR 0013 §2). Once an image has a promoted build, every new range
clones exactly that build. It uses the copy on the target host, checked against the build's
stamp. There is no separate pin button. Each VM records which build it got.

- `tn_template_allow_unpromoted` (default `false`) lets images with **no** promoted build
  through with a warning. Use it only while moving a site over. An image with a promoted
  build always uses it.
- Enabling or disabling a catalogue row, and importing the catalogue, are **API only**
  (`PATCH /golden-images/{id}`, `POST /golden-images/import-catalogue`).

**Porting images from the test site** to this site, and pruning old host copies:
[06-operations-runbook.md](06-operations-runbook.md).

---

## 6. Enclave and result-transfer trust registry

Two installer settings identify this deployment. Production won't start without both.

- `tn_enclave_id` (`TN_ENCLAVE_ID`, default `unclass-training`): 1–63 lowercase letters,
  digits or hyphens. It must never be reused for another enclave.
- `tn_classification_level` (`TN_CLASSIFICATION_LEVEL`): `unclassified`, `secret` or
  `top_secret`.

**Unclassified behaviour.** Scores and grades are pass or fail only: nothing numeric is
shown, exported or written to an enrolment. Free-text comments on results and notes on
attendance are still allowed (an approved exception, ADR 0014 §13).

**Trust registry (API only, platform administrator).** These are public keys of the remote
enclave that a result transfer trusts. They are never private keys.

```bash
curl -H "Authorization: Bearer $TOKEN" https://<site>/api/result-transfer/trust-keys
curl -X POST https://<site>/api/result-transfer/trust-keys \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"purpose": "receipt-signing", "remote_enclave": "secret-a", "public_key": "<44-char base64>"}'
curl -X POST https://<site>/api/result-transfer/trust-keys/<id>/retire -H "Authorization: Bearer $TOKEN"
```

- `purpose` is one of `result-signing`, `subject-signing`, `receipt-signing` or
  `result-recipient`. Which ones an enclave may hold depends on its classification.
- Enrolling and retiring are audited. A retired key stays in the history.
- The transfer itself is a foundation, not a finished operator flow: see
  [result-transfer.md](../result-transfer.md).

---

## 7. Content feeds: on or off

The feeds flag golden templates **stale** when a new release, an end-of-life date or a
known-exploited vulnerability affects them. They are set by `tn_feeds_mode`, and are **off**
on a fresh install.

| `tn_feeds_mode` | What happens |
|---|---|
| `off` | Nothing is pulled. Time-based review dates still work. |
| `online` | The worker with internet egress pulls endoflife.date, CISA KEV and the NVD daily at 04:10 UTC. Optional settings: `tn_feeds_http_proxy`, `tn_feeds_nvd_api_key`, `tn_feeds_nvd_min_cvss` (default 9.0). |
| `bundle` | Air-gapped: the platform administrator imports a signed feed bundle. Set its public key in `tn_feeds_bundle_public_key`. |

Change the mode in the installer variables and re-run the installer.

Check it at **Prepare & Operate → Calendar → Content → Feeds**. It shows **Off**, **Online**
or **Signed bundles**, the last pull or import, and any errors. In bundle mode, the platform
administrator has **Import bundle** there.

---

## 8. AD status sync: dry run first, then enable

Every night, TrueNorth checks Keycloak's view of AD. It deactivates TrueNorth accounts whose
AD account is disabled or gone, and local accounts unused for 90 days. It **never writes to
AD and never deletes anything**: enrolments, results and audit history stay. A deactivated
Student loses their roll-call places; their instructors are emailed.

**Where:** **Admin → Users → AD Sync** tab → **Directory status check** card. It shows the
mode, the break-glass account and its last use, the last run's counts, and a table with
**Account**, **Why**, **Deactivated** and **Seats freed**.

1. **Dry run.** The installer starts in dry run (`tn_ad_status_sync_dry_run: true`). Runs
   record who they *would* deactivate and change nothing. Press **Dry run now** to see a
   result straight away, then read a few nights of results.
2. **Check every name** in the table. Expect only people who really left. A name you didn't
   expect usually means the AD mapping is wrong.
3. **Enable it.** Set `tn_ad_status_sync_dry_run: false` and re-run the installer. The next
   night's run is the first one that applies changes.

Safety rules built in:

- An account is deactivated only if an earlier run, 12–36 hours before, found the same thing
  for the same reason.
- If one rule would deactivate more than a quarter of the tenant's accounts (at least 3), it
  is **held** and changes nothing. **Run now, over the limit** releases it for your tenant.
- The tenant's last active admin is never deactivated.
- The **90-day rule for local accounts runs only when a break-glass account is configured**,
  which needs AD and `tn_keycloak_local_admin`. The break-glass account itself is never
  disabled. Signing in with it raises the `BreakGlassAccountUsed` alert.

Known limits:

- Some accounts are created without their AD object id: the bootstrap admin, users added
  with **Add User**, and demo links. Such an account is **not** deactivated when its AD
  account is deleted, only when AD disables it, **until it signs in again**: the next
  sign-in records the id from the token (never over an id already held, #310). An account
  that never signs in again stays without one.
- **Delete** in Users only deactivates. A deactivated account shows a **Re-enable** button
  (the person-check icon, tooltip "Re-enable") in the Users list.

More detail: [identity.md](../identity.md), "AD status sync".

---

## 9. Email status

Email is **optional**. Without it, TrueNorth still works, but sends nothing:

- no roll-call onboarding emails;
- no day-before reminders;
- no seat-forfeit notices;
- no calendar invites.

The relay is set by `tn_smtp_host`, `tn_smtp_port` (587), `tn_smtp_security` (`starttls`,
`tls` or `none`), `tn_smtp_username`, `tn_smtp_from_address` and `vault_smtp_password`. An
empty host means email is off. **On v1.2.0 sites SMTP is parked:** no relay is set.

**The banner.** While email is off, every admin sees a red banner on every page:

> Email is not configured: onboarding emails, reminders and calendar invites are not being
> sent.

It can't be dismissed. It goes away once the relay is set and the API is restarted. Its
**Set up email** link opens a help page bundled in the app (**Set up email**, at
`/admin/help/email`), so an air-gapped site can read it; it summarises `install/README.md`,
"Email (SMTP)". Instructors see a matching warning on the roll call.

**While email is off, tell instructors:**

- Students must be told by other means to sign in.
- A roll-call Student who nobody could notify (email off, a refused send, the daily limit)
  **keeps their seat** even if they haven't signed in 2 hours after the first session
  starts; the roll call shows "not notified — seat kept". Instructors must get them signed
  in, or remove them.

**Check it:** **Admin → Admin → System Health → Email** shows **Configured** or **Not
configured: nothing is emailed**. The platform administrator also sees the relay and
**Send test email** (to your own address; once a minute; audited).

---

## 10. Tenants, units, instructors and observers

### Tenants

**Admin → Admin → Tenants → New Tenant**: **Name** and **Slug**, then **Create**. Only the
platform administrator sees **New Tenant**; other admins see "Only the platform administrator
creates tenants…" instead (they can rename their own). A name already taken is refused as a
conflict. Read
the warning in [section 0](#0-roles-and-the-one-you-need-to-be) before creating a second
tenant.

### Units

A Student's **unit** is the free-text **Unit** on their user record. It is filled from AD,
from Request access, or by an admin in **Users → Personnel → Edit**. The unit readiness page
filters by it.

**Users → Directory → Organizational Units** groups teams and security groups, not people.

> The **New OU** type list offers Country, Branch, Division, Unit, Section and Custom
> (default Custom); **Security Groups** offer Access, Distribution, Training and Role-based
> (default Access). These are the types the API accepts.

### Instructors and observers

- **Someone with no account yet:** they sign in once and fill in **Request access**. You
  approve them in **Admin → Approvals** (also **Users → Approvals**). Their AD groups
  suggest a role and tenant; the role picker shows the suggestion, and you decide.
  - **Filter by Cohort**, tick several, choose **Role to assign**, then **Approve N** to
    approve a group at once.
  - **Decline** asks for a reason, which the applicant sees.
  - **Only an administrator approves Request access.** Instructors no longer have
    **Approvals** on their menu; their Students come in through a class roll call.
- **Someone with an account:** **Admin → Users → Personnel →** the pencil (**Edit**) →
  **Role**: Student, Instructor, Observer, Range Ops or Admin.
- **On a class:** the lead instructor, assistant instructors and observers are chosen when
  the class is created (**Prepare & Operate → Classes → New class**). Changing them later
  (**Edit**), handing a class to another lead (**Hand over**) and cancelling it (**Cancel
  class**) are buttons on the class's row, for its lead or an admin while it is open
  ([04-instructor-guide.md](04-instructor-guide.md)).
- Approving someone into a **different** tenant is **API only** (`tenant_id` on
  `POST /registration/requests/{id}/approve`, platform administrator).

Students on a roll call need no approval: being on the roll call is the approval.

**Pre-loading people who must exist before they sign in (API only).** Upload a CSV roster
with `POST /users/import-csv` (`user:create`; `?dry_run=true` checks it first). When such
a person later signs in and is approved, TrueNorth **adopts** their row instead of making a
duplicate. For classes, use the roll call instead. Details:
[identity.md](../identity.md).

### Unit readiness (observers)

**Review → Unit readiness**. For observers, instructors and admins (not range ops, not
Students). It shows the classes of your tenant, with each Student's enrolment, recorded
attendance, result (Pass, Fail, Incomplete or "Awaiting result", with the instructor's
comment) and the qualifications earned, filtered by **From**, **To** and **Unit**, with **CSV**,
**PDF** and **Print**. It is read-only and never shows scores. Each CSV or PDF export is
audited. The Unit filter uses the free-text unit described above, so fill in units for your
Students first.

---

## 11. Content calendar and the review clock

All content has a review date: courses, modules, labs, scenarios, range templates and golden
images.

- An item becomes **stale** when its review is overdue (90 days by default).
- It also becomes stale when a feed flags something it is built from. That flag flows down
  the chain: golden image → range templates using it → courses using those.

**Prepare & Operate → Calendar → Content**:

- Items sit on their review-due dates in **Week** or **Month** view, coloured by theme. The
  theme chips filter them.
- **Stale now** lists everything stale, with its reason.
- On an item:
  - **Mark reviewed** resets its clock and clears what it caused downstream.
  - **Assign reviewer** sets who reviews it.
  - **Retire** (admins only) takes it off the calendar.

Instructors see this calendar read-only, except for items they own or have been assigned.
Range ops can see it.

**Proven chip.** The selected item on the Content view, a course's page, each range template
card in **Create → Authoring Studio → Content**, and the scenario studio show a chip for
staff who can read the content clock: **Proven**, or **Not proven: <reasons>**. **Why**
lists the three conditions and their evidence: a range built from the template as it is now
reached Certified, it was used with passing evidence (a closed class with a passing Student,
or a completed exercise at 70% or more), and the content clock does not call it stale. It is
worked out each time it is shown. An item ported here from another site reads "Ported here …
It must be proven on this site too."

**Port content panel (platform administrator).** Beside the Content calendar, a **Port
content** panel moves **proven** courses, range templates, scenarios and exercise templates
between sites as a signed bundle (golden templates travel separately, guide 06 §4):

- **Export:** **Choose content to export**, tick what to move (anything not proven is greyed
  with its reason), **Export proven content**. If anything selected, or anything it needs,
  is not proven, nothing is exported and the panel lists each item with its reasons.
- **Import:** optionally **Land as shared content**, then **Preview a bundle**. The preview
  shows each item as New, Update, Unchanged, Conflict, Refused or Skipped, with a diff.
  Tick **Replace what is here** on each conflict you accept, then **Import**. A bundle can be
  imported once. Everything lands not yet proven here; courses land unpublished.
- **This site's key → Show:** the public key an importing site must list in
  `tn_content_port_trusted_keys`. With none set the importing site refuses every bundle.
- Every account password in a range template is removed in the bundle: set the scenario
  passwords again on the new site before building from it.

Details and the offline command line: [content-port.md](../content-port.md).

Setting a content type's cycle (90–120 days), adding or renaming themes, and setting an
item's owner, themes or own cycle are **API only** on v1.2.0. Use `/content-review/cycles`,
`/content-review/themes` and `PATCH /content-review/items/{id}`. Shared themes and cycles
need the platform administrator.

---

## 12. Your daily job: approving class sessions

Every **class session** needs approval by an **admin or range ops** before anything happens.
Until then nothing is held, built, emailed or invited.

Approve them from the **Awaiting approval** panel. It is on **Prepare & Operate → Calendar →
Events** (side panel) and on **Prepare & Operate → Schedule**. Each item shows **Fits** or
**Doesn't fit**, the reasons, and the next free slot. Then:

- **Approve.** Capacity is re-checked. Under **Block**, a session that no longer fits is
  refused, with the next free slot.
- **Propose time** → **Day**, **From**, **To** → **Send proposal**. The time must fall inside
  the class's dates and outside maintenance windows. The instructor accepts or declines it.
- **Reject** → **Reason (the instructor sees it)** → **Reject**.

Below the sessions, **Roll-call additions that didn't fit** lists Students added after
approval when there was no room. **Approve** or **Reject** each one; a rejection needs a
reason.

A session nobody decides on is cancelled once its end time passes. The instructor (and the
class's lead) get a notification in the app (the bell) when you approve, reject or propose a
time, and an email as well if the site has email on. You are not notified of your own
decision.

---

## Using the API for the API-only steps

1. Get a token from a terminal with the device grant (`truenorth-cli` client), as described
   in [api.md](../api.md), "Keycloak OIDC JWT Flow". Approve the device code in a browser
   signed in as yourself.
2. Call `https://<site>/api/<path>` with `-H "Authorization: Bearer $TOKEN"`. Tokens last 5
   minutes.

Every change is audited under your name.

---

## Troubleshooting

- **"I sign in and land back on the login page."** The API is unreachable: check
  `https://<site>/api/health`.
- **"Everyone's access request shows no suggested role."** The token carries no `groups`
  claim. Fix Keycloak, then re-run `playbooks/60-keycloak.yml` ([identity.md](../identity.md),
  "The claim contract").
- **"An approval failed: a user with email … already exists from source ad."** Two identity
  subjects share one address. A person must sort it out; TrueNorth won't re-point an
  account.
- **"Instructor: every roll-call add is refused."** No roll-call domains are set and the
  tenant has no AD accounts yet. See [section 3](#3-set-the-roll-call-email-domains).
- **"After the second tenant, Publish / Promote / New Tenant say Failed or 403."** Nobody is
  platform administrator: see [section 0](#0-roles-and-the-one-you-need-to-be).

---

## Known gaps

| Gap | Workaround |
|---|---|
| Trust keys: no screen | `/result-transfer/trust-keys` |
| Catalogue import and enable/disable: no screen | `/golden-images/...` |
| Review cycles and themes: no screen | `/content-review/cycles`, `/content-review/themes` |
| Bulk CSV roster of people who must exist before they sign in: no screen | `POST /users/import-csv` |
| Approving a request into a different tenant: no screen | `tenant_id` on `POST /registration/requests/{id}/approve` |
| Observer consoles on class sessions are off, and the installer has no setting for them | `CLASS_OBSERVER_CONSOLES` on the API, see guide 04 |

---

## Printable checklist: administrator, first day

```
Site: ____________________   Date: __________   Admin: ____________________

[ ] Signed in as the bootstrap admin (Sign in with SSO); MFA enrolled
[ ] Platform tenant checked: tn_platform_tenant_id empty (bootstrap tenant) or the operator's tenant id
[ ] Rules of use: Admin → Rules of use → draft selected → edited → Save draft → Publish
    [ ] "Sessions may be observed" section kept
    [ ] Signed it myself (I agree)
[ ] Roll-call email domains set (Classes → Roll-call domains → Change): ____________________
[ ] Site time zone set (tn_site_timezone), or UTC accepted
[ ] Over-capacity policy checked (Schedule → Over-capacity policy): Block / Warn
[ ] Maintenance windows entered (Admin → Maintenance windows)
[ ] Golden images: each needed image has a promoted build
    (Infrastructure → Golden images → Promote, reason given)
[ ] Images ported from the test site, if any (06-operations-runbook.md)
[ ] tn_enclave_id = ______________   classification = ______________
[ ] Trust keys enrolled, if this site transfers results
[ ] Feeds: off / online / bundle; checked on Calendar → Content → Feeds
[ ] AD status sync: dry run read (Users → AD Sync → Dry run now)
    [ ] every name checked     [ ] tn_ad_status_sync_dry_run: false after ___ nights
[ ] Email: Configured / Not configured (banner explained to instructors)
[ ] Tenants and units checked; instructors, range ops and observers given their roles
[ ] Approval queue checked (Calendar → Events → Awaiting approval)
[ ] Content calendar opened; Stale now list reviewed
```
