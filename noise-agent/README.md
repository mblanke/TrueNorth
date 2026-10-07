# tn-noise — background-noise agent

Runs inside range VMs and acts out synthetic personas, so there is believable everyday
traffic around the attack. It is **white-cell infrastructure and out of bounds** for
students: it takes orders only from the controller over the management NIC, and its
ground-truth log of what it did is visible only to holders of `noise:read`.

## Run

```bash
python -m tn_noise_agent --controller https://10.255.0.1:4200/api --token-file /etc/tn-noise/token
```

The flags:

- `--dry-run` fetches and reports the plan but touches no network.
- `--once` runs a single cycle and exits.
- `--ca-file` names the CA bundle for a privately signed controller certificate.

It uses only the standard library (Python 3.11+), so it needs no install step on an
air-gapped image.

## Turning it on for a range

Add a `noise:` block to the range template:

```yaml
noise:
  enabled: true
  preset: office          # or level: 0-100
  seed: 7
  personas: 30
  domain: corp.local
  mgmt: {vlan_id: 4001, cidr: 10.255.0.0/24, controller_url: "https://10.255.0.1/api"}
```

You can also add `noise: {agent: true|false}` to an individual node to opt it in or out.

When the range is provisioned on vSphere, every Linux agent VM gets a second VMXNET3
NIC on the management portgroup (`VSPHERE_NOISE_NETWORK`, default `TN-Noise-Mgmt`;
create it once, tagged with the noise VLAN, reachable from the controller only).

- The guest is configured through cloud-init's VMware datasource (`guestinfo.metadata`):
  - the management NIC gets a static address and no route;
  - the training NIC gets the template's address, its gateway, and the range's DCs as
    resolvers.
- This needs the Ubuntu golden image built by `infra/vsphere/packer/ubuntu-2404.pkr.hcl`
  (cloud-init + open-vm-tools, `cloud-init clean`).

- Agents go on workstations and user-simulation boxes by default.
- VLANs named like red, blue, soc, mgmt or management are excluded.

Once the range is ready, the white cell calls `POST /noise/ranges/{id}/deploy`
(`{"dry_run": true}` previews the run first). It does four things:

1. Derives the target pools from the template's servers.
2. Registers an agent on each node.
3. Creates the roster.
4. Has the worker run `infra/ansible/playbooks/deploy-noise-agents.yml` over the
   management network.

Windows nodes are listed as skipped until the Windows agent exists.

## How it works

1. The white cell enables noise for a range and sets the dial:
   `PUT /noise/ranges/{id}`, with a level from 0 to 100 or one of the presets
   `quiet`, `office`, `busy` and `chaos`. It also sets which hosts there are to talk to
   (`targets`).
2. Agents are registered per node with `POST /noise/ranges/{id}/agents`. Each node's
   token is returned once, and re-registering a node rotates its token.
3. Personas are created with `POST /noise/ranges/{id}/personas/roster` (built-in
   roster) and assigned to agent nodes.
4. Each agent polls `GET /noise/agent/plan`, which also serves as its heartbeat. It
   carries out each action when it falls due and posts the results to
   `POST /noise/agent/report`.

Plans are deterministic for a given seed, node and time window. Overlapping fetches
therefore agree, an agent never repeats an action, and an AAR can reconstruct exactly
what the background was meant to be.

## Activities (v1, Linux)

| Kind | What it does |
|---|---|
| `web_browse` | Opens a site and follows one or two links |
| `dns_lookup` | Resolves a name |
| `email_send` | Sends a short message through SMTP |
| `email_read` | Opens an IMAP connection (no login yet) |
| `file_share` | Connects to SMB/445 |
| `ad_logon` | Connects to Kerberos/88 |
| `ssh_admin` | Connects to SSH and reads the banner |
| `ntp_sync` | Makes an SNTP query |

There are also four lookalikes, done only by personas flagged for them (IT admins) and
only above dial level 20:

| Kind | What it does |
|---|---|
| `admin_scan` | Bounded port sweep |
| `admin_remote_exec` | Connects to RPC and SMB |
| `bad_password` | A few failed SMTP AUTH attempts |
| `bulk_upload` | Connects to SMB/445 |

Credentialed actions (IMAP and SMB with an account, Kerberos) need persona passwords
from an org pack. They arrive with the Windows agent.
