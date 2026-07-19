# TrueNorth Range — Synthetic Network Spec

Estate: **ESXi / vSphere only**. Every range is VLAN‑isolated with **deny‑egress** by default; a
pfSense (or NGFW/DFW) instance terminates every VLAN and owns the `.1` gateway. Reconciles the two
addressing schemes found in the repo (the `content/ranges/` `/24` scheme and the per‑PO scenario
`distributed_port_group` scheme).

## Two addressing schemes (both valid, different purposes)

### A. Range library topologies (`content/ranges/*/template.yaml`)
Human‑authored multi‑VLAN enterprise topologies. Convention: `10.<range-octet>.<vlan-lo>.0/24`,
gateway `.1` on pfSense, mirror/SPAN to a monitoring VLAN. VLAN‑per‑segment, tagged trunk.

| Range | VMs | Base | VLANs (id → name → CIDR) | Egress |
|---|---|---|---|---|
| soc-training | 25 | 10.60 | 200 attacker_infra .200.0/24 · 201 victim_network .201.0/24 · 202 soc_tools .202.0/24 · 203 network_monitoring .203.0/24 · 204 management .204.0/24 | deny (mirror all→203) |
| medium-enterprise | ~? | 10.10 | 100 corporate .100.0/24 · 200 servers .200.0/24 · 300 dmz .300.0/24 · 400 management .400.0/24 | deny |
| large-enterprise | 54 | 10.50 | 100 dmz · 101 corporate_lan · 102 server_vlan · 103 security_vlan · 104 ot_scada · 105 management · 106 ad_forest_b · 107 cloud_hybrid (all .10x.0/24) | deny |
| red-team | 30 | 10.70 | 300 target_corp .0.0/24 · 301 target_dmz .1.0/24 · 302 target_db .2.0/24 · 303 attacker_infra .3.0/24 · 304 pivot_network .4.0/24 | controlled (C2 lanes) |
| red-vs-blue | 40 | mixed | 100 management 10.0.0.0/24 · 200 corporate_lan 10.10.0.0/24 · 300 dmz 10.20.0.0/24 · 400 server_vlan 10.30.0.0/24 · 500 blue_team 10.40.0.0/24 · 666 red_team 10.66.0.0/24 | controlled |
| cloud-security | 20 | 10.80 | 400 cloud_sim .0.0/24 · 401 container_cluster .1.0/24 · 402 cicd_pipeline .2.0/24 · 403 monitoring .3.0/24 · 404 management .4.0/24 | deny |
| small-enterprise | ~? | — | uses abstract `assets:` + `policies:{egress,east_west}` schema, no explicit VLANs | policy‑driven |

**Note — VLAN 300 CIDRs:** `medium-enterprise` and `red-team` write `10.10.300.0/24` / `10.70.x`
style third octets; `300` is a VLAN id, not a valid octet — the CIDR third octet must stay ≤255.
Treat VLAN id and subnet octet independently when generating Terraform (id → 802.1Q tag; octet → address plan).

### B. Per‑PO scenario ranges (`truenorth-content-pack/.../scenarios/<PO>/range.tf`)
Minimal, single isolated segment for one assessed PO. Convention (from `PO_009_EXAMPLE` / `PO_007`):
```hcl
resource "vsphere_distributed_port_group" "range" {
  name    = "range-<PO>-${var.vlan_id}"   # e.g. range-PO009-909
  vlan_id = var.vlan_id                    # isolated; DFW policy = deny egress
}
```
- One `distributed_port_group`, one `vlan_id` per PO (900‑range ids observed: PO_009→909).
- Instant‑clone golden templates onto that port group; **no egress ever**.
- SecurityOnion sensor on the segment feeds OpenSearch (the telemetry the validators query).

## Standard firewall matrix (defensive ranges — from soc-training)
| src → dst | action | notes |
|---|---|---|
| attacker_infra → victim_network | allow | attacks must be visible to monitoring |
| victim_network → attacker_infra | allow :80,443,53,8080 | C2 callback channels |
| * → network_monitoring | mirror (SPAN) | full packet capture |
| soc_tools → victim_network | allow | investigation access |
| soc_tools → network_monitoring | allow | — |
| any → internet | **deny** | isolated range, no egress |

## Generation rules for Taz's `range.tf`
1. vSphere provider only; one `distributed_port_group` per PO with a unique `vlan_id`.
2. DFW/NGFW policy = deny egress; instant‑clone (never full‑clone) from catalogue `template_id`s.
3. Bake a SecurityOnion sensor on any range whose validators query OpenSearch telemetry.
4. Keep the assessed PO's segment minimal — do not import the big library topologies unless the PO
   conditions call for enterprise scale.

## Open items
- Assign a non‑overlapping `vlan_id` block per PO family (e.g. 90x = ALJQ, 91x = TEMP67) to avoid
  collisions at concurrent‑range scale — Standards to ratify.
- `red-vs-blue` references a `vyos-1.4` router with no golden image (see `vm_iso_catalogue.csv` GAP‑vyos):
  decide add‑image vs swap‑to‑pfsense before that range is buildable.
