# Next work-site visit: rc23 (written 2026-11-27)

One page for the next day at the office. Wired network only; the lab is not reachable from
home or Wi-Fi. Nothing here has been run yet. Earlier state:
[`audit-2026-11-23.md`](audit-2026-11-23.md), "Follow-up on site, 2026-11-24".

Staged on the Mac on 2026-11-27, so step 2 is one command:

- the install checkout at tag `v1.2.0-rc23` (`~/.truenorth-work/checkouts/work-rc23`);
- the release files, signature and checksums verified
  (`~/.truenorth-work/release-v1.2.0-rc23/`), with Sigstore's `trusted_root.json` beside them.

## 1. Before the upgrade (15 min)

- [ ] **Security Onion install login.** `VSPHERE_SO_INSTALL_USER` and
      `VSPHERE_SO_INSTALL_PASSWORD` were added by hand to `.env.production` on TN-MGMT01 on
      2026-11-23. rc23's installer owns both lines and replaces them. Put the same values in
      the work inventory (`tn_vsphere_so_install_user`) and vault
      (`vault_vsphere_so_install_password`) first. If the password equals
      `vault_template_linux_password` and the user is `soadmin`, nothing needs adding.
- [ ] **Migration `d6f8b0a2c4e5`** refuses to run if `imported_results` holds two first
      results of one attempt. The work site has never imported enclave results, so the table
      should be empty. If the upgrade stops there, it names the rows.
- [ ] Note the installed version before starting (expected: rc22).

## 2. Install rc23

```bash
~/.truenorth-work-install.sh v1.2.0-rc23 -e tn_release_dir=$HOME/.truenorth-work/release-v1.2.0-rc23 -e tn_release_trusted_root=$HOME/.truenorth-work/release-v1.2.0-rc23/trusted_root.json
```

- [ ] The `tn_release_trusted_root` setting makes this the first real run of the installer's
      air-gapped signature check (#541). It runs on the Mac, early, before anything on the
      host changes. If it fails, record cosign's message on #541 and rerun without that
      setting.
- [ ] Limit: the checkout is rc23, whose role still passes the deprecated `--offline=true`
      (the fix, #542, is on main after the tag). A pass shows the trusted-root path works in
      the installer; the fixed command line gets its real run with the next rc.
- [ ] It proves "no network needed" only if the Mac cannot reach Sigstore at that moment.
      Check first, Wi-Fi off: `curl -sS -m 5 https://tuf-repo-cdn.sigstore.dev/ -o /dev/null`.
      Record on #541 whether it could.
- [ ] After: the app reports rc23; Infrastructure → Golden images shows the Promote button
      and the status filter (#527).
- [ ] Windows range VMs now default to Eastern time (index 35). Ranges already built keep
      their zone.

## 3. Quick checks right after the install

- [ ] **#539, CSRF cookie `Secure` flag.** Sign in over HTTPS, open the browser's developer
      tools, Application → Cookies, and read the `Secure` column of `truenorth_csrf`. Record
      the answer on #539. No `Secure` means a code fix; that is done from home.
- [ ] **Strict acceptance:** `~/.truenorth-work-acceptance.sh v1.2.0-rc23`. Reports land on
      the host in `/srv/truenorth/acceptance-reports/latest.{txt,json}`; copy the JSON into
      the repo as the evidence record.
- [ ] **Promote `ubuntu-webstack`** on the Golden images page, with a reason. Approved by the
      owner on 2026-11-24; it passed test clones on esx02-04.

## 4. Timed range builds (owner's go-ahead for each build)

The first measured baseline. Nothing has ever timed a build on real hardware (#536).

- [ ] Build **medium-enterprise**, then **soc-training**, one at a time.
- [ ] For each, copy `timings` from the range's provisioner output and the worker log line
      `vSphere range <id> build took ...` (how to read them:
      `docs/deployment/vmware-site-runbook.md`, "Where a build's time goes").
- [ ] Record both tables in this directory.
- [ ] **Decision rule:** if cloning (`create`) is under about 30% of the build, pod masters
      come before linked clones. Otherwise linked clones are next.
- [ ] Destroy both ranges; confirm the teardown record lists nothing left.

Note: `securityonion` times only the start of so-setup, which then runs 20 to 60 minutes in
the guest. The SOC range is ready later than `total_seconds` says.

## 5. Golden images still open

| Image | Next step |
|---|---|
| parrot | Read the fifth run's log on TN-MGMT01. If built: replicate, test-clone, register, promote on the owner's word. |
| srv2019, win10-22h2 | During a build, on the guest console: read `vminst.log` and check the VMware Tools CD is mounted. Tools 12.5.1 never installs, so there is no network driver. |
| vyos | Console session on a test clone: why VMware Tools never starts. Then delete the leftover vyos test clones on esx02-04. |
| Windows role images, win11-analyst, win11-dev | Upload the installers to the depot (`UPLOAD.md`; files are in `~/.truenorth-work/role-installers`). |
| greyspace-host | Apply the depot mirrors from #534 (not proven on site), then build. |
| precomp-host, detonation-host | Not started; after the rows above. |

- [ ] Give the depot's apt-cacher-ng a watchdog: it wedged after its nightly expire on 11-24.
- [ ] When the builds are done, set the depot caches offline.
- [ ] Heads-up to the owner before each pfSense, Security Onion or VyOS build. Promotion is
      always on the owner's word.

## 6. If time is left

- [ ] `journeys` as a required check (#197): the owner's setting in branch protection.
- [ ] Console on real ESXi 8 (#367), with the 8-hour ceiling from guide 01.

## Bring back

Paste or copy to the repo: the installed version, the acceptance JSON, both `timings`
tables, the #539 answer, the #541 answer, and one line per image in section 5.
