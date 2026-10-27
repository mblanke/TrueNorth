# Identity

> How authentication, the AD claim contract and account creation fit together.
> For the human-facing journey, see [onboarding.md](onboarding.md).

---

## Model

Active Directory is the system of record for **who someone is**. TrueNorth is
the system of record for **what they may do here**. The two meet at Keycloak.

```
Active Directory (corp.tnrange.lab, on TN-DC01)
        │  LDAPS 636, READ_ONLY federation, bind as svc-keycloak-bind
        ▼
    Keycloak  ── realm: truenorth
        │  OIDC access token carrying the claim contract below
        ▼
   TrueNorth API ── users table, roles, tenants
```

TrueNorth never stores a password and never writes back to AD. `editMode` on the
federation component is `READ_ONLY` deliberately.

---

## Two dependencies, deliberately separate

`control-plane/api/app/auth.py` exposes two FastAPI dependencies:

| Dependency | Validates | Touches the DB | Used by |
|---|---|---|---|
| `get_token_identity` | The JWT, and nothing else | No | `/auth/me`, `/registration*` |
| `get_current_user` | JWT **and** resolves a `users` row | Yes | Everything else |

The second refusal is load-bearing. Access is gated on *having a row at all*, so
a pending registrant is rejected by every route that depends on
`get_current_user` — **including routes written in future**. That is a
structural guarantee rather than a convention someone has to remember.

> Do not add a `get_current_user_or_pending` variant. It would turn that
> guarantee back into something a new router can forget.

### Response contract

| Condition | Status | Body / header |
|---|---|---|
| Missing, invalid or expired token | **401** | — |
| Valid token, no account | **403** | `code: registration_required`, `X-TrueNorth-Auth-State: unregistered` |
| Valid token, request pending | **403** | `code: registration_pending`, state `pending` |
| Valid token, request declined | **403** | `code: registration_rejected`, state `rejected` |
| Account disabled | **403** | `code: account_disabled`, state `disabled` |
| Ordinary permission denial | **403** | *no* `X-TrueNorth-Auth-State` header |

**401 means the token. 403 with a state header means the account. 403 without
one means the permission.** Previously all of these were an indistinguishable
`403` separated only by prose in `detail`.

### The status oracle

`GET /auth/me` returns **200 for any valid token**, with a discriminated union
on `status`:

```jsonc
{"status": "registered",   "user": { … }}
{"status": "pending",      "request": { … }}
{"status": "rejected",     "request": {"decision_reason": "…"}}
{"status": "unregistered", "prefill": { … }, "suggestions": { … }}
```

The SPA routes off this rather than interpreting an error. The 403 variants
above exist so the ~35 pre-existing routers behave sanely when hit directly.

---

## The claim contract

This is the interface between the installer and the application, and the part
most likely to be quietly wrong.

`install/roles/tn_keycloak` creates a **`truenorth-identity` client scope**
attached to both `truenorth-web` and `truenorth-api` — one place, so the two
clients cannot drift apart. It carries three protocol mappers:

| Claim | Mapper | Read by | Missing ⇒ |
|---|---|---|---|
| `groups` | `oidc-group-membership-mapper` | `identity.suggest_role()`, `may_register()` | **No role suggestion on any approval screen, and group-based eligibility silently permits everyone** |
| `ad_object_guid` | `oidc-usermodel-attribute-mapper` ← `adObjectGuid` | `claims_to_prefill()` | `User.ad_object_guid` stays NULL; re-linking after an AD rename degrades to matching on email |
| `ad_distinguished_name` | `oidc-usermodel-attribute-mapper` ← `adDistinguishedName` | `claims_to_prefill()` | The approval screen cannot show OU placement |

`email`, `given_name` and `family_name` come from the built-in `profile` and
`email` scopes.

`adObjectGuid` and `adDistinguishedName` are populated on the Keycloak user by
`user-attribute-ldap-mapper` sub-components on the federation provider.
`objectGUID` is binary — without `is.binary.attribute: true` the value arrives
mangled.

