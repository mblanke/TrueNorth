# Lab runner: live vSphere tests from GitHub Actions

`.github/workflows/lab.yml` runs `tests/integration/test_lab_sessions_vsphere.py` against
the real vCenter: two Students' labs are built, reset, expired, cleaned up and reconciled
(see the test's docstring). GitHub-hosted runners cannot reach the vCenter, so the job
runs on a **self-hosted runner inside the lab network** with the labels
`[self-hosted, truenorth-lab]`.

It runs on `workflow_dispatch` (Actions -> "Lab (live vSphere)" -> Run workflow) and
nightly at 06:17 UTC. Never on `push` or `pull_request`: a self-hosted runner executes
whatever the workflow checks out, so it must not run code from forks or unreviewed
branches. Keep it that way. One run at a time (`concurrency: lab-vsphere`).

A skipped test is a failed job: the test skips itself when any variable below is missing,
and `scripts/junit_require_pass.py` turns "zero passed" into exit 1.

## Network posture: outbound only

The runner opens **outbound HTTPS (443)** to GitHub and holds a long poll for jobs.
Nothing connects to it; it needs **no inbound port, no port forward, no public IP**.

| From the runner to          | Port | Why                                              |
|-----------------------------|------|--------------------------------------------------|
| `github.com`, `api.github.com`, `*.actions.githubusercontent.com`, `codeload.github.com`, `objects.githubusercontent.com`, `pkg-containers.githubusercontent.com`, `results-receiver.actions.githubusercontent.com`, `*.blob.core.windows.net` | 443 | job polling, checkout, artifacts, logs |
| `pypi.org`, `files.pythonhosted.org` | 443 | `pip install -r requirements-test.txt` |
| Docker Hub (`registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com`) | 443 | the disposable `postgres:16` |
| the vCenter (`VSPHERE_URL`) | 443 | the test itself |

GitHub publishes the current hostname list at
<https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners#communication-between-self-hosted-runners-and-github>;
check it when the egress firewall is set up. If egress goes through a proxy, set
`https_proxy`/`no_proxy` in the runner's `.env` file (keep the vCenter in `no_proxy`).

## Host requirements

- Linux x64 VM on the lab management network, 2 vCPU, 4 GB RAM, 30 GB disk. Not a
  hypervisor host, not the platform host: a dedicated, rebuildable VM.
- Docker Engine, with the runner's user in the `docker` group (the job starts a
  throwaway `postgres:16` on `127.0.0.1:55499` and removes it afterwards).
- `curl`, `tar`, `git`. Python is installed per job by `actions/setup-python` into the
  runner's tool cache.
- The vCenter account in the secrets should be a dedicated service account scoped to
  the lab cluster, datastore, content library and the lab port groups, nothing else.

## Registration

Repository admin rights are needed for steps 1 and 5.

1. GitHub: *Settings -> Actions -> Runners -> New self-hosted runner*, Linux x64. Leave
   the page open; it shows a registration token valid for one hour.
2. On the VM, as an unprivileged user (not root):

   ```bash
   mkdir -p ~/actions-runner && cd ~/actions-runner
   # Use the version and checksum shown on the GitHub page, not one copied from here.
   curl -fsSLo runner.tar.gz https://github.com/actions/runner/releases/download/v<VERSION>/actions-runner-linux-x64-<VERSION>.tar.gz
   echo "<SHA256 from the page>  runner.tar.gz" | sha256sum -c -
   tar xzf runner.tar.gz
   ./config.sh --url https://github.com/<owner>/<repo> --token <TOKEN> \
     --name truenorth-lab-01 --labels truenorth-lab --unattended --replace
   ```

   `self-hosted`, `linux` and `x64` are added automatically; `truenorth-lab` is what
   `lab.yml` selects on.
3. Install and start it as a service so it survives reboots:

   ```bash
   sudo ./svc.sh install "$USER"
   sudo ./svc.sh start
   sudo ./svc.sh status
   ```

4. Confirm it shows **Idle** under *Settings -> Actions -> Runners*.
5. Add the repository secrets (*Settings -> Secrets and variables -> Actions*):

   | Secret | Example / meaning |
   |---|---|
   | `VSPHERE_URL` | `https://vcenter.lab.example` |
   | `VSPHERE_USERNAME`, `VSPHERE_PASSWORD` | the dedicated service account |
   | `VSPHERE_DATACENTER`, `VSPHERE_CLUSTER`, `VSPHERE_DATASTORE` | inventory names |
   | `VSPHERE_CONTENT_LIBRARY` | library holding the test template |
   | `VSPHERE_VERIFY_SSL` | `true` (or `false` for a self-signed lab vCenter) |
   | `LAB_PORT_GROUPS` | two or more pre-created, isolated port groups, comma separated |
   | `LAB_TEST_TEMPLATE` | content-library item that boots Linux with VMware Tools |

   `LAB_TEST_DATABASE_URL` is not a secret: the workflow points it at its own disposable
   PostgreSQL.

6. Run it once by hand (Actions -> "Lab (live vSphere)" -> Run workflow) and check the
   job log for the readiness timings the test prints.

## Operating notes

- **Cancelled runs can leak VMs.** The test reconciles leftovers by name at the end; a
  cancelled or timed-out job never gets there. Look for VMs named after lab range ids in
  the lab cluster and remove them; the next successful run's reconcile step only covers
  its own sessions.
- **Rotating the token / moving the runner:** `./config.sh remove --token <removal token>`
  on the old VM, then register again. Registration tokens are single-use and short-lived;
  nothing long-lived is stored except the runner's own credentials in `~/actions-runner`.
- **Updates:** the runner self-updates by default. Rebuilding the VM is preferred over
  patching it in place if it is ever suspected of compromise.
- **Repository visibility:** if this repository is ever made public, remove the runner
  first or restrict it with a runner group limited to this workflow.
