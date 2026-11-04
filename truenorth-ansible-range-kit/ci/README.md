# CI

## Nightly canary

`ci/canary.sh` builds, verifies, resets and tears down `ranges/CANARY.yml`. If it fails, do not build
exercise ranges until the cause is known. The evidence for each run is kept under `build/CANARY/` and
`build/canary-logs/`.

### Schedule (systemd timer on the control node)

`/etc/systemd/system/tn-canary.service`
```ini
[Unit]
Description=TrueNorth nightly canary range

[Service]
Type=oneshot
User=svc-tn-range
WorkingDirectory=/opt/truenorth/range
Environment=TNRANGE_ANSIBLE_ARGS=-e @vault/vcenter.yml --vault-password-file /etc/truenorth/vault-pass
ExecStart=/opt/truenorth/range/ci/canary.sh
```

`/etc/systemd/system/tn-canary.timer`
```ini
[Unit]
Description=Run the TrueNorth canary nightly

[Timer]
OnCalendar=*-*-* 02:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now tn-canary.timer
systemctl list-timers tn-canary.timer        # validation
sudo systemctl disable --now tn-canary.timer # rollback
```

Add `config/blackout.json` windows for exercises. The canary refuses to run inside one.

### Approval signing key (required for CI, recommended everywhere)

```bash
# Where to run: control node, as root
sudo install -d -m 0750 -o root -g tn-approvers /etc/truenorth
openssl rand -hex 32 | sudo tee /etc/truenorth/approval.key >/dev/null
sudo chown root:tn-approvers /etc/truenorth/approval.key && sudo chmod 0640 /etc/truenorth/approval.key
# Members of tn-approvers: the human approvers and svc-tn-range (CI). NOT the account Claude Code runs as.
# Validation: as the Claude account, `cat /etc/truenorth/approval.key` must fail with Permission denied
# Rollback:   sudo rm /etc/truenorth/approval.key   (approvals revert to unsigned, terminal-only)
```

Once the key exists, every approval must be signed. The account Claude Code runs as cannot read the key,
so it cannot forge an approval, even if it gets past the guard hook. The playbooks re-check approvals with
`tools/range approval-check`, so run real builds under an account that can read the key.

### Pull request checks (no vCenter needed)

```bash
python3 -m unittest discover -s tests
ansible-lint playbooks/
for p in playbooks/*.yml; do ansible-playbook --syntax-check -i localhost, "$p" -e range_id=CANARY; done
for r in ranges/*.yml; do tools/range validate "$(basename "$r" .yml)"; done
```
