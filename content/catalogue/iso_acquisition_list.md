# ISO Acquisition List — TrueNorth Range (vSphere/ESXi estate)

What each golden image needs media‑wise, where it comes from, and what's still missing. Target: stage
all ISOs on a vCenter datastore (`[datastore1] iso/...`); golden images build to the vCenter Content
Library via the vSphere Packer builder. Derived images (built on a linux/Windows base, no distinct
installer) are marked accordingly.

## Direct ISO downloads (free / evaluation — acquire from upstream)
| Image | ISO | Source | Notes |
|---|---|---|---|
| kali | kali-linux-2024.1-installer-amd64.iso | https://cdimage.kali.org/kali-2024.1/ | vSphere Packer already present |
| ubuntu-lts | ubuntu-24.04-live-server-amd64.iso **or** 22.04.3 | https://releases.ubuntu.com/ | ⚠ version conflict — catalogue=24.04, Packer files=22.04.3. Pin one. |
| rocky | Rocky-9-latest-x86_64-dvd.iso | https://download.rockylinux.org/pub/rocky/9/ | no Packer file yet |
| pfsense | pfSense-CE-2.7.2-RELEASE-amd64.iso | https://www.pfsense.org/download/ | on every range; vSphere Packer TODO |
| securityonion | securityonion-2.4.10-20240220.iso | https://securityonionsolutions.com | sensor appliance; vSphere Packer TODO |
| (GAP) vyos | vyos-1.4-rolling.iso | https://vyos.io | only if GAP‑vyos is approved as an image |

## Microsoft Evaluation Center (free eval media, 180‑day)
| Image | ISO | Notes |
|---|---|---|
| win10-22h2 | Win10_Enterprise_Eval_x64.iso (+ virtio-win.iso) | ⚠ range alias `windows-10-ltsc` — confirm 22H2 vs LTSC channel |
| win11-24h2 | Win11_Enterprise_Eval_x64.iso (+ virtio-win.iso) | no Packer file yet |
| srv2016 | SERVER_2016_EVAL_x64FRE_en-us.iso | distinct from 2019/2022 |
| srv2019 | SERVER_2019_EVAL_x64FRE_en-us.iso | distinct from 2022 |
| srv2022 | SERVER_EVAL_x64FRE_en-us.iso (+ virtio-win.iso) | ✅ vSphere Packer present |
| virtio-win | virtio-win.iso | https://fedorapeople.org/groups/virt/virtio-win/ — required alongside every Windows build |

## Licensed / entitlement‑gated (NO public eval — procurement action)
| Image | Blocker |
|---|---|
| win7-sp1 | No public eval ISO. Needs entitled volume‑license media. Default legacy host — resolve first. |
| win-xp-sp3 | **Disabled.** Licensing + EO justification required. Isolated no‑egress only. Prefer win7. |

## Derived images (no distinct ISO — built on a base image, on‑box)
| Image | Built from |
|---|---|
| remnux | ubuntu-lts + REMnux installer (on‑box mirror) |
| sift | ubuntu-lts + SANS SIFT installer (on‑box mirror) |
| svc-emulators, ca-host, usersim, cloudlog-emu | ubuntu-lts base, configured on‑box |
| precomp-host | snapshot of win10-22h2 / srv2019 with staged artifacts |
| detonation-host | win10-22h2 base, sterile (no egress ever) |
| blueteam-soc | composite multi‑service stack |
| c2-server | **AUTHOR‑REQUIRED / disabled** — Standards/instructor only |

## Summary of pending actions
1. **Resolve the ubuntu version conflict** (24.04 vs 22.04.3) before any linux golden build.
2. **Procure Win7 entitled media**; keep Win XP disabled pending justification.
3. **Author vSphere Packer files** for every `enabled=yes` image lacking one (all except srv2022, kali,
   ubuntu) — this is Taz task 1.
4. **Decide GAP‑vyos** (add image vs swap red‑vs‑blue router to pfSense).
5. **Stage virtio-win.iso** on the datastore — needed by every Windows build.
