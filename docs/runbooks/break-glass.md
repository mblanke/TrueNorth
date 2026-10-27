# Break-glass: MFA and sign-in recovery

Privileged users (admin, instructor, range_ops, observer) sign in with a password and an
authenticator-app code (docs/identity.md "MFA"). This runbook covers getting someone back
in **without ever turning MFA off**.

| Situation | Path |
|---|---|
| A user lost or replaced their phone / authenticator | [Reset the second factor](#reset-the-second-factor) |
| The only administrator is locked out | The same reset, run by whoever has SSH and sudo on the host |
| Keycloak is down or the realm is broken | [In-process harness](#keycloak-unavailable) |
| You need the Keycloak admin console | [Master realm admin](#the-master-realm-admin) |
| Everyone is stuck on the Rules of use page | [Rules of use](#rules-of-use) |
| A local account was disabled after 90 days unused | [The 90-day rule](#the-90-day-rule-and-the-break-glass-account) |

Every path is audited: Keycloak admin events (90 days, enabled by 60-keycloak) and a row
in TrueNorth's `audit_logs`.

## Reset the second factor

On the installer host, with the vault password:

```bash
cd /srv/truenorth/app/install   # the installer, in the app checkout (tn_app_dir)
ansible-playbook playbooks/breakglass-reset-mfa.yml -K --ask-vault-pass \
  -e tn_breakglass_user=maj.smith@corp.tnrange.lab \
  -e tn_breakglass_reason="lost phone, ticket 1234"
```

What it does (`install/roles/tn_keycloak/tasks/breakglass.yml`), through the Keycloak
Admin API on the host's loopback port only:

1. refuses unless both `tn_breakglass_user` and `tn_breakglass_reason` are given;
2. writes a TrueNorth audit row **first**: `action=mfa_reset`, `resource_type=user`,
   detail with the subject, the reason and the operator (`SUDO_USER`), via
   `scripts/audit_note_inprocess.py` in a one-off api container. If the row cannot be
   written, nothing is changed and the play fails; `-e tn_breakglass_no_audit=true` resets
   anyway, with a loud warning, leaving Keycloak's admin event as the only record (write
   it into the incident log);
3. deletes that user's OTP (and any WebAuthn) credentials;
4. for a **local** account, sets a fresh temporary password (Keycloak makes the user
   replace it at the next sign-in). The password is written first, root-owned 0600, to
   `<tn_state_dir>/breakglass-<upn>.password`, and only then set in Keycloak (a failed
   write changes nothing). Hand it over out of band, then **delete the file**
   (`sudo rm <tn_state_dir>/breakglass-<upn>.password`); `-e tn_breakglass_reset_password=false`
   skips this;
5. adds `CONFIGURE_TOTP` (and, for a local account, `UPDATE_PASSWORD`) to the user's
   required actions;
6. signs the user out of every session.

> **Until the user re-enrols, anyone who knows their password can sign in and enrol an
> authenticator of their own.** That is why a local account's password is replaced in
> the same step by a temporary one that only the person you hand it to knows. An **AD** account's password belongs to AD (read-only
> federation): reset it in AD at the same time as this playbook, and have the user sign in
> promptly.

It never changes the realm, the `tn-browser` flow or its binding. The user's next sign-in
asks for the password, then shows "Mobile Authenticator Setup": MFA is never off.

Tell the user: sign in, scan the new QR code, type the code; the app may ask for one more
code straight away (that is the step-up, docs/identity.md).

### Check it worked

- Keycloak: Realm `truenorth` > Events > Admin events shows `DELETE .../credentials/...`
  and `ACTION` on the user (needs the [master admin](#the-master-realm-admin)).
- TrueNorth: Admin > Audit log (platform administrator) shows `mfa_reset` for the user.

## Keycloak unavailable

If Keycloak is down or the realm is broken, nobody can sign in, privileged or not. Fix
Keycloak first (docs/runbooks/incident.md). Work that cannot wait goes through the
in-process harness, which needs root and Docker on the host and acts as a named
TrueNorth user without a token:

```bash
cd /srv/truenorth/app/infra/platform/docker
sudo docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production \
  run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v /srv/truenorth/app:/srcapp:ro \
  --entrypoint python api /srcapp/scripts/load_content_inprocess.py --admin <upn>
```

(Paths are the installer defaults, `tn_data_root: /srv/truenorth`.)

The harness identity carries `tn_session=inprocess` (exempt from the MFA rule, which
host root already outranks) and writes an `inprocess_session` audit row per user per run.

## Rules of use

Everyone signs the published rules of use before doing anything else (ADR 0014 §3), the
platform administrator included. The API refuses texts that render blank, and "I agree"
enables once the text has rendered, so this should not happen; if it does (a text nobody
can agree to, or the platform administrator unable to reach the admin page), on the host:

```bash
cd /srv/truenorth/app/infra/platform/docker
RUN="sudo docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production \
  run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v /srv/truenorth/app:/srcapp:ro --entrypoint python api"

# Let one person through (e.g. the platform administrator, to fix the text in the UI):
$RUN /srcapp/scripts/acknowledgement_breakglass_inprocess.py sign \
  --as <their email> --reason "<why, ticket>" --operator "$USER"

# Or supersede the bad version with the shipped default text (everyone signs it):
$RUN /srcapp/scripts/acknowledgement_breakglass_inprocess.py publish-default \
  --as <platform admin email> --reason "<why, ticket>" --operator "$USER"
```

Each writes an `acknowledgement_breakglass` audit row (operator, reason, version) beside
the usual `acknowledgement_sign` / `acknowledgement_publish` row. A published version
never changes; correct it afterwards by publishing a new one from Admin > Rules of use.
The in-process harness above (`tn_session=inprocess`) is itself exempt from the rules of
use, so scripted recovery work is never blocked by them.

## The 90-day rule and the break-glass account

The nightly AD status sync (docs/identity.md "AD status sync") disables TrueNorth accounts
whose local Keycloak account was not used for 90 days. The **break-glass** account, the
local account named by `AD_STATUS_SYNC_BREAK_GLASS` (the installer sets it to
`tn_bootstrap_admin_upn`), is never disabled, so it always works when the directory does
not. Every use of it raises the critical alert `BreakGlassAccountUsed` and writes a
`break_glass_sign_in` audit row: when it fires and nobody planned it, treat it as an
incident (docs/runbooks/incident.md) and reset its password with the playbook above.

Someone else's local account disabled by the rule: an administrator re-enables it in Users
(the 90 days start again). The sync never touches Keycloak, so the Keycloak account itself
was never disabled.

## The master realm admin

The Keycloak master realm administrator (`KEYCLOAK_ADMIN`, password in
`config/secrets/keycloak_admin_password`, escrowed by `backup.sh`) is **password-only**.
Compensating controls: it is published on `127.0.0.1:KEYCLOAK_ADMIN_PORT` only, nginx
answers 404 for `/auth/admin` and `/auth/realms/master`, so using it needs an SSH login
to the host (and an SSH tunnel for the console). This is a recorded risk acceptance
(docs/security.md). Applying `tn-browser` to the master realm is possible but adds a
lockout risk with no recovery path short of the database; not done in v1.2.0.

## Drill (staging)

Part of the staging acceptance (docs/runbooks/staging-acceptance.md, "MFA drill"):

1. Sign in as the demo or a test instructor; enrol an authenticator; confirm the app works.
2. Run the reset playbook for that user with a reason.
3. Sign in again: the authenticator setup page appears (not the code prompt).
4. Confirm the `mfa_reset` audit row and the Keycloak admin event.

`scripts/check_keycloak_mfa.sh` (CI) performs the same reset against the pinned Keycloak
image and proves step 3 on every change.