**Nothing errors when these are absent.** The API simply sees no groups. That is
why `95-smoke-test` obtains a real token, decodes it, and asserts the claims are
present: it is the only check that catches "everything is green and role
suggestion has never worked".

### Audience and issuer (opt-in)

By default the Keycloak backend checks signature, `kid`, algorithm (RS256) and
time claims, but **not** `aud` or `iss`: a token issued to any client in the realm
is accepted. Two settings on the API tighten that. Both are off when unset, except
that with `TN_ENV=production` the API refuses to start without `KEYCLOAK_AUDIENCE`
(`OIDC_AUDIENCE` for `AUTH_BACKEND=generic_oidc`) and always verifies `aud`
(`app/settings.py`). Add the mapper below before setting `TN_ENV=production`.

| Setting | Effect | Prerequisite |
|---|---|---|
| `KEYCLOAK_AUDIENCE=truenorth-api` | `aud` must include it | Keycloak puts only `account` in `aud`. Add an **Audience** mapper (`oidc-audience-mapper`, *Included Client Audience* `truenorth-api`, *Add to access token* on) to the `truenorth-identity` scope or to `truenorth-web`'s dedicated scope **first**, or every user is locked out |
| `KEYCLOAK_ISSUER=https://<public-host>/realms/truenorth` | `iss` must equal it | The realm's **public** URL as the browser sees it. `KEYCLOAK_URL` (e.g. `http://keycloak:8080`) is the internal address and will not match |

Verified 2026-10-04 against Keycloak 24: with the mapper, `aud` is
`["truenorth-api", "account"]` and the token is accepted; the internal URL as
`KEYCLOAK_ISSUER` is refused. Roll out by adding the mapper, confirming a fresh
token's `aud` with the smoke test, then setting the variables.

### Signing-key rotation

The API caches the realm's JWKS (`app/auth_backends/jwks_cache.py`, both the Keycloak
and the generic OIDC backend). It refreshes after `JWKS_CACHE_TTL_SECONDS` (600) and
at once when a token names a `kid` it has not seen, which is what a Keycloak key
rotation looks like, so logins keep working without an API restart. Unknown-`kid`
refreshes are throttled to one per `JWKS_MIN_REFRESH_SECONDS` (30); if a refresh fails
the cached keys stay in use.

---

## AD groups → roles

Configured with `REGISTRATION_GROUP_ROLE_MAP` (JSON), defaulting to the groups
created on TN-DC01:

| AD group | Suggested role |
|---|---|
| `TN-Platform-Admins` | `admin` |
| `TN-Range-Operators` | `range_ops` |
| `TN-Instructors` | `instructor` |
| `TN-Content-Authors` | `instructor` |
| `TN-Students` | `student` |
| `TN-Observers` | `observer` |

Multi-group membership resolves by an explicit precedence list
(admin > range_ops > instructor > observer > student), so the outcome is
deterministic rather than dependent on set ordering.

**A suggestion is not a grant.** `REGISTRATION_ALLOWED_GROUPS` gates who may
*apply*; the role on the account is whatever the approver selects. A member of
`TN-Platform-Admins` who is approved as `student` is a student.

**Auth zone at registration.** `REGISTRATION_AUTH_ZONE` (installer:
`tn_registration_auth_zone`) names the auth zone whose clearance and IP rules apply
when someone registers. Empty (the default) applies none. There is no default zone:
before v1.2.0 an unset zone meant "the first active zone by name", which on every
install was the seeded "Secret Zone", so registration demanded MFA and a `secret`
clearance claim that nothing emits. A name that matches no active zone refuses
registration. Registration never applies a zone's MFA rule: an identity with no
account has no role yet, and the role decides MFA.

