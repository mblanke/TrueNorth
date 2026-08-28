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
