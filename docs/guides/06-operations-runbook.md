# 06 — Operations runbook

**For:** whoever runs TrueNorth after it is installed: the platform administrator and the
range operators who look after the VM, vCenter and the network.
**Assumes:** an install done with [02 — Install guide](02-install-guide.md): the installer
checkout in `~/truenorth` on the TrueNorth VM, the site inventory in
`~/truenorth/install/inventory/site.yml`.

| Section | When |
|---|---|
| [1. Routine checks](#1-routine-checks) | Daily and weekly |
| [2. Upgrade a release or release candidate](#2-upgrade-a-release-or-release-candidate) | A new release is approved for this site |
| [3. Backup, restore and the restore drill](#3-backup-restore-and-the-restore-drill) | Set up once; drill quarterly; on host loss |
| [4. Port golden templates and content](#4-port-golden-templates-and-content) | After the install, and whenever the test site promotes a new build |
| [5. Common failures and their fixes](#5-common-failures-and-their-fixes) | Something is wrong |
| [6. The range isolation check](#6-the-range-isolation-check) | Before Students use ranges; when the network team signs off |
| [7. Content feeds](#7-content-feeds) | Daily (online) or per bundle (air-gapped) |
| [8. Placement and port mirroring](#8-placement-and-port-mirroring) | SOC ranges miss traffic |
| [9. AD status sync: dry run, then enable](#9-ad-status-sync-dry-run-then-enable) | The first nights after install |

Deeper references, which this guide points into rather than repeats: `install/README.md`
(every installer variable), `docs/operations.md` (alert rules, range leases, Greyspace),
`docs/runbooks/` (backup-restore, upgrade, break-glass, key-rotation, incident, on-call).

Throughout, `dc` is this shell helper on the TrueNorth VM:

```bash
dc() { docker compose -f /srv/truenorth/app/infra/platform/docker/compose.prod.yml \
                      --env-file /srv/truenorth/config/.env.production "$@"; }
```

Never stop processes with `pkill -f <pattern>`: the pattern also matches your own shell and
whatever else has it in its command line. Stop a service with `dc stop <service>`, a
process by its PID.

---

## 1. Routine checks

Daily (five minutes):

- [ ] **Admin → System Health**: every service healthy.
- [ ] Firing alerts. Email is parked (02 §6.4), so read them on the VM:

  ```bash
  docker exec tn-alertmanager amtool --alertmanager.url=http://localhost:9093 alert query
  ```

  Empty output is good. What each alert means and what to do: `docs/operations.md` "Alert
  Response Procedures".
- [ ] Last night's backup: `tail -n 20 /srv/truenorth/logs/backup.log` ends with a success
      line and no `[ALERT]`; the off-host copy ran (§3.2): `sudo tail -n 5 /var/log/tn-offhost-copy.log`.
- [ ] `dc ps`: nothing `unhealthy`, `restarting` or `exited`.

Weekly:

- [ ] Disk: `df -h / /srv/truenorth` below 80 %.
- [ ] Certificate expiry: `openssl x509 -enddate -noout -in /srv/truenorth/tls/truenorth.crt`.
- [ ] **Users → AD Sync**: last night's run and what it found (§9).
- [ ] Calendar → Content view → **Feeds**: each source's last pull or import, no error (§7).

Containers restart on their own after a VM reboot (`restart: always`). After any reboot,
still check `dc ps` and System Health.

---

## 2. Upgrade a release or release candidate

An upgrade is the same installer run with the new release; the installer works out the
order. A release candidate (`vX.Y.Z-rcN`) and a release (`vX.Y.Z`) upgrade the same way;
moving from the last candidate to its release is an ordinary upgrade. The installer refuses
to go **backwards** (an older release, or a database newer than the code) unless told to
(rollback, §2.4).

### 2.1 Before (the day before)

1. Read the release notes (`docs/release-notes/<vX.Y.Z>.md` in the new release) and
   `CHANGELOG.md`: new migrations, new required settings, known limitations.
2. **[DECISION]** The window. In-flight range builds either finish (the default, `drain`:
   up to an hour for a large range) or are abandoned (`-e tn_upgrade_inflight=abandon`;
   interrupted ranges must be rebuilt). Tell instructors (`docs/runbooks/on-call.md`
   "Communication").
3. Check last night's backup succeeded and was copied off the host (§1).
4. Optional, for releases with risky migrations, at the test site: the rollback rehearsal in
   `docs/runbooks/upgrade.md` ("Before"). If it fails, a rollback means restoring the
   pre-upgrade backup, not redeploying the old release.

### 2.2 Fetch and verify the new release

On the TrueNorth VM:

```bash
NEW=<vX.Y.Z>
git -C ~/truenorth fetch --tags origin
git -C ~/truenorth checkout "$NEW"
```

Then guide 02 §4.2 with `TN_RELEASE=$NEW`: download the four release files into
`~/release/$NEW`, verify both signatures and the checksums, and check `git_sha` matches the
checkout. Stop if anything fails. Air-gapped: the new release arrives as a new transfer kit
(guide 02 Appendix A).

### 2.3 Upgrade

Edit two lines of `~/truenorth/install/inventory/site.yml`:

```yaml
          tn_release_version: <vX.Y.Z>
          tn_release_dir: /home/tnadmin/release/<vX.Y.Z>
```

Then:

```bash
cd ~/truenorth/install
ansible-galaxy collection install -r requirements.yml
ansible-playbook -i inventory/site.yml site.yml -K --ask-vault-pass -e @smoke.yml
```

When the database is behind the release, `50-stack-up` first takes a **pre-upgrade backup**
into `/srv/truenorth/backups/pre-upgrade/` (note its name in the output: it is the rollback
point), stops the old application services, migrates, and starts the new release; the smoke
test then proves health, readiness and sign-in. A pre-upgrade backup that exits `6` (the
audit archive does not verify) **stops the upgrade** on purpose:
`docs/runbooks/upgrade.md` "Pre-upgrade backup exit 6".

Check by hand: sign in, open a course, build and destroy a small range. Then update the
template kit checkout used in §4 to the same release:

```bash
git -C /srv/truenorth/template-kit fetch --tags origin
git -C /srv/truenorth/template-kit checkout <vX.Y.Z>
```

Keep the pre-upgrade backup until the next release is stable.

### 2.4 Roll back

Always explicit (`-e tn_allow_downgrade=true`). Two kinds, both in
`docs/runbooks/upgrade.md` "Roll back":

- **Code rollback, schema kept**, valid only when the rehearsal passed: check out the
  previous release in `~/truenorth`, put it back in `site.yml`, run `site.yml` with
  `-e tn_allow_downgrade=true`. Never run `alembic downgrade` for this.
- **Data rollback** (a migration damaged data): restore the pre-upgrade backup (§3.5 step 9
  with that backup), then install the previous release as above. Everything written since
  the backup is lost; record it in the incident.

---

## 3. Backup, restore and the restore drill

### 3.1 What is backed up, and what is not

The installer schedules `scripts/backup/cron-backup.sh` nightly at **02:17** (the
`tnadmin` crontab). Each run writes one directory under `/srv/truenorth/backups/`
(`daily/`, `weekly/` on Sundays, `monthly/` on the 1st; 7 / 4 / 12 kept), holding:

| In the backup | Protection |
|---|---|
| Every PostgreSQL database (TrueNorth, Keycloak, LRS) | `pg_dump`, checksummed; **not encrypted** |
| Every MinIO bucket | Copied, checksummed; **not encrypted** |
| The env file and every persisted secret (`/srv/truenorth/config/secrets/`) | **Encrypted** to the backup escrow public key (`tn_backup_escrow_pubkey`) |
| The signed audit export archive | Copied and its signatures verified |
| OpenSearch telemetry | A snapshot in `/srv/truenorth/opensearch-snapshots` (outside the backup directory; copy both) |

So the secrets are encrypted, the data is not: protect the copies (§3.2) like the live
system. Not in any backup: Redis (a transient broker), and **the installer's own site
files**. Keep a copy of these off the VM, because a rebuild needs them: `install/inventory/site.yml`,
the encrypted `install/inventory/group_vars/all/vault.yml`, `install/smoke.yml`,
`install/files/` (CA, TLS pair, escrow public key) and `~/release/<vX.Y.Z>/`. §3.2 copies them
nightly.

**Enclave profile.** `tn_enclave_id` and `tn_classification_level` (in `site.yml`) are
rendered into the env file as `TN_ENCLAVE_ID` and `TN_CLASSIFICATION_LEVEL`. They name this
deployment and are part of the result-transfer security boundary. A backup may be restored
**only** into the same enclave: the same `site.yml` values, on a deployment of the same
classification. Never restore one enclave's backup into another, and never copy a
classified deployment's keys, database or secrets with these values.

**Trust registry.** The result-transfer trust registry (`result_transfer_trust_keys`, ADR
0015, `docs/result-transfer.md`) holds only **public** keys of trusted remote enclaves,
each with its purpose, key id and validity window. It is in the TrueNorth database, so every
backup carries it and a restore brings it back exactly as it was at backup time: a key
enrolled or retired after that backup must be enrolled or retired again (platform
administrator, `POST /result-transfer/trust-keys`, `.../retire`). Private signing and
decryption keys are never in the registry or the backups.

Full detail: `docs/runbooks/backup-restore.md`.

### 3.2 Copy the backups to the second management host

There is no shared storage, so the copy goes to a small **backup target VM** on management
host 2 (guide 01 §1.2), whose data disk lives on that host's datastore. Build it as guide 02
§1.1 (Ubuntu Server 24.04, OpenSSH, no snaps; hostname `<backup-vm-name>`, address
`<backup-vm-ip>`), with its data disk mounted at `/srv/tn-backup` as in guide 02 §1.2 (use
volume group `bk-data` and label `tn-bkp` in place of `tn-data` and `tn-srv`).
**[DECISION]** Whether that VM's disks must also be encrypted at rest (vSphere VM
encryption): the database dumps in it are not.

On the **backup target VM**:

```bash
sudo apt-get install -y rsync
sudo adduser --disabled-password --gecos "" tnbackup
sudo install -d -o tnbackup -g tnbackup -m 0700 /srv/tn-backup
sudo install -d -o tnbackup -g tnbackup -m 0700 /home/tnbackup/.ssh
```

On the **TrueNorth VM**, a key for root (the snapshot repository is not readable by
`tnadmin`) and the backup target's host key:

```bash
sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/tn-backup -C "truenorth off-host copy"
sudo cat /root/.ssh/tn-backup.pub
sudo ssh-keyscan -t ed25519 <backup-vm-ip> | sudo tee -a /root/.ssh/known_hosts
```

Compare the key `ssh-keyscan` printed with the backup VM's own
(`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on it). Then on the **backup target VM**,
allow that key to do rsync into `/srv/tn-backup` and nothing else (paste the public key in
place of `<public-key>`):

```bash
echo 'restrict,command="/usr/bin/rrsync /srv/tn-backup" <public-key>' \
  | sudo tee /home/tnbackup/.ssh/authorized_keys
sudo chown tnbackup:tnbackup /home/tnbackup/.ssh/authorized_keys
sudo chmod 0600 /home/tnbackup/.ssh/authorized_keys
```

On the **TrueNorth VM**, the copy script and its schedule (05:30, after the 02:17 backup has
had time to finish; check `backup.log` for how long yours take):

```bash
sudo tee /usr/local/sbin/tn-offhost-copy.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
# Copy TrueNorth's backups, telemetry snapshots and installer site files to the backup VM.
set -euo pipefail
DEST="tnbackup@<backup-vm-ip>"
SSH="ssh -i /root/.ssh/tn-backup -o BatchMode=yes"
echo "=== $(date -u +%FT%TZ) start"
rsync -a --delete -e "$SSH" /srv/truenorth/backups/ "$DEST:backups/"
rsync -a --delete -e "$SSH" /srv/truenorth/opensearch-snapshots/ "$DEST:opensearch-snapshots/"
rsync -a -e "$SSH" --relative \
  /home/tnadmin/truenorth/install/inventory/site.yml \
  /home/tnadmin/truenorth/install/inventory/group_vars/all/vault.yml \
  /home/tnadmin/truenorth/install/smoke.yml \
  /home/tnadmin/truenorth/install/files/ \
  /home/tnadmin/release/ \
  "$DEST:install-config/"
echo "=== $(date -u +%FT%TZ) done"
EOF
sudo chmod 0750 /usr/local/sbin/tn-offhost-copy.sh
echo '30 5 * * * root /usr/local/sbin/tn-offhost-copy.sh >> /var/log/tn-offhost-copy.log 2>&1' \
  | sudo tee /etc/cron.d/tn-offhost-copy
sudo /usr/local/sbin/tn-offhost-copy.sh
```

The last line runs it once now; it must end with `done`. On the backup VM,
`sudo ls /srv/tn-backup/backups/daily` then lists the backups. The copy is a mirror
(`--delete`): it protects against losing the TrueNorth host, not against deleting backups on
it. **[DECISION]** whether to keep a longer history on the backup VM or on removable media.

The escrow **private** key is never on either VM.

### 3.3 Check a backup

```bash
ls -1d /srv/truenorth/backups/*/truenorth-backup-* | sort | tail -n 3
B=$(ls -1d /srv/truenorth/backups/*/truenorth-backup-* | sort | tail -n 1)
jq '{created: .created_utc, databases: [.postgres.databases[].name], opensearch: .opensearch.status,
    audit_verified: .audit_archive.verified, escrow: .escrow.status}' "$B/manifest.json"
(cd "$B" && sha256sum --quiet -c SHA256SUMS && echo "checksums OK")
```

Expected: `opensearch` is `snapshot`, `audit_verified` is `true`, `escrow` is `encrypted`.

Exit codes and alerts (`BackupFailed`, `BackupIncomplete`, `BackupStale`,
`AuditArchiveTampered`): `docs/runbooks/backup-restore.md` "Alerts". A run by hand:

```bash
cd /srv/truenorth/app
set -a; . /srv/truenorth/config/backup.env; set +a
scripts/backup/cron-backup.sh
```

### 3.4 Recovery objectives

RPO **24 hours** (nightly backups; anything written after the last good one is lost). RTO
**4 hours** (rebuild, restore, verify). These are targets; the drill (§3.6) measures yours.

### 3.5 Disaster recovery: rebuild on management host 2

When management host 1 or the TrueNorth VM is lost. The rebuilt VM takes the **same name,
address and certificate**, so DNS, the TLS certificate and users' bookmarks stay valid.

1. **Declare it and note the time** (T0). Make sure the old VM is powered off, if its host
   still runs: two TrueNorth VMs must never run at once on one address and one vCenter.
2. **Build the VM** on management host 2, exactly as guide 02 §1–§2 (same `<tn-ip>`, same
   hostname; in vCenter you may name it `<tn-vm-name>-dr`).
3. **A key for the backup VM.** On the new VM, make root's key and record the backup VM's
   host key exactly as in §3.2 (`ssh-keygen`, `ssh-keyscan`, compare the fingerprint). On
   the **backup VM**, **add** (do not replace) a line for the new key:

   ```bash
   echo 'restrict,command="/usr/bin/rrsync /srv/tn-backup" <new-public-key>' \
     | sudo tee -a /home/tnbackup/.ssh/authorized_keys
   ```

4. **Bring back the site files and the release.** On the new VM:

   ```bash
   SSH="ssh -i /root/.ssh/tn-backup -o BatchMode=yes"
   sudo rsync -a -e "$SSH" tnbackup@<backup-vm-ip>:install-config/ /root/restore-config/
   C=/root/restore-config/home/tnadmin
   REL=$(sudo awk '/^[[:space:]]*tn_release_version:/ {print $2}' "$C/truenorth/install/inventory/site.yml" | tr -d "\"'")
   echo "release that was running: $REL"
   git clone https://github.com/mblanke/TrueNorth.git ~/truenorth     # air-gapped: unpack the kit's checkout
   git -C ~/truenorth checkout "$REL"
   sudo cp -a "$C/truenorth/install/." ~/truenorth/install/
   sudo cp -a "$C/release" ~/release
   sudo chown -R tnadmin:tnadmin ~/truenorth ~/release
   ```

   Then guide 02 §3.1 (govc) if you will port templates from here later.
5. **Bring back the newest backup and the snapshot repository:**

   ```bash
   sudo install -d -o tnadmin -g tnadmin -m 0755 /srv/truenorth /srv/truenorth/backups
   sudo install -d -o tnadmin -g tnadmin -m 0750 /srv/truenorth/config
   sudo install -d -o 1000 -g 1000 -m 0750 /srv/truenorth/opensearch-snapshots
   sudo rsync -a -e "$SSH" tnbackup@<backup-vm-ip>:backups/ /srv/truenorth/backups/
   sudo rsync -a -e "$SSH" tnbackup@<backup-vm-ip>:opensearch-snapshots/ /srv/truenorth/opensearch-snapshots/
   sudo chown -R tnadmin:tnadmin /srv/truenorth/backups
   sudo chown -R 1000:1000 /srv/truenorth/opensearch-snapshots
   B=$(ls -1d /srv/truenorth/backups/*/truenorth-backup-* | sort | tail -n 1); echo "$B"
   ```

   **[DECISION]** For data corruption (not host loss), pick the last backup taken
   **before** the corruption instead of the newest.
6. **Secrets first.** Bring the escrow **private** key in for this step only (removable
   media, from its custodians):

   ```bash
   ~/truenorth/scripts/backup/restore-secrets.sh "$B" /media/<escrow-media>/backup-escrow.key \
     --secrets-dir /srv/truenorth/config/secrets
   ```

   Take the private key away again.
7. **Run the installer** (guide 02 §6.1–§6.2). It finds the restored secrets, renders the
   env file from them and brings up an empty stack:

   ```bash
   cd ~/truenorth/install
   ansible-galaxy collection install -r requirements.yml
   ansible-playbook -i inventory/site.yml playbooks/00-docker.yml -K --ask-vault-pass
   ansible-playbook -i inventory/site.yml site.yml -K --ask-vault-pass -e @smoke.yml
   ```

8. **Restore the data:**

   ```bash
   cd /srv/truenorth/app
   set -a; . /srv/truenorth/config/backup.env; set +a
   scripts/backup/restore.sh "$B"
   ```

   It verifies the checksums, asks you to type the compose project name, stops the
   application services, restores every database, the buckets, the audit archive and the
   telemetry snapshot, and starts the services again.
9. **Verify:**

   ```bash
   cd ~/truenorth/install
   ansible-playbook -i inventory/site.yml playbooks/95-smoke-test.yml -K --ask-vault-pass -e @smoke.yml
   dc exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select count(*) from users" -c "select count(*) from ranges" -c "select version_num from alembic_version"'
   ```

   Sign in; confirm recent classes, ranges and results are there; Infrastructure discovers
   vCenter. Everyone signs in again (sessions are gone).
10. **Re-create the off-host copy** (§3.2's script and cron on this VM; the key exists from
    step 3), so tonight's backup leaves the host too. On the backup VM, remove the dead
    VM's key line from `/home/tnbackup/.ssh/authorized_keys`.
11. **Record** T0, the time of each step and the backup used. Note the measured RTO in the
    site record.

### 3.6 The restore drill (quarterly)

The drill is §3.5 done for real in a maintenance window, then failed back. Running a second
copy beside production is **not** safe: it would act on the same vCenter with the same
identity.

1. **[DECISION]** The window (half a day) and who approves it. No Students on the platform.
2. Shut the production VM down cleanly: `sudo poweroff` on it.
3. Do §3.5 steps 2–9 on management host 2, with the newest backup. **Skip step 10**: the
   drill VM must not copy its backups over the real ones.
4. Record the measured time against the 4-hour RTO.
5. **Fail back:** power the drill VM off (`sudo poweroff`) and power the original VM on.
   Anything done on the drill VM is discarded; that is why the window has no users. Remove
   the drill VM's key line from the backup VM's `authorized_keys`. Check System Health on
   the original.
6. Remove or keep the drill VM powered off (**[DECISION]**; a powered-off copy is a warm
   spare, but it must never be powered on while the original runs).

Between drills, CI proves the backup and restore scripts on every change
(`scripts/backup/drill.sh`); it does not time a restore on real data, which is what this
drill is for.

---

## 4. Port golden templates and content

Golden templates are built and proven at a **test site**, then ported to each site with
their build record, so that every site clones the same, checksummed build, and the range
certification's template-pinning section (S2) shows the promoted build on every VM. This is
the normal path; building from ISO at the site is guide 02 Appendix D.

You need, per template: the exported OVF, its **build record**
(`<id>.build.json`, from the test site's `infra/vsphere/packer/logs/`), and checksums. You
also need the **template build passwords** (`vault_template_windows_admin_password`,
`vault_template_linux_password`): they come from the test site's custodians, never in the
kit, and must already be in this site's vault (02 §5.2).

### 4.1 At the test site: export

On a machine with govc pointed at the test site's vCenter (02 §3.1) and the template kit
whose `logs/` holds the build records:

```bash
ID=<catalogue-id>                                         # e.g. srv2022, ubuntu-lts, pfsense
REC=/path/to/template-kit/infra/vsphere/packer/logs/$ID.build.json
TEMPLATE=$(jq -r .template "$REC")
jq '{build_id, template, checksum, build_date, iso_checksum}' "$REC"
SRC=$(govc find / -type m -name "$TEMPLATE" | grep -v '/_retired/' | head -n 1); echo "$SRC"
OUT=~/template-port/$ID
mkdir -p "$OUT"
govc export.ovf -vm "$SRC" "$OUT"
cp "$REC" "$OUT/"
(cd "$OUT" && find . -type f ! -name SHA256SUMS -exec sha256sum {} + > SHA256SUMS)
```

Export the **golden template itself** (`<template>`), not a host-local copy
(`<template>@<host>-<date>`). Check that the build id is the one promoted at the test site
(Infrastructure → Golden images there). If the test site keeps its templates in a content
library, export the library item instead (`govc library.export`), with the same build record
and checksums. Carry `~/template-port/` to the site on approved media.

### 4.2 At the site: import, replicate, register, promote

Once per site, a template-kit checkout readable by the API's containers, at the running
release:

```bash
sudo install -d -o tnadmin -g tnadmin -m 0755 /srv/truenorth/template-kit
git clone ~/truenorth /srv/truenorth/template-kit
git -C /srv/truenorth/template-kit checkout <vX.Y.Z>
```

Once per site, load the golden-image catalogue (the list of catalogue ids) into TrueNorth,
as an administrator (`<admin-email>`: the AD administrator's address from 02 §7.2):

```bash
cd /srv/truenorth/app/infra/platform/docker
docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production \
  run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v /srv/truenorth/app:/srcapp:ro \
  --entrypoint python api -c '
import sys; sys.path.insert(0, "/srcapp/scripts")
from _inprocess_api import connect
c = connect(sys.argv[1])
assert c, "no such administrator"
with open("/srcapp/content/catalogue/vm_iso_catalogue.csv", "rb") as f:
    print(c[1].post("/golden-images/import-catalogue", params={"hypervisor": "vsphere"},
                    files={"file": ("vm_iso_catalogue.csv", f, "text/csv")}))
' '<admin-email>'
```

It prints `(200, {... "imported": true ...})`. Infrastructure → Golden images then lists the
catalogue, every image `planned`.

Then, **per template**, on the TrueNorth VM with govc pointed at this site's vCenter
(02 §3.1):

```bash
ID=<catalogue-id>
IN=~/template-port/$ID                       # where you copied the kit
(cd "$IN" && sha256sum -c SHA256SUMS)        # every line OK, or stop
TEMPLATE=$(jq -r .template "$IN/$ID.build.json")
OVF=$(find "$IN" -name '*.ovf' | head -n 1); echo "$OVF"

# 1. Import it once, on one range host, into TrueNorth/Templates, as a template named exactly $TEMPLATE.
govc import.spec "$OVF" > "$IN/spec.json"
jq --arg n "$TEMPLATE" --arg net '<non-range-port-group>' \
  '.Name = $n | .MarkAsTemplate = true | .PowerOn = false | .NetworkMapping |= map(.Network = $net)' \
  "$IN/spec.json" > "$IN/spec.site.json"
govc import.ovf -options="$IN/spec.site.json" -ds='<range-host-1>-local' \
  -pool='/<datacenter>/host/<range-cluster>/Resources' -host='<range-host-1-name-in-vcenter>' \
  -folder='/<datacenter>/vm/TrueNorth/Templates' "$OVF"

# 2. Put its build record where the template kit expects it, without the test site's copies.
mkdir -p /srv/truenorth/template-kit/infra/vsphere/packer/logs
jq '.copies = {}' "$IN/$ID.build.json" > "/srv/truenorth/template-kit/infra/vsphere/packer/logs/$TEMPLATE.build.json"

# 3. Replicate: a stamped host-local copy on every range host. Dry run first.
cd /srv/truenorth/template-kit/infra/vsphere/packer
export RANGE_HOSTS="<range-host-1> <range-host-2> <range-host-3> <range-host-4>"
export RANGE_DATASTORE_FMT="%s-local"
DRY_RUN=1 ./build.sh replicate "$TEMPLATE"
./build.sh replicate "$TEMPLATE"
jq .copies "logs/$TEMPLATE.build.json"         # one copy per range host

# 4. Register it built, with its build record (never promotes).
cd /srv/truenorth/app/infra/platform/docker
docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production \
  run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v /srv/truenorth/template-kit:/srcapp:ro \
  --entrypoint python api /srcapp/scripts/register_golden_image_inprocess.py \
  --admin '<admin-email>' --id "$ID" --template "$TEMPLATE" \
  --build-record "/srcapp/infra/vsphere/packer/logs/$TEMPLATE.build.json"
```

The template's network mapping (`<non-range-port-group>`) does not matter: TrueNorth puts
every clone's NICs on the range's own port groups. Use any port group that is not a range
port group. `RANGE_HOSTS` are the hosts' short names as vCenter knows them, and
`RANGE_DATASTORE_FMT` turns a short name into its datastore (guide 01 §2.2). A tagging
warning from replicate is harmless.

5. **Promote it.** As the platform administrator: Infrastructure → **Golden images** →
   open the image → its build (same build id and checksum as the test site's) → **Promote**,
   with a reason ("ported from <test site>, build <build id>"). The build must name a copy on
   every range host; the page lists any host that has none (ranges will not be placed there
   for that image).
6. **Prove it.** Build a small range that uses the image. On its Certification card the
   template-pinning section shows the promoted build for every VM.

Optional, before registering: `scripts/lab/test-clone.sh` (in the template kit) clones the
template on each range host, customises it and checks that VMware Tools reports the new name
and address. It needs an isolated port group and test addresses (`--network`, `--ip-base`):
**[DECISION]** which. See its header for the options.

A newer build of the same template later: repeat 4.1–4.2 with the new build record (a new
build id); the replicated copies get a new date, and you promote the new build. Old copies
are pruned automatically once no promoted build uses them.

**Windows licensing (ADR 0012 decision 3):** templates carry no product key; activation is
applied at deploy from the site's KMS host or MAK. Until that setting exists (it is built
with the production KMS server), Windows range VMs run unactivated: fully functional, with
a watermark.

### 4.3 Content

| Content | How it moves | Where |
|---|---|---|
| Range templates | The YAML file | Range Designer → **Import YAML** (validated before saving) |
| Courses built with the Course Studio (ARC²) | The release bundle | Course Studio → **Upload release** |
| The repository's shipped curriculum | Loaded by the installer as drafts | `tn_load_shipped_content: true` in `site.yml`, re-run `site.yml`; publish what the site uses. Off by default: a production site starts with an empty catalogue |

How instructors and authors then use them: [04 — Instructor guide](04-instructor-guide.md).

---

## 5. Common failures and their fixes

| Symptom | Cause | Fix |
|---|---|---|
| The site opens as an empty page; sign-in says `invalid_redirect_uri` | Opened by IP address | Always `https://<tn-fqdn>/`; the workstation must resolve it |
| Preflight: a `CHANGE_ME` is left in the vault | Placeholder still set | `ansible-vault edit inventory/group_vars/all/vault.yml` |
| Preflight: clock not synchronised | No reachable NTP source | 02 §1.3; `chronyc sources`; UDP 123 to the source |
| Preflight: AD DNS has no answer for the platform host or the DC | Missing `A`/`PTR` | Add them in AD DNS (01 §5.1) |
| `00-docker` stops on an existing Docker | Ubuntu's `docker.io`, the Docker snap, or an old `docker.list` | Remove it (`sudo snap remove docker`; `sudo apt-get purge docker.io containerd`; delete the extra `.list`), re-run `00-docker` |
| `PKIX path building failed` (Keycloak, AD federation) | Keycloak does not trust the DC's LDAPS certificate | Correct root CA in `install/files/corp-root-ca.cer`; re-run `site.yml` |
| The installer stops: a vault value differs from the persisted one | A first-start password was changed in the vault | Put the old value back (or empty it); to change it, `playbooks/rotate-secret.yml` (`docs/runbooks/key-rotation.md`) |
| The installer stops: tables but no `alembic_version` | A database built outside migrations | Stop and ask the owner. Never stamp without a decision (`install/README.md` "The migration hazard") |
| The installer refuses an older release or a newer database | A downgrade | Intended; §2.4 if you meant it |
| Smoke test: no `groups` claim, or not from AD | The smoke account is in no TN-* group, or only through a nested group | Put `<smoke-upn>` **directly** in `TN-Observers` |
| An AD user cannot sign in | Account disabled, not in the users OU, nested group only, or its email is also a local account's | Check AD; Keycloak refuses an email collision (`install/README.md` "AD on staging", behaviours table) |
| Someone lost their authenticator | — | `docs/runbooks/break-glass.md` "Reset the second factor" |
| Everyone is stuck on the Rules of use page | A text nobody can agree to | `docs/runbooks/break-glass.md` "Rules of use" |
| Readiness degraded: OpenSearch `yellow` | An index asks for a replica on a one-node cluster | Re-run `playbooks/70-telemetry.yml`; by hand: `install/README.md` "OpenSearch" |
| Every range build fails with 403 | The vCenter role is missing a privilege, or is not assigned | The error names the privilege: 02 §3.2; then `playbooks/90-vsphere.yml` |
| "MAC … is already used by VM …" | Another install, or a stray VM, uses this install's MAC slice | Give each install on a vCenter its own `tn_vsphere_mac_pool` (02 §5.3) |
| "built but not ready: … not reported by VMware Tools" | The guest never showed its planned address (Tools or cloud-init broken in the template) | Check the template; destroy the range; a new build of the template |
| A VM's `template_login` is `accepted` (Not safe) | The template's build password still logs in | Rebuild that range; if it repeats, the template is wrong: report it to the test site |
| A range refuses to build: image has no promoted build | The template was registered but not promoted | §4.2 step 5 |
| A range stuck `provisioning`; a worker died | Lost task | `docs/operations.md` "Range Leases, Abandon and Recovery". Never reset state in SQL |
| Backup exit 3 / 5 / 6 | No escrow / telemetry snapshot failed / audit archive does not verify | `docs/runbooks/backup-restore.md` "Alerts"; 6 is a security event |
| AI features answer 503 "AI is not configured" | `tn_ai_enabled: false`, by design | Guide 02 Appendix C |
| `99-validate`: AI models MISSING on the endpoint | LiteLLM has no alias the installer was told to use | Add the alias in `litellm.yaml`, then `sudo docker compose restart litellm` on the AI node (a plain `up -d` does not reload it) |
| AI test answers are empty | A reasoning model used its small `max_tokens` budget on reasoning | Test with `max_tokens` of 200 or more |
| Grafana or another dashboard reads "offline" everywhere | Prometheus is down, not the platform | Check the platform directly: System Health, `dc ps`; then `dc up -d prometheus` |
| A container is `Up` but its port answers nothing | Restarted without its port mapping | `dc up -d --force-recreate <service>` |
| `BreakGlassAccountUsed` fires | Someone signed in with the local break-glass account | Expected only during planned recovery; otherwise an incident (`docs/runbooks/incident.md`) |

---

## 6. The range isolation check

Every range that becomes ready is probed for isolation (range certification S4): from one
VM in each range network, TrueNorth tries to reach every `tn_range_isolation_probe_targets`
entry (TCP and ICMP), checks that the external name (`tn_range_isolation_external_name`)
does not resolve, and, with a depot, that the permitted exceptions do connect while every
other port of those hosts does not. The result is on the range's **Certification** card,
"Isolation evidence per zone".

`tn_range_isolation_check` decides what a bad result does:

| Mode | Effect |
|---|---|
| `warn` (as installed) | The verdict is recorded on the certification record and lowers it; the range still builds |
| `enforce` | A range not proven isolated fails and is powered off |

It stays `warn` until the **network team signs off** the isolation model (guide 01 §2,
`vlan_trunk_no_l3`; issue #238). To get there:

1. Build each range template the site will use. On each range's Certification card, every
   zone shows a probed row and "Isolated". A zone with no VM that has VMware Tools and a
   declared account shows "No probe VM": add a small, single-homed `probe: true` VM to that
   zone of the template.
2. **[DECISION]** With owner approval only, prove the check can fail: plant a route out on
   a range's edge firewall, press **Probe again**, see the verdict turn *Not safe*, remove the
   route, probe again.
3. The network team reviews the switch and firewall configuration (guide 01 §3–§4) and the
   evidence, and signs off. Record who and when.
4. Set `tn_range_isolation_check: enforce` in `site.yml` and re-run:

   ```bash
   cd ~/truenorth/install
   ansible-playbook -i inventory/site.yml site.yml -K --ask-vault-pass -e @smoke.yml
   ```

What the evidence is: what the probe VM reported, from the account named on the card.
Whoever controls that guest could fake it, which is why a re-probe can only raise a verdict
from a `probe: true` VM using a staff-only account. More detail:
`docs/deployment/vmware-site-runbook.md` §7.

---

## 7. Content feeds

Release, end-of-life and vulnerability data make content due for review: a feed entry that
names something a golden template is built from (for example "Ubuntu 20.04 reached end of
life") flags that template, and the flag flows to the range templates, scenarios, modules and
courses that use it. Flags appear on **Calendar → Content view**; **Mark reviewed** clears
one. Off on a fresh install unless `site.yml` sets `tn_feeds_mode`.

### 7.1 Online: a daily pull

`tn_feeds_mode: online` (02 §5.3). The provisioning worker pulls endoflife.date, CISA KEV and
the NVD CVE API once a day at 04:10 UTC: GET requests naming public products, no site data.
Optional settings: `tn_feeds_http_proxy` (the site proxy), `vault_feeds_nvd_api_key` (raises
NVD's rate limit), `tn_feeds_nvd_min_cvss` (default 9.0). Check the Content view's **Feeds**
panel: each source's last pull and its error, and a warning for any product-map entry that
matched nothing.

### 7.2 Air-gapped: signed bundles

Once, on a connected machine with a checkout of the running release and Python 3.11 or
newer, make the bundle signing key:

```bash
cd ~/truenorth
python3 -m venv ~/.venvs/tn-feeds
~/.venvs/tn-feeds/bin/pip install -r control-plane/api/requirements.txt -r control-plane/worker/requirements.txt
mkdir -p ~/.truenorth && chmod 700 ~/.truenorth
~/.venvs/tn-feeds/bin/python scripts/build_feed_bundle.py --new-key ~/.truenorth/feed-bundle.key
```

It prints the public key. Put it in `site.yml` as `tn_feeds_bundle_public_key`, set
`tn_feeds_mode: bundle`, and re-run `site.yml`. The private key stays on the connected
machine (and its backup), never at the site.

Each time (for example weekly), build a bundle and carry it in:

```bash
cd ~/truenorth
~/.venvs/tn-feeds/bin/python scripts/build_feed_bundle.py --key ~/.truenorth/feed-bundle.key \
  --out "feeds-$(date +%F).json"
```

(`--proxy http://<proxy>:<port>` if the machine needs one; an NVD key, if any, is read from
`TN_FEEDS_NVD_API_KEY`.) At the site, a platform administrator imports it on the Content
view's **Feeds** panel. TrueNorth refuses, and records, an unsigned bundle, one signed by
another key, one changed after signing, one dated more than a day ahead, and one older than
the last import. Each bundle replaces the last: to undo a bad one, import a newer, correct one.

---

## 8. Placement and port mirroring

SOC ranges copy traffic to their sensors with vDS port mirroring. Copies from VMs on other
hosts travel on a remote-mirroring (RSPAN) VLAN from the pool, which needs **MAC learning
off** on the switch (guide 01 §2.1). If a sensor sees traffic only from VMs on its own host,
and the switch cannot turn learning off, place each range on one host:
`tn_vsphere_placement: per-range-host` in `site.yml`, then re-run `site.yml`. The check
procedure is `docs/deployment/vmware-site-runbook.md` §7.1.

---

## 9. AD status sync: dry run, then enable

Every night (02:00 UTC) TrueNorth reads Keycloak's view of AD and deactivates TrueNorth
accounts whose AD account was disabled or removed, and, with a break-glass account, local
accounts unused for 90 days (never the break-glass account itself, never the tenant's last
administrator). It never writes to AD. It is installed as a **dry run**
(`tn_ad_status_sync_dry_run: true`): it records whom it would deactivate and changes nothing.

1. For the first few nights (**[DECISION]** how many; at least three), open
   **Users → AD Sync** each morning. Check every account listed: each must really be
   disabled or gone in AD (or an unused local account). Anyone listed who should keep
   access means a directory problem to fix first (wrong OU, a renamed account).
2. When the lists are right, set `tn_ad_status_sync_dry_run: false` in `site.yml` and re-run:

   ```bash
   cd ~/truenorth/install
   ansible-playbook -i inventory/site.yml site.yml -K --ask-vault-pass -e @smoke.yml
   ```

3. Nothing happens at once. The **next night** is the first applied run: it deactivates only
   accounts that an earlier run 12 to 36 hours before also found, for the same reason. A run
   that would deactivate more than max(3, 25 %) of a tenant's accounts in one family holds
   instead and changes nothing; the panel says so, and **Run now, over the limit** releases
   it after you have checked.

Deactivating removes the person from the roll call of open classes (freeing their seats);
nothing is deleted, and an administrator can re-enable the account in **Users**. Without
email (02 §6.4), class leads are not told: read the panel. Details: `docs/identity.md`
"AD status sync (nightly)".

---

## Checklist — operations

Print and keep with the site record.

**After the install (once)**

- [ ] Backup target VM built on management host 2; `tnbackup` with `rrsync` key only
- [ ] Off-host copy script and cron installed; first run ended `done`
- [ ] Golden-image catalogue loaded; each template imported, replicated, registered, promoted (§4)
- [ ] A small range built per image; template pinning shown on its Certification card
- [ ] AD status sync dry-run nights reviewed: ___ / ___ / ___; then enabled
- [ ] Feeds: online pull green, or bundle key made and first bundle imported
- [ ] Isolation evidence reviewed; network team sign-off recorded; `enforce` set

**Daily**

- [ ] System Health green; `amtool alert query` empty; `dc ps` clean
- [ ] Backup log clean; off-host copy log ends `done`

**Weekly**

- [ ] Disk below 80 %; certificate expiry date noted
- [ ] AD Sync panel read; Feeds panel without errors

**Quarterly**

- [ ] Restore drill (§3.6) in a window: measured RTO ______ ; failed back
- [ ] Escrow private key custodians confirmed; vault password still held

**Each upgrade**

- [ ] Release notes read; window agreed; last backup off the host
- [ ] New release verified (cosign ×2, checksums, `git_sha`)
- [ ] `site.yml` updated; `site.yml` run passed; pre-upgrade backup name recorded
- [ ] Template kit checkout moved to the same release