The other direction follows: **removing someone from an AD group does not change their
TrueNorth role.** Their `groups` claim drops (within Keycloak's changed-users sync, 300 s),
but the role was granted by an approval and stays until someone changes it here. To
revoke TrueNorth access, disable or re-role the TrueNorth account, or disable the AD
account (sign-in then stops). Disabling or deleting their AD account also deactivates
the TrueNorth account, after two nightly
[AD status sync](#ad-status-sync-nightly) runs. Leaving the TN-* groups does not.

**Nested groups are not expanded.** The federation reads direct membership
(`LOAD_GROUPS_BY_MEMBER_ATTRIBUTE`): a user in a group that is a member of `TN-Students`
carries that group, not `TN-Students`.

**An email match never takes over another person's staff account.** Registration
approval adopts a pre-provisioned roster row by email, but never one holding `admin`,
`instructor` or `range_ops` (409), whoever approves. The bootstrap step
(`app.bootstrap_admin`, 80-seed) adopts by email only a roster row or an earlier `admin`
row (the nominated administrator after a realm rebuild), never an `instructor` or
`range_ops` row (it stops with an error). Keycloak itself refuses to import an AD user
whose email a local account already has. All of this is measured against a directory by the CI `ad` lane
(`tools/ad-test-dc/README.md`).

---

## AD status sync (nightly)

ADR 0014 decision 6, `app/ad_status_sync`. TrueNorth **reads** the directory and never
writes to it: only an AD administrator disables or removes an AD account (AD itself
disables one after 90 days unused).

**What it reads.** Keycloak's view of AD, through the Admin API with the same
`KEYCLOAK_ADMIN_CLIENT_ID` client as `/ad-sync`, GET only: every realm user (`enabled`,
`federationLink`), paged. AD's disabled flag reaches Keycloak's `enabled` through the
`msad-user-account-control` mapper that vendor `ad` adds; deleted AD users leave Keycloak at
its full sync (hourly). Group membership is not read. No LDAP connection, and no Keycloak
write: the tests assert on the HTTP transport.

**Who is deactivated** (`users.is_active = false`, the same as `DELETE /admin/users/{id}`):

| Keycloak says | Reason |
|---|---|
| AD account (`federationLink`) disabled | `ad_disabled` |
| No user with the account's id, nor exactly one AD user with its address (a rebuilt realm), for a known AD account (`users.ad_object_guid` set from the directory's claims) | `ad_removed` |
| Local account (no `federationLink`) unused for 90 days, when a break-glass account exists | `local_unused` |

Never: anyone still enabled in AD (TN-* groups or not: roll-call Students are admitted
without one, `classes/onboarding.py`); the **break-glass** account (below); the tenant's
last active admin; result-transfer subjects (`source = subject`, never judged by any rule
nor counted as eligible); accounts Keycloak does not know that are not known AD accounts
(CSV roster rows, LTI Students, and local accounts after a realm rebuild: registration marks
every account `source = ad`, so `source` proves nothing).

**What deactivating does.** In one transaction per account: the guarded update (so any
number of replicas, or a "run now" overlapping the night's run, deactivate it once); each
of their places on the roll call of an open, unfinished class is removed with the classes'
own removal (seat freed in every approved session, teams of sessions not started, their
ranges at the next range tick, a CANCEL of sessions to come); an `ad_status_deactivate`
audit row. After the commit, each class's lead and assistants (the tenant's admins when it
has none) get one email per class. **Nothing is deleted**: the account row, enrolments,
results, roll-call entries (marked removed) and audit history stay. An administrator
re-enables the account in Users; the next runs deactivate it again only if the directory
still says so. Re-enabling restarts a local account's 90 days.

**Last use** of a local account is TrueNorth's own record, `user_last_seen`, written by
`auth.get_signed_in_user` (every authenticated request, after the disabled and MFA checks)
at most once an hour per account and process. Not Keycloak's LOGIN events: they are kept
exactly 90 days (`eventsExpiration`), so 89 days unused and never used look alike; reading
them needs `view-events`; and an SSO refresh is not a LOGIN. An account with no row counts
from the later of its creation and the **first sync run**, so the time before the record
existed never counts.

**Break-glass** is a *local* Keycloak account (no `federationLink`) whose username is in
`AD_STATUS_SYNC_BREAK_GLASS` (comma-separated). The installer renders the bootstrap
administrator's UPN there only with AD **and** `tn_keycloak_local_admin` (the local recovery
account `install/roles/tn_keycloak/tasks/local_identity.yml` keeps beside AD); otherwise it
is empty. A configured name that is not a local Keycloak user (the local administrator
turned off, the name now an AD account's) is no break-glass. It is never disabled; its use
is announced instead, at most once an hour per API process: the Prometheus alert
`BreakGlassAccountUsed` (critical; `truenorth_break_glass_sign_ins_total`), a
`break_glass_sign_in` audit row and a WARNING log. **With no break-glass account the 90-day
rule does not run** (so not on an install without AD): disabling local accounts without
knowing which one is the recovery account could lock everyone out. When a name is
configured but no local Keycloak user has it, the panel says so ("Break-glass account not
found ... the 90-day rule for local accounts is off") rather than switching off silently.

**When and where.** Every API replica ticks (`AD_STATUS_SYNC_TICK_SECONDS`, 300); from
`AD_STATUS_SYNC_HOUR_UTC` (2) the night's run is claimed by inserting the unique slot
`nightly:<UTC date>` into `ad_status_sync_runs`, so one replica runs it, with or without
Redis. `AD_STATUS_SYNC_ENABLED=false` turns the clock off; it idles while the Admin API
client is not configured.

**Safety**, per tenant:
- `AD_STATUS_SYNC_DRY_RUN` (default **true**; installer `tn_ad_status_sync_dry_run`): runs
  record who they would deactivate and change nothing, and the API refuses (409) a request
  for a real run. Turn it off once a few nights look right. **Turning it off acts on the
  next night**: that night's run is the first applied one, and it deactivates the accounts
  the last dry-run night also found, for the same reason (below). Nobody is deactivated the
  moment the setting changes.
- **Confirmed by the night before**: an account is deactivated only when a good run
  covering its tenant (dry, held or applied) that **finished 12 to 36 hours before this one**
  found the same account **for the same reason**. A dry "run now" followed by a real one
  confirms nothing (too close), a finding from months ago confirms nothing (too old), and a
  user missing from one page of Keycloak's answer one night keeps their account. A "run now"
  can apply what last night found. The panel counts the rest as "found once".
- **Held, per tenant and rule family**: the AD rules (`ad_disabled`, `ad_removed`) are judged
  against the tenant's AD accounts, the 90-day rule against its local accounts under that
  rule, so neither dilutes the other (nor do roster rows, LTI Students or subjects). A family
  that would deactivate more than max(`AD_STATUS_SYNC_MAX_FLOOR` (3),
  `AD_STATUS_SYNC_MAX_FRACTION` (0.25) x its accounts) changes nothing that run; other
  families and tenants go on. The held message shows that tenant's numbers only. "Run now,
  over the limit" releases the caller's tenant.
- **Last admin**: never the tenant's last active admin. The check locks the tenant's active
  admin rows (`SELECT ... FOR UPDATE`) and counts inside the same transaction, so two
  deactivations at once cannot both pass; `DELETE /users/{id}` has the same guard (409).
- Keycloak unreadable: the run **fails** and changes nothing.

**The panel.** Users > AD Sync (`GET /ad-status-sync`, admin): the mode, the break-glass
account and its last use, the last run with this tenant's counts and accounts, and "Dry
run now" / "Run now" (`POST /ad-status-sync/runs`, the caller's tenant only, audited
`ad_status_sync_run`; "Run now, over the limit" after a held run). A nightly run covers
every tenant; each tenant's administrators see only their own accounts.

---

## Role vocabulary

The backend enum is the source of truth:

```
admin | instructor | student | observer | range_ops
```

The frontend previously used `trainee`, which exists nowhere server-side.
`UserRole` is a native Postgres enum baked into `ROLE_PERMISSIONS` and the
`UserCreateIn` regex, so renaming it would mean an `ALTER TYPE` plus reseeding
every row — the frontend moved instead. "Trainee" survives as a display label.

A legacy `trainee → student` alias remains in the group map so an old token
still resolves.

---

## Frontend

`keycloak-angular` 15 + `keycloak-js` 24, version-matched to the Keycloak 24
server pinned in `compose.prod.yml`.

The previous hand-rolled flow built an authorize URL with **no `code_challenge`**
while the realm requires PKCE `S256` — Keycloak rejected it outright. It was not
an unfinished flow; it was one that could not work. Finishing it by hand would
have meant owning WebCrypto PKCE, state/nonce, the token exchange, a refresh
timer and `id_token_hint` logout.

> **Keep both pinned at 24.** `keycloak-js` 25+ deprecates the silent-check-sso
> iframe this configuration relies on.

Tokens come from the adapter, never from `localStorage` — the interceptor used
to read an `access_token` key that nothing in the app ever wrote, so every
authenticated request went out bare.

### Guards

`app.routes.ts` previously had **zero** `canActivate` entries. Now:

| Guard | Applies to |
|---|---|
| `authGuard` | Everything except `login`, `register`, `registration-pending` |
| `registrationGuard` | `/register` — only for identities with no account |
| `pendingGuard` | `/registration-pending` |
| `onboardingGuard` | Dashboard, learning, exercises — **students only** |
| `instructorGuard` | Authoring, scoring, MESL, ops-center |
| `adminGuard` | Admin, users, integrations |

Guards resolve asynchronously against `AuthService.ready$`, because `/auth/me`
is a network call and a guard can run before it lands. Reading the signal
synchronously would bounce a legitimately signed-in user to `/login` on a hard
refresh.

`onboardingGuard` is scoped to `role === 'student'` on purpose — trapping an
administrator behind a trainee profile wizard on their first login would be
both wrong and infuriating.

---

## MFA (multi-factor sign-in, v1.2.0)

**Who.** `admin`, `instructor` (including `TN-Content-Authors`), `range_ops` and
`observer` sign in with a password **and** a one-time code from an authenticator app
(TOTP). Students sign in with a password only; LTI-launched Students never see Keycloak
and are not affected. The list is `TN_MFA_ROLES` (installer `tn_mfa_roles`); production
refuses to start unless all four are in it.

**Two halves, one release.**

| Half | What | Where |
|---|---|---|
| Keycloak asks | Realm role `tn-mfa-required` on the groups whose role is privileged; the `tn-browser` flow asks those users for an OTP code, and sends anyone without one to enrolment (`CONFIGURE_TOTP`). The `amr` protocol mapper puts how the user signed in in the token. | `scripts/keycloak_mfa.py`, run by `install/roles/tn_keycloak/tasks/mfa.yml`; spec `install/roles/tn_keycloak/files/mfa-spec.json` |
| The API enforces | `auth.get_current_user` (every route and every WebSocket) refuses a privileged **database** role whose token shows no second factor: 403 `{"code": "mfa_required"}`, header `X-TrueNorth-Auth-State: mfa_required`. MFA means `amr` contains `otp`/`hwk`/`swk`/`mfa`, or `acr` is `mfa`. `acr` `"0"`, `"1"` or `"pwd"` never counts. | `control-plane/api/app/mfa_policy.py` |

The role is the database's (`users.role`), never the token's. If an administrator
promotes someone in the UI before their Keycloak groups change, the API refuses them and
the SPA steps up (below): one extra prompt, never access without a second factor.

**Enrolment on next sign-in.** After the upgrade, every privileged user, the bootstrap
administrator included, is shown "Mobile Authenticator Setup" at the next sign-in: scan
the QR code with an authenticator app (FreeOTP, Google Authenticator, Microsoft
Authenticator, ...), type the code, then continue. On Keycloak 24 the code typed while
enrolling is not recorded as a second factor in that first token, so the SPA asks for one
more code straight away (step-up, no password). Every later sign-in: password, then code.

**Step-up.** On a 403 `mfa_required`, or when `/auth/me` reports `mfa_required: true`
and `mfa_satisfied: false`, the SPA signs in again asking for `acr` `mfa`
(`keycloak.login({acr: {values: ['mfa'], essential: true}})`); the realm's level-2 subflow
asks for the code. One step-up per five minutes: if the token still does not satisfy the
API, the app shows `/mfa-required` ("contact your administrator") instead of looping.

**WebAuthn (security keys).** Not in v1.2.0. On Keycloak 24, offering WebAuthn or OTP as
alternatives to a user with neither configured ends on an error page rather than
enrolment (proven by `scripts/check_keycloak_mfa.sh`), so the flow is OTP only. The API
already accepts `amr` `hwk` for when it is added.

**Exempt, and audited.**
- The in-process harness (`scripts/_inprocess_api.py`: content loading, ARC² batch,
  staging acceptance) acts with `tn_session="inprocess"`. It needs root and Docker on the
  host; each user it acts as gets an `inprocess_session` audit row. An IdP token can never
  carry `tn_session` (`auth.token_from_idp_claims` strips it).
- Test hosts only: with `tn_mfa_demo_exempt: true`, on a host with `tn_arc2_auto_accept`
  (`TN_TEST_HOST`) and `tn_load_shipped_demo`, the shipped demo staff
  (`*.demo@truenorth.test`) hold the realm role `tn-mfa-exempt` and the API sets
  `TN_MFA_DEMO_EXEMPT`. The API ignores it without `TN_TEST_HOST`, production refuses to
  start with it and without `TN_TEST_HOST`, and every use writes an `mfa_demo_exempt`
  audit row (one per user per day per API process). Off by default; without it, the demo
  instructor enrols an authenticator once and the break-glass playbook resets it.
- `AUTH_DISABLED` (dev, CI e2e, itest, cmi5 lanes) is unchanged: no token, no MFA.

**Break-glass.** A lost authenticator is reset with
`playbooks/breakglass-reset-mfa.yml` (`docs/runbooks/break-glass.md`). There is no HTTP
path around MFA and no password-only privileged account in the `truenorth` realm. The
Keycloak **master** realm admin stays password-only: it is reachable only on the host's
loopback port (nginx answers 404 for `/auth/admin` and `/auth/realms/master`), so using
it needs an SSH login to the host. That is a recorded risk acceptance.

**Startup check.** In production the API reads the realm through its Admin API client
(view-realm) at startup and refuses to start when the realm answers without the
`tn-browser` flow, `acr.loa.map` "mfa" or the `tn.mfa.configured` marker that
`scripts/keycloak_mfa.py` writes last (`TN_MFA_KEYCLOAK_CHECK`: refuse | warn | off;
the installer sets warn, Helm keeps refuse and configures the realm first with its
`keycloak-mfa` pre-install/pre-upgrade hook, which runs the same script and, with
`config.keycloak.adminClientId`, creates that client; infra/k8s/README.md). A Keycloak that does not
answer is logged and never blocks the start.

**Proof.** `scripts/check_keycloak_mfa.sh` (CI job `keycloak-mfa`) runs the pinned
Keycloak image, applies `keycloak_mfa.py` twice (the second run must change nothing) and
drives real browser sign-ins: enrolment forced for privileged groups, `amr`/`acr` in
tokens, refreshes and SSO re-entry, Students password-only, step-up, the demo exemption,
and the break-glass reset. `tests/api/test_mfa_policy.py` covers the API rule.

The smoke test (95-smoke-test) no longer signs in as the bootstrap administrator: without
AD it uses the local, non-privileged `tn_smoke_local_user` (TN-Observers, never
registered; password `config/secrets/smoke_user_password`); with AD, name a
non-privileged account with `-e tn_smoke_username=... -e tn_smoke_password=...`.

---

## Data model

`registration_requests` is a separate table, not a half-formed `users` row.

`users.tenant_id` is `NOT NULL` and `email`/`keycloak_id` are unique, so a
pending person could only live in `users` behind a placeholder tenant that every
one of the ~46 tenant-scoped lookups would then have to exclude. Keeping them
out is what makes the enforcement above structural.

Onboarding state is three columns on `users` (`onboarding_state`,
`onboarded_at`, `onboarding_data`) rather than a fourth table: the state machine
is linear and strictly one row per user, and `/auth/me` runs on every page load
and should not join for it.

Migration: `a3b4c5d6e7f8`.
