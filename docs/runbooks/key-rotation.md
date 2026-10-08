# Runbook — key and secret rotation

Every rotation: take a backup first (`scripts/backup/backup.sh`, exit 0), change one secret
at a time, verify, then record the date here or in the change log. The env file is
`/srv/truenorth/config/.env.production` (rendered by the installer's `tn_config` from the
vault — change the vault too, or the next installer run reverts you).

```bash
dc() { docker compose -f /srv/truenorth/app/infra/platform/docker/compose.prod.yml \
                      --env-file /srv/truenorth/config/.env.production "$@"; }
```

## TN_SECRETS_KEY (stored-credential sealing key)

Seals hypervisor passwords/API tokens and AI engine keys in PostgreSQL (`app/secretbox.py`,
Fernet). It is a comma-separated list: the **first** key seals, **every** key unseals.

1. Generate: `openssl rand -hex 32`.
2. Put the new key first, keep the old one: `TN_SECRETS_KEY=<new>,<old>` — in the vault
   (`vault_secrets_key`) and the env file. The api and **every** worker must get the same value.
3. Restart the services that read it: `dc up -d api worker-provision worker-scenario worker-telemetry`.
   New and re-saved values are now sealed with `<new>`; old ones still open.
4. Re-seal: re-save each credential (admin UI: hypervisor connections, AI engines), or run
   the one-off re-seal if the release has one. Check none is still sealed only with `<old>`:
   remove `<old>` on a staging copy of the database and open each connection, or ask the
   release notes for the verification query.
5. Take a backup **with escrow** (its `secrets/env.enc` now holds `<new>,<old>`).
6. Drop the old key: `TN_SECRETS_KEY=<new>`, restart as in 3, test a provisioning run (it
   unseals the hypervisor credential), back up again.

Never drop `<old>` before step 4 is done — anything still sealed with it becomes unreadable
(`SecretUnreadableError`) and must be re-entered by hand. Backups taken before the rotation
need the old key: keep it in escrow until those backups have aged out.

## JWKS / signing keys

**Keycloak realm keys** (sign user access tokens; the API verifies them against
`<KEYCLOAK_URL>/realms/<realm>/protocol/openid-connect/certs`).

1. Keycloak admin → realm `truenorth` → Realm settings → Keys → Providers → add a new
   `rsa-generated` provider with a **higher priority** than the current one. Both keys are
   now published; new tokens are signed with the new one.
2. The API caches the JWKS **per process and never refreshes it**, so it will reject tokens
   signed with the new key until restarted: `dc restart api` (and anything else validating
   tokens). Do this immediately after step 1.
3. After the longest token/session lifetime (realm → Sessions/Tokens; default ≤ 10 h for SSO
   sessions), set the old provider to passive, then delete it a week later.

**LTI 1.3 tool key** (TrueNorth signs LTI messages; platforms such as Moodle fetch
`GET /api/lti/jwks`). One active row in `lti_tool_keys`; a new keypair is generated lazily
when none is active.

1. Mark the current key inactive:
   `dc exec -T postgres psql -U <user> -d <db> -c "update lti_tool_keys set is_active=false where is_active"`.
2. The next LTI call generates and publishes a new `kid`. The JWKS publishes only the active
   key, so there is no overlap: messages signed with the old key stop verifying at once.
   Platforms configured with the keyset URL pick up the new `kid` when they re-fetch it; a
   platform configured with a pasted public key must be updated in the same window.
   Do this in a quiet period.
3. Test one launch from each registered platform.

## Other secrets

| Secret | Where | How |
|---|---|---|
| `POSTGRES_PASSWORD`, `KEYCLOAK_DB_PASSWORD`, `LRS_DB_PASSWORD` | env + vault | `ALTER ROLE <role> PASSWORD '<new>'` via `dc exec -T postgres psql`, update env, `dc up -d` the dependants (pgbouncer, api, workers, keycloak, lrs). Note: a restore of an older backup resets role passwords to that backup's (see backup-restore.md). |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | env + vault | root credentials: update env, `dc up -d minio api worker-*`. Backups read them from the env file, nothing else to change. |
| `REDIS_PASSWORD` | env + vault | update env, `dc up -d redis api worker-* flower` (Redis content is transient). |
| `JWT_SECRET`, `CSRF_SECRET` | env + vault | update env, `dc up -d api`; users are logged out. |
| Backup escrow key pair | `BACKUP_ESCROW_PUBKEY` on the host; private key offline | new pair per backup-restore.md; switch the public key; keep the old private key until every backup sealed to it has aged out (the manifest's `recipient_sha256` says which). |
| Hypervisor / AI engine credentials | sealed in DB | re-enter in the admin UI after rotating them at the source. |

If a secret was **exposed**, treat it as an incident ([incident.md](incident.md)): rotate
first, then look for copies (logs, `docs/`, old backups, CI artifacts).
