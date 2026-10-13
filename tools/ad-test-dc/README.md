# AD test DC (plan M2)

An isolated Samba 4 Active Directory domain controller, its own test CA, and the
Keycloak TrueNorth pins, for qualifying directory-backed sign-in. It is the fixture of
the CI `ad` lane and runs the same way on a laptop. It never joins, trusts or talks to
a production domain, and it is not deployed on staging or any shared host.

The same tests also run against a real directory ("External DC mode" below): that is
how the owner's Windows AD DS / AD CS DC is qualified on staging. Passing here means
**Samba-tested**; only an external-mode pass against Windows means
**Windows AD / AD CS qualified** (docs/release-notes/v1.2.0.md keeps the two apart).

## What it builds

| Piece | What |
|---|---|
| `dc` | Samba 4.17 AD DC (Debian bookworm-slim, pinned by digest). Realm `TNLAB.TEST`, NetBIOS `TNLAB`, host `dc01.tnlab.test`. TN-DC01's layout: `OU=TrueNorth` with `Users`, `Groups`, `ServiceAccounts`; groups `TN-Platform-Admins`, `TN-Range-Operators`, `TN-Instructors`, `TN-Content-Authors`, `TN-Students`, `TN-Observers`, plus `TN-Cohort-Alpha` (a member of `TN-Students`: the nested case); bind account `CN=TrueNorth Keycloak Bind,OU=ServiceAccounts,…` (`svc-keycloak-bind`). LDAPS only, certificate from the test CA. |
| accounts | `accounts.tsv`: one per TN-* group, `ad.multi` (Students + Instructors), `ad.nested` (only in TN-Cohort-Alpha), `ad.mover` (group-removal test), `ad.disabled` (disabled), `ad.collision` (TN-Platform-Admins, with the local recovery administrator's email). |
| `rogue` | The same image presenting a certificate from a CA nothing trusts, in front of the DC's LDAPS (untrusted-CA test). |
| `misnamed` | The same front end with a certificate from the **trusted** test CA issued to `dc99.tnlab.test`, reached as `misnamed.tnlab.test` (hostname-verification test). |
| decoy | A second group whose cn is `TN-Platform-Admins`, in `OU=Legacy,OU=Groups,OU=TrueNorth`, holding `ad.nested`. Without the mapper's DN filter it gave ad.nested a TN-Platform-Admins claim (measured). |
| `keycloak` | `quay.io/keycloak/keycloak:24.0@sha256:…` as compose.prod.yml pins it, trusting only the test CA (`KC_TRUSTSTORE_PATHS`). |

Everything is published on **127.0.0.1 only** (LDAPS 16360, Keycloak 18480); 389, 88 and
53 are not published at all. Containers, network and volume belong to compose project
`tn-ad-test`, so `down` removes exactly what `up` made.

## Secrets and keys

`run.sh secrets` writes one password per account into `build/ad-test-dc/secrets/`
(directory 0700, files 0600, gitignored), generated with Python's `secrets`. Nothing
prints them: the DC reads them from a read-only mount, the federation playbook reads the
bind password with `no_log`, and the tests read them from the files.

`run.sh certs` makes the test CA. Its private key (and the rogue CA's) stays in
`build/ad-test-dc/ca-private/` (0600) and is never mounted into any container; the DC
gets only its own pair and the CA's public certificate, Keycloak only the public
certificate.

## Use

```
tools/ad-test-dc/run.sh up         # build and start; waits for the DC's LDAPS and Keycloak
tools/ad-test-dc/run.sh federate   # realm + LDAP federation via install/roles/tn_keycloak
tools/ad-test-dc/run.sh test       # tests/integration/test_ad_directory.py
tools/ad-test-dc/run.sh down       # or `clean` to also delete the passwords and CA keys
```

`run.sh all` does `secrets certs up federate test`. Set `PY` to the Python with
`requirements-test.txt` (e.g. `PY=.venv/bin/python`); `federate` needs `ansible-playbook`
(ansible-core, no collections). `AD_PROPAGATION_WAIT=1` also measures the periodic
changed-users sync (up to `changedSyncPeriod` + 2 min). Measurements land in
`build/ad-test-dc/ad-report.json` (the CI lane uploads it).

`federate.yml` runs the installer's own task files — `local_identity.yml` (the local
recovery administrator, first), `ldap_federation.yml`, `identity_scope.yml`,
`ldap_sync.yml` — with this fixture's DNs and secrets, so the lane tests the federation
60-keycloak builds, not a copy.

## What it proves (and the measured numbers)

| Check | Result on Samba (2026-10-12) |
|---|---|
| LDAPS chains to the supplied CA; a different CA is refused | handshake OK / `SSLCertVerificationError` |
| Keycloak binds through its truststore; a front end from an untrusted CA fails | 204 / 400 (`testLDAPConnection`) |
| a certificate from the trusted CA for another hostname | refused by Keycloak (400) and by a client (`SSLCertVerificationError`) |
| a same-named TN-* group outside `OU=Groups` | never counts (`groups.ldap.filter` = the exact TN-* DNs) |
| installer: a TN-* group missing from AD | 60-keycloak fails ("found 5 of the 6"), even with the local TN-* groups present |
| installer: an edited sub-mapper (LDAP_ONLY, filter removed) | put back on the next run |
| installer: `tn_keycloak_local_admin` turned off | local admin disabled (loud warning), AD admins unaffected; on again re-enables it |
| group → suggested role, for every TN-* group; several groups → highest | as `tn_registration_group_role_map` |
| tenant binding (`REGISTRATION_GROUP_TENANT_MAP`) → approval lands there | registered, `source=ad`, objectGUID kept |
| nested groups | **direct membership only**: TN-Cohort-Alpha ⊂ TN-Students gives no TN-Students claim (and, outside the TN-* DNs, no TN-Cohort-Alpha claim), no suggestion, registration refused (403) |
| disabled AD account | no token (`Account disabled`) |
| disabling a signed-in user | new sign-in refused at once; refresh token works until the next changed-users sync, then `User disabled` |
| removing a group | claim drops at the next token once Keycloak re-reads the user: at once from a fresh load; with the user cached, at the periodic changed-users sync (`changedSyncPeriod` 300 s; measured 93 s and 287 s on two runs, the timer's phase). **The TrueNorth role does not change**: groups suggest, approval grants |
| email collision (AD admin with the local administrator's email) | Keycloak refuses to import the AD user (`Invalid user credentials`); the local admin keeps its subject, groups and TrueNorth row; an approval never adopts a staff row by email (409) |
| local account over an AD email | Keycloak refuses (409) — why 60-keycloak creates the recovery admin before federating |
| Moodle SSO ticket | `sub` = TrueNorth user id, never the Keycloak subject, objectGUID or username |
| DC stopped | local recovery admin signs in (0.14 s); an AD user fails in ~5 s (HTTP 500, `connectionTimeout`); recovers when the DC is back |
| MFA for AD staff | skipped, TODO: `app.mfa_policy` is not on main (S-track) |

Revocation bound for a directory change: `changedSyncPeriod` (300 s) for Keycloak to
re-read the user, plus up to `accessTokenLifespan` (300 s) for tokens already issued.
The API validates tokens locally (JWKS) and does not ask Keycloak per request.

## External DC mode (Windows AD on staging)

Run the same file with `AD_MODE=external` and point it at the real Keycloak and DC.
Every check whose inputs are missing skips and names the variable.

```
AD_MODE=external
AD_KC_URL=https://tn-staging.corp.tnrange.lab/auth   # or http://127.0.0.1:8180/auth on the host
AD_KC_CLIENT=<a client with the password grant and the truenorth-identity default scope>
AD_KC_CLIENT_SECRET_FILE=<its secret, if confidential>
AD_KC_ADMIN_PASSWORD_FILE=<master admin password file>     # federation checks; optional
AD_SECRETS_DIR=<dir of 0600 files named by sAMAccountName, plus local-admin>
AD_USER_ADMIN=… AD_USER_STUDENT=… (one per key; see the test module docstring)
AD_LOCAL_ADMIN_UPN=tnadmin@tn-staging.local
AD_LDAPS_ADDR=<dc>:636  AD_DC_HOST=<dc fqdn>  AD_CA_FILE=install/files/corp-root-ca.cer (PEM)
AD_GROUP_REMOVE_CMD / AD_GROUP_ADD_CMD / AD_USER_DISABLE_CMD / AD_USER_ENABLE_CMD
    e.g. ssh dc 'powershell Remove-ADGroupMember -Identity {group} -Members {user} -Confirm:$false'
.venv/bin/python -m pytest tests/integration/test_ad_directory.py -m integration -rs
```

Test accounts the Windows DC needs for full coverage: one per TN-* group, a user in two
groups, a user only in a group nested inside TN-Students, a disabled user, a user to
move, and one in TN-Platform-Admins whose `mail` equals `AD_LOCAL_ADMIN_UPN`. Run it
from a host that already trusts nothing it should not: the tests never write the
directory except through the commands you give them, and they restore what they change.
