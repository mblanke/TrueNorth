#!/usr/bin/env bash
# TrueNorth work site - build the golden templates phase by phase (ADR 0012), on TN-MGMT01.
#
#   sudo scripts/lab/build-phase.sh status        # read-only: what exists where
#   sudo scripts/lab/build-phase.sh prereq        # packer init/validate, ISOs, DRS rule, checks
#   sudo scripts/lab/build-phase.sh depot --ssh-key /home/<you>/.ssh/id_ed25519_lab.pub
#   sudo scripts/lab/build-phase.sh base          # offline bases
#   sudo scripts/lab/build-phase.sh appliances    # pfsense vyos securityonion (heads-up each)
#   sudo scripts/lab/build-phase.sh derived       # Linux images cloned from ubuntu-lts (depot)
#   sudo scripts/lab/build-phase.sh roles         # Windows role images (precomp/detonation-host,
#                                                 #   srv2022-exchange2019/-sharepointse/-mecm)
#   sudo scripts/lab/build-phase.sh variants      # infra/vsphere/packer/variants/*.pkrvars.hcl
#
# Options:
#   --yes            answer yes to every y/N (appliance heads-ups still pause: TN_NO_PAUSE=1)
#   --only "a b"     only these ids of the phase
#   --rebuild        rebuild templates that already exist (default: skip the build, still
#                    replicate, test and register them)
#   --no-test        skip the test clones; the image is then NOT registered built
#   --admin UPN      TrueNorth administrator to register as (or TN_ADMIN_UPN); without it the
#                    registration command is printed instead
#   --ssh-key PUB    depot: public key for tnadmin@TN-DEPOT01
#   --range-pg PG    depot: the range uplink port group for the depot's third NIC
#                    (10.30.32.10, #214). Default $TN_RANGE_UPLINK_PG, else dPG-TN-SVC.
#   --dry-run        print what each phase would do; change nothing
#   --env-file FILE  default /srv/truenorth/config/.env.production (vCenter for govc)
#   --app-dir DIR    installed app (compose file), default /srv/truenorth/app
#
# After each template builds: build.sh replicate <id> (esx02-04), test-clone.sh <id>
# (decision 9), then register_golden_image_inprocess.py marks it built. Every step that
# changes vCenter says what it will do and asks y/N, unless --yes. Builds are approved once
# per phase (decision 11). Re-running a phase is safe: existing templates are not rebuilt,
# replicate skips today's copies, registration skips images already built.
#
# Run it from the template-kit checkout (/srv/truenorth/template-kit), never from the
# installed app checkout. Packer reads infra/vsphere/packer/lab.auto.pkrvars.hcl, written by
# scripts/lab/pkrvars-work.py.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
REPO="$(cd "$LAB_DIR/../.." && pwd)"
PACKER_DIR="$REPO/infra/vsphere/packer"
PKRVARS="${TN_PKRVARS:-$PACKER_DIR/lab.auto.pkrvars.hcl}"   # TN_PKRVARS: tests only (Packer reads the default)
ISOS="${TN_ISOS:-$REPO/install/lab/work-site/isos.conf}"
WORK_SITE="$REPO/install/lab/work-site"
ENV_FILE="${TN_ENV_FILE:-/srv/truenorth/config/.env.production}"
APP_DIR="${TN_APP_DIR:-/srv/truenorth/app}"
ADMIN="${TN_ADMIN_UPN:-}"
MGMT_VM="${TN_MGMT_VM:-TN-MGMT01}"
BUILD_HOST="${TN_BUILD_HOST:-esx04}"
BUILD_IP="10.30.31.20"
DEPOT_VM="TN-DEPOT01"; DEPOT_BUILD_IP="10.30.31.30"
DEPOT_URL_DEFAULT="http://${DEPOT_BUILD_IP}:8081/repository"
# The depot's third NIC (#214): range firewall WANs reach it there. TODO(site): the work
# site has no range uplink port group configured yet (the installer's
# tn_vsphere_range_uplink_network is empty). dPG-TN-SVC (VLAN 32) is the planned name:
# create it with scripts/lab/create-portgroups.sh --site work, or pass --range-pg.
RANGE_UPLINK_PG="${TN_RANGE_UPLINK_PG:-dPG-TN-SVC}"
DEPOT_RANGE_IP="10.30.32.10"; DEPOT_RANGE_MAC=""
# Packer reads the depot's raw "installers" repository as tn-build-read (#214). Its password
# is generated here, on TN-MGMT01, into this root-only file and never printed; depot.yml
# gets it in its environment, pkrvars-work.py --depot copies it into the vars file.
DEPOT_BUILD_ENV="${TN_DEPOT_BUILD_ENV:-/srv/truenorth/config/depot-build.env}"
RANGE_HOSTS="${RANGE_HOSTS:-esx02 esx03 esx04}"
DRS_VM_GROUP="tn-mgmt01-vm"; DRS_HOST_GROUP="tn-build-host"; DRS_RULE="tn-mgmt01-should-run-on-${BUILD_HOST}"

PHASE=""; ONLY=""; REBUILD=0; NO_TEST=0; SSH_PUB=""; DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --only) ONLY="$2"; shift ;;
    --rebuild) REBUILD=1 ;;
    --no-test) NO_TEST=1 ;;
    --admin) ADMIN="$2"; shift ;;
    --ssh-key) SSH_PUB="$2"; shift ;;
    --range-pg) RANGE_UPLINK_PG="$2"; shift ;;
    --dry-run) DRY=1 ;;
    --env-file) ENV_FILE="$2"; shift ;;
    --app-dir) APP_DIR="$2"; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1" >&2; usage 2 ;;
    *) [[ -z "$PHASE" ]] || usage 2; PHASE="$1" ;;
  esac
  shift
done
export ASSUME_YES="${ASSUME_YES:-0}"
# act() (lib.sh) only prints unless APPLY=1. Every act below is behind a y/N (confirm) and
# skipped in --dry-run, so outside a dry run the confirmed change is made. Not exported:
# child scripts get their own --apply. An APPLY inherited from the caller's environment
# (lib.sh reads it) never counts: a --dry-run must stay a dry run.
unset APPLY
APPLY=0
[[ "$DRY" == "1" ]] || APPLY=1

# ── phase contents (ADR 0012 decision 11) ─────────────────────────────
BASE_IDS="srv2022 ubuntu-lts win11-24h2 kali rocky debian13 parrot srv2025"
BASE_PENDING="win10-22h2:iso_win10 srv2019:iso_srv2019"   # added once their ISO is in
APPLIANCE_IDS="pfsense vyos securityonion"
DERIVED_IDS="remnux sift svc-emulators greyspace-host ca-host usersim cloudlog-emu"  # c2-server: gated
ROLE_IDS="precomp-host detonation-host"
# Server role images (roles.pkr.hcl): <id>:<iso vars it needs, comma-separated>. Each clones
# srv2022 and installs prerequisites from the depot; added once their media are in.
ROLE_SERVER="srv2022-exchange2019:iso_exchange2019 srv2022-sharepointse:iso_sharepointse,iso_sql2022 srv2022-sharepoint2019:iso_sharepoint2019,iso_sql2022,iso_srv2022 srv2022-mecm:iso_mecm,iso_sql2022"
role_server_ids() { local p; for p in $ROLE_SERVER; do printf '%s ' "${p%%:*}"; done; }

test_mode() {
  case "$1" in
    # No guest customization on the appliances. A clone must run VMware Tools AND publish
    # guestinfo.tn.template.finish, which only a finished template does (pfSense: the boot
    # hook reads /cf/conf/tn-template-finish.txt from tn-pfsense-finish.sh; VyOS:
    # tn-template-marker.service, from tn-vyos-install.py). Tools alone is not proof: a
    # hand-built VyOS has Tools without the cloud-init datasource fix. No marker: not
    # registered.
    pfsense|vyos) echo finish ;;
    securityonion) echo tools ;;  # OS-only template; so-setup runs per range (decision 8)
    *) echo customize ;;  # the worker's path: Windows Sysprep spec, Linux cloud-init guestinfo
  esac
}

appliance_note() {
  case "$1" in
    pfsense) echo "Keystroke build to the installer's Shell, then tn-pfsense-finish.sh (second CD) adds Open-VM-Tools offline, the boot hook and the template config, and powers off. Watch the console in vCenter: a 'tn-finish: FAILED' line means no template (appliances.pkr.hcl)." ;;
    vyos) echo "Logs in on the live console, then tn-vyos-install.py (second CD) answers 'install image' by prompt text and powers off. Watch the console in vCenter: '[tn-vyos-install] FAILED' means no template." ;;
    securityonion) echo "OS-only install (so-setup runs per range). The first time, finish the OS install by hand if the keystrokes miss, then let Packer template it." ;;
  esac
}

# ── helpers ───────────────────────────────────────────────────────────
run_or_show() { if [[ "$DRY" == "1" ]]; then echo "  [dry-run] $*"; else echo "  + $*"; "$@"; fi; }
# A template, not just a VM of that name: a build still in progress is a VM called <id>, and
# treating it as built replicated a half-installed pfsense (2026-10-20). TN-MGMT01/TN-DEPOT01
# are VMs, so exists_vm keeps the plain name check for them.
exists() { [[ -n "$(govc find / -type m -name "$1" -config.template true 2>/dev/null | head -n1)" ]]; }
exists_vm() { [[ -n "$(govc find / -type m -name "$1" 2>/dev/null | head -n1)" ]]; }

filter_only() { # the ids of "$@" that --only allows, on one line (bash 3.2: no mapfile)
  local id o out=()
  for id in "$@"; do
    if [[ -z "$ONLY" ]]; then out+=("$id"); continue; fi
    for o in $ONLY; do if [[ "$o" == "$id" ]]; then out+=("$id"); fi; done
  done
  echo "${out[*]-}"
}

iso_size() { # iso_size <datastore> <path>: bytes, or empty when it is not there
  govc datastore.ls -json -l -ds "$1" "$2" 2>/dev/null \
    | jq -r "$JQ_CI"' [ci("fileSize")[] | select(type == "number")][0] // empty' 2>/dev/null || true
}

# The ISO for packer variable $1 is buildable: listed in isos.conf (not PENDING), in the
# vars file, and on esx04-local with EXACTLY the size it has on the source (esx01-local, the
# copy iso-sync-esx04.sh makes it from), so a partial or stale copy is not ready. When
# isos.conf pins sha256=<hex>, iso-sync-esx04.sh --verify must also have recorded a match
# for this size. ISO_WHY says why not.
ISO_SRC_DS="esx01-local"; ISO_BUILD_DS="esx04-local"
ISO_STATE="${TN_ISO_STATE:-/var/lib/truenorth/iso-verified}"
ISO_WHY=""
iso_ready() {
  local var="$1" rel src dst sum
  rel="$(awk -v v="$var" '{sub(/#.*/, "")} $1 == v && $2 != "PENDING" {print $2}' "$ISOS" | head -n1)"
  [[ -n "$rel" ]] || { ISO_WHY="$var is PENDING or not in isos.conf"; return 1; }
  grep -q "^${var} = " "$PKRVARS" 2>/dev/null || { ISO_WHY="$var is not in the vars file (pkrvars-work.py)"; return 1; }
  dst="$(iso_size "$ISO_BUILD_DS" "ISO/$rel")"
  [[ -n "$dst" ]] || { ISO_WHY="ISO/$rel is not on $ISO_BUILD_DS (iso-sync-esx04.sh --apply)"; return 1; }
  src="$(iso_size "$ISO_SRC_DS" "ISO/$rel")"
  [[ -n "$src" ]] || { ISO_WHY="ISO/$rel is not on $ISO_SRC_DS: the $ISO_BUILD_DS copy cannot be checked"; return 1; }
  [[ "$dst" == "$src" ]] || { ISO_WHY="ISO/$rel is $dst bytes on $ISO_BUILD_DS, $src on $ISO_SRC_DS (partial or stale; iso-sync-esx04.sh --apply)"; return 1; }
  sum="$(awk -v r="$rel" '{sub(/#.*/, "")} $2 == r {for (i = 3; i <= NF; i++) if ($i ~ /^sha256=/) {sub(/^sha256=/, "", $i); print tolower($i)}}' "$ISOS" | head -n1)"
  if [[ -n "$sum" && "$(cat "$ISO_STATE/$rel.sha256" 2>/dev/null)" != "$sum $dst" ]]; then
    ISO_WHY="ISO/$rel pins sha256 in isos.conf but no matching check is recorded (iso-sync-esx04.sh --verify)"
    return 1
  fi
  ISO_WHY=""
}

# The isos.conf variable an ISO-built template boots from (empty: a clone, no ISO of its own).
iso_var_of() {
  case "$1" in
    srv2022) echo iso_srv2022 ;; srv2025) echo iso_srv2025 ;; srv2019) echo iso_srv2019 ;;
    win11-24h2) echo iso_win11_24h2 ;; win10-22h2) echo iso_win10 ;;
    ubuntu-lts) echo iso_ubuntu ;; debian13) echo iso_debian ;; rocky) echo iso_rocky ;;
    kali) echo iso_kali ;; parrot) echo iso_parrot ;;
    pfsense) echo iso_pfsense ;; vyos) echo iso_vyos ;; securityonion) echo iso_securityonion ;;
    *) echo "" ;;
  esac
}

pkrvars_check() {
  local relax=(); [[ "${TN_ALLOW_NON_ROOT:-0}" == "1" ]] && relax=(--allow-non-root)
  python3 "$LAB_DIR/pkrvars-work.py" --check --out "$PKRVARS" ${relax[@]+"${relax[@]}"}
}
depot_url_set() { pkrvars_check 2>/dev/null | grep -q '^depot_url=.\+'; }

register() { # register <id> [extra args for register_golden_image_inprocess.py]
  local id="$1"; shift
  local compose="$APP_DIR/infra/platform/docker/compose.prod.yml"
  # The build record build.sh wrote and replicate filled in (ADR 0013 §2): recorded with the
  # image, never promoted. An admin promotes it on the Golden Images page.
  if [[ -f "$PACKER_DIR/logs/$id.build.json" ]]; then
    set -- "$@" --build-record "/srcapp/infra/vsphere/packer/logs/$id.build.json"
  else
    warn "$id: no build record ($PACKER_DIR/logs/$id.build.json): registered built with no build to promote." \
      "Ranges need TN_TEMPLATE_ALLOW_UNPROMOTED until '$PACKER_DIR/build.sh manifest $id' + replicate (new REPLICA_DATE)."
  fi
  local cmd=(docker compose -f "$compose" --env-file "$ENV_FILE" run --rm --no-deps -T -w /app
             -e PYTHONPATH=/app -v "$REPO:/srcapp:ro" --entrypoint python api
             /srcapp/scripts/register_golden_image_inprocess.py --admin "${ADMIN:-<admin upn>}"
             --id "$id" --template "$id" "$@")
  if [[ -z "$ADMIN" || "$DRY" == "1" ]]; then
    [[ -n "$ADMIN" ]] || warn "$id: not registered (no --admin / TN_ADMIN_UPN). Run:"
    echo "  [later] (cd $(dirname "$compose") && $(printf "%q " "${cmd[@]}"))"
    return 0
  fi
  echo "  + register $id as built (in-process, as $ADMIN)"
  (cd "$(dirname "$compose")" && "${cmd[@]}")
}

# Replicate to the range hosts, test-clone, register. $1 id, $2 test mode, rest: register args.
post_build() {
  local id="$1" mode="$2"; shift 2
  local yes=(); [[ "$ASSUME_YES" == "1" ]] && yes=(--yes)
  step "$id: replicate to $RANGE_HOSTS, test clone ($mode), register"
  if [[ "$DRY" == "1" ]]; then
    echo "  [dry-run] RANGE_HOSTS='$RANGE_HOSTS' $PACKER_DIR/build.sh replicate $id"
    [[ "$NO_TEST" == "1" ]] || echo "  [dry-run] $LAB_DIR/test-clone.sh $id --mode $mode --hosts '$RANGE_HOSTS'"
    register "$id" "$@"; return 0
  fi
  DRY_RUN=1 RANGE_HOSTS="$RANGE_HOSTS" "$PACKER_DIR/build.sh" replicate "$id" | sed 's/^/  /'
  if confirm "Make the copies of $id above on $RANGE_HOSTS?"; then
    RANGE_HOSTS="$RANGE_HOSTS" "$PACKER_DIR/build.sh" replicate "$id" || { warn "$id: replicate failed; not registered"; return 1; }
  else
    warn "$id: replicate skipped (not confirmed); not registered"; return 1
  fi
  if [[ "$NO_TEST" == "1" ]]; then warn "$id: --no-test: built and replicated but NOT registered built (decision 9)"; return 0; fi
  "$LAB_DIR/test-clone.sh" "$id" --mode "$mode" --hosts "$RANGE_HOSTS" --env-file "$ENV_FILE" ${yes[@]+"${yes[@]}"} \
    || { warn "$id: test clone failed; not registered"; return 1; }
  register "$id" "$@"
}

# Build (unless it exists), then post_build. $1 id, $2 test mode, rest: register args.
build_and_post() {
  local id="$1" mode="$2"; shift 2
  if exists "$id" && [[ "$REBUILD" != "1" ]]; then
    info "$id: template exists; not rebuilt (--rebuild to rebuild)"
  elif [[ -n "$(iso_var_of "$id")" ]] && ! iso_ready "$(iso_var_of "$id")"; then
    # Never build from a missing, partial or unverified ISO (a 2026-10 copy was truncated).
    warn "$id: NOT BUILDABLE: $ISO_WHY"
    return 1
  elif [[ "$DRY" == "1" ]]; then
    echo "  [dry-run] (cd $PACKER_DIR && ./build.sh $id)"
  else
    step "$id: packer build (log: $PACKER_DIR/logs/)"
    env -u TN_API_URL -u TN_API_TOKEN -u REPLICATE_AFTER_BUILD "$PACKER_DIR/build.sh" "$id" \
      || { warn "$id: build failed"; return 1; }
  fi
  post_build "$id" "$mode" "$@"
}

approve_phase() { # approve_phase <what> <ids...>
  local what="$1"; shift
  [[ "$#" -gt 0 ]] || { echo "Nothing to build in $what."; return 1; }
  step "Phase $PHASE: $what"
  info "templates: $*"
  info "build on $BUILD_HOST (esx04-local), callbacks on $BUILD_IP, then copies on $RANGE_HOSTS"
  [[ "$DRY" == "1" ]] && return 0
  confirm "Approve the $PHASE phase (builds create and overwrite VMs/templates in vCenter)?" \
    || die "phase not approved; nothing built"
}

run_phase() { # run_phase <what> <ids...>: approve once, then build each
  local what="$1" id failed=0; shift
  approve_phase "$what" "$@" || return 0
  for id in "$@"; do build_and_post "$id" "$(test_mode "$id")" || failed=1; done
  return "$failed"
}

require_root() {
  [[ "${EUID:-$(id -u)}" == "0" || "${TN_ALLOW_NON_ROOT:-0}" == "1" ]] \
    || die "run with sudo: the Packer vars file and .env.production are root-only"
}

preflight() {
  require_root
  for t in govc jq python3; do need "$t" "install/lab/work-site: ansible-playbook ../build.yml"; done
  if [[ -z "${GOVC_USERNAME:-}" || -z "${GOVC_PASSWORD:-}" ]]; then govc_from_env_file "$ENV_FILE"; fi
  lab_preflight
  CLUSTER_PATH="$(govc find / -type c -name "$TN_CLUSTER" | head -n1)"
  [[ -n "$CLUSTER_PATH" ]] || die "cluster $TN_CLUSTER not found"
}

# ── phases ────────────────────────────────────────────────────────────
phase_status() {
  step "Templates (inventory) and host-local copies"
  local id copies
  for id in $BASE_IDS win10-22h2 srv2019 $APPLIANCE_IDS $DERIVED_IDS $ROLE_IDS $(role_server_ids); do
    copies="$(govc find / -type m -name "${id}@*" 2>/dev/null | sed 's#.*/##' | tr '\n' ' ')"
    printf '  %-22s %-8s %s\n' "$id" "$(exists "$id" && echo built || echo -)" "${copies:-}"
  done
  step "Depot"
  if exists_vm "$DEPOT_VM"; then info "$DEPOT_VM exists"; else info "$DEPOT_VM not deployed"; fi
  pkrvars_check || true
}

phase_prereq() {
  step "Tools"
  local t
  for t in packer xorriso curl openssl docker; do
    if command -v "$t" >/dev/null 2>&1; then info "ok    $t"; else warn "missing $t (install/lab/work-site: ansible-playbook ../build.yml)"; fi
  done
  need packer "install/lab/work-site: ansible-playbook ../build.yml"

  step "Packer vars ($PKRVARS)"
  pkrvars_check || die "fix the vars file: sudo python3 $LAB_DIR/pkrvars-work.py"

  step "Build network on this host"
  if ip -4 -o addr show 2>/dev/null | grep -q " ${BUILD_IP}/"; then info "ok    ${BUILD_IP} is configured here"
  else die "${BUILD_IP} (http_bind_address) is not on this host: Packer callbacks would fail. Run this on TN-MGMT01"; fi
  if systemctl is-active --quiet tn-build-dhcp.service 2>/dev/null; then info "ok    tn-build-dhcp.service (DHCP 10.30.31.100-199)"
  else warn "tn-build-dhcp.service is not active: build VMs get no address on dPG-TN-BUILD"; fi

  step "vCenter objects"
  local lib dc folder
  lib="$(awk -F'"' '/^content_library = /{print $2}' "$PKRVARS")"
  if ! grep -q '^publish_to_library = true' "$PKRVARS"; then
    info "skip  content library (publish_to_library = false: ranges clone the host-local copies)"
  elif govc library.info "/${lib:-TrueNorth-Templates}" >/dev/null 2>&1; then info "ok    content library ${lib:-TrueNorth-Templates}"
  else die "content library ${lib:-TrueNorth-Templates} not found (or not readable by this account); re-run pkrvars-work.py without --library to skip publishing"; fi
  dc="$GOVC_DATACENTER"
  for folder in "/$dc/vm/TrueNorth" "/$dc/vm/TrueNorth/Templates"; do
    if govc folder.info "$folder" >/dev/null 2>&1; then info "ok    folder $folder"
    else
      info "folder $folder does not exist"
      if [[ "$DRY" == "1" ]]; then echo "  [dry-run] govc folder.create $folder"
      elif confirm "Create VM folder $folder?"; then act govc folder.create "$folder"
      else die "the Packer builds need $folder"; fi
    fi
  done

  step "packer init + validate"
  run_or_show bash -c "cd '$PACKER_DIR' && packer init . && packer validate ."

  step "ISOs on esx04-local"
  if [[ "$DRY" == "1" ]]; then "$LAB_DIR/iso-sync-esx04.sh" --env-file "$ENV_FILE"
  else
    local yes=(); [[ "$ASSUME_YES" == "1" ]] && yes=(--yes)
    # One ISO that cannot be copied (e.g. locked by a VM that mounts it) must not stop the
    # rest of prereq: the builds that need it fail their own iso check later.
    "$LAB_DIR/iso-sync-esx04.sh" --apply --env-file "$ENV_FILE" ${yes[@]+"${yes[@]}"} \
      || warn "ISO sync incomplete (see FAILED above); continuing. A file locked by a running VM: try iso-sync-esx04.sh --stream"
  fi
  # What the sync left buildable: each template's ISO against the source (iso_ready). The
  # builds refuse the rest (build_and_post), so this list is what the phases will skip.
  local id var not_buildable=0
  for id in $BASE_IDS win10-22h2 srv2019 $APPLIANCE_IDS; do
    var="$(iso_var_of "$id")"
    [[ -n "$var" ]] || continue
    if iso_ready "$var"; then info "ok    $id ($var)"
    else warn "NOT BUILDABLE $id: $ISO_WHY"; not_buildable=$((not_buildable + 1)); fi
  done
  [[ "$not_buildable" == "0" ]] || warn "$not_buildable template(s) not buildable until their ISO is fixed (the builds will refuse them)"

  step "DRS: $MGMT_VM should run on $BUILD_HOST (VLAN 31 reaches only $BUILD_HOST)"
  local host_path groups rules drs
  host_path="$(govc find / -type h -name "${BUILD_HOST}" | head -n1)"
  [[ -n "$host_path" ]] || host_path="$(govc find / -type h -name "${BUILD_HOST}.*" | head -n1)"
  [[ -n "$host_path" ]] || die "host $BUILD_HOST not found"
  exists_vm "$MGMT_VM" || die "$MGMT_VM not found in inventory"
  drs="$(govc object.collect -s "$CLUSTER_PATH" configurationEx.drsConfig.enabled 2>/dev/null || true)"
  [[ "$drs" == "true" ]] || warn "DRS is not enabled on $TN_CLUSTER (${drs:-unknown}): the rule is kept but not acted on"
  groups="$(govc cluster.group.ls -cluster "$CLUSTER_PATH" 2>/dev/null || true)"
  rules="$(govc cluster.rule.ls -cluster "$CLUSTER_PATH" 2>/dev/null || true)"
  local todo=()
  grep -qx "$DRS_VM_GROUP" <<<"$groups" || todo+=("govc cluster.group.create -cluster $CLUSTER_PATH -name $DRS_VM_GROUP -vm $MGMT_VM")
  grep -qx "$DRS_HOST_GROUP" <<<"$groups" || todo+=("govc cluster.group.create -cluster $CLUSTER_PATH -name $DRS_HOST_GROUP -host ${host_path##*/}")
  grep -qx "$DRS_RULE" <<<"$rules" || todo+=("govc cluster.rule.create -cluster $CLUSTER_PATH -name $DRS_RULE -enable -mandatory=false -vm-host -vm-group $DRS_VM_GROUP -host-affine-group $DRS_HOST_GROUP")
  if [[ "${#todo[@]}" -eq 0 ]]; then info "ok    groups $DRS_VM_GROUP, $DRS_HOST_GROUP and rule $DRS_RULE exist"
  else
    local c
    for c in "${todo[@]}"; do info "plan  $c"; done
    if [[ "$DRY" != "1" ]] && confirm "Create the DRS should-rule above (non-mandatory; DRS may still move $MGMT_VM in an emergency)?"; then
      for c in "${todo[@]}"; do read -r -a argv <<<"$c"; act "${argv[@]}"; done
    elif [[ "$DRY" != "1" ]]; then warn "DRS rule not created"; fi
  fi
  local on mgmt_path
  mgmt_path="$(govc find / -type m -name "$MGMT_VM" | head -n1)"
  on="$(govc ls -L "$(govc object.collect -s "$mgmt_path" runtime.host 2>/dev/null || echo none)" 2>/dev/null || true)"
  if [[ "${on##*/}" == "${BUILD_HOST}"* ]]; then info "ok    $MGMT_VM is on ${on##*/}"
  else warn "$MGMT_VM is on '${on##*/}', not $BUILD_HOST: builds on $BUILD_HOST cannot reach $BUILD_IP until it moves back"; fi

  step "Next"
  info "sudo $0 depot --ssh-key <your .pub> [--admin <upn>]"
}

# The ethernet-N of VM path $1 backed by port group path $2 (never guessed by position).
nic_on_pg() {
  local vm="$1" key dev k
  key="$(govc object.collect -s "$2" key)"
  for dev in $(govc device.ls -vm "$vm" | awk '/^ethernet-/{print $1}'); do
    k="$(govc device.info -json -vm "$vm" "$dev" | jq -r "$JQ_CI"' ci("portgroupKey")[0] // empty')"
    if [[ -n "$k" && "$k" == "$key" ]]; then echo "$dev"; return 0; fi
  done
}

# TN_DEPOT_BUILD_READ_PASSWORD (#214): the Nexus tn-build-read password Packer uses for the
# installers repository. Generated once into $DEPOT_BUILD_ENV (0600, root), read into the
# environment for depot.yml. Never printed, never on a command line.
depot_build_secret() {
  local value=""
  if [[ -f "$DEPOT_BUILD_ENV" ]]; then
    value="$(sed -n 's/^TN_DEPOT_BUILD_READ_PASSWORD=//p' "$DEPOT_BUILD_ENV" | head -n1)"
  fi
  if [[ -z "$value" ]]; then
    if [[ "$DRY" == "1" ]]; then echo "  [dry-run] would generate TN_DEPOT_BUILD_READ_PASSWORD into $DEPOT_BUILD_ENV"; return 0; fi
    need openssl "apt install openssl"
    value="$(openssl rand -hex 24)"
    ( umask 077; mkdir -p "$(dirname "$DEPOT_BUILD_ENV")"
      printf '# TN-DEPOT01 tn-build-read (Packer reads the installers repository; #214). Root only.\nTN_DEPOT_BUILD_READ_PASSWORD=%s\n' "$value" >>"$DEPOT_BUILD_ENV" )
    chmod 0600 "$DEPOT_BUILD_ENV"
    info "ok    generated TN_DEPOT_BUILD_READ_PASSWORD into $DEPOT_BUILD_ENV (0600; not shown)"
  else
    info "ok    TN_DEPOT_BUILD_READ_PASSWORD in $DEPOT_BUILD_ENV (not shown)"
  fi
  export TN_DEPOT_BUILD_READ_PASSWORD="$value"
}

# The depot's range uplink NIC (#214): present, connected and starting connected. Adds or
# connects it after a y/N each. Sets DEPOT_RANGE_MAC for depot.yml (tn_depot_range_mac),
# which puts 10.30.32.10/24 on it and its host firewall in front of it.
depot_range_nic() {
  local vm pg nic power state
  case "$RANGE_UPLINK_PG" in
    ""|dPG-TN-MGMT|dPG-TN-BUILD)
      die "range uplink port group '$RANGE_UPLINK_PG': ranges must never share the depot's management or build network" ;;
  esac
  pg="$(govc find / -type g -name "$RANGE_UPLINK_PG" | head -n1)"
  if [[ -z "$pg" ]]; then
    warn "port group $RANGE_UPLINK_PG does not exist. TODO(site): create the range uplink network (scripts/lab/create-portgroups.sh --site work --apply, then its UniFi checklist), or pass --range-pg <name>. Until then ranges cannot reach the depot"
    return 1
  fi
  vm="$(govc find / -type m -name "$DEPOT_VM" | head -n1)"
  [[ -n "$vm" ]] || die "$DEPOT_VM not found"
  nic="$(nic_on_pg "$vm" "$pg")"
  if [[ -z "$nic" ]]; then
    info "plan  govc vm.network.add -vm $DEPOT_VM -net $RANGE_UPLINK_PG -net.adapter vmxnet3   (third NIC, for ${DEPOT_RANGE_IP})"
    if [[ "$DRY" == "1" ]]; then echo "  [dry-run] not added"; return 1; fi
    confirm "Add a third NIC on $RANGE_UPLINK_PG to $DEPOT_VM (range firewalls reach the depot there)?" \
      || { warn "range NIC not added: ranges cannot reach the depot"; return 1; }
    act govc vm.network.add -vm "$vm" -net "$RANGE_UPLINK_PG" -net.adapter vmxnet3
    nic="$(nic_on_pg "$vm" "$pg")"
    [[ -n "$nic" ]] || die "no NIC on $RANGE_UPLINK_PG after adding one"
  else
    info "ok    $nic is on $RANGE_UPLINK_PG"
  fi
  power="$(govc object.collect -s "$vm" runtime.powerState 2>/dev/null || true)"
  state="$(govc device.info -json -vm "$vm" "$nic" \
    | jq -r "$JQ_CI"' ci("connectable")[0] | "\(ci("connected")[0]) \(ci("startConnected")[0])"')"
  if [[ "${state#* }" != "true" || ( "$power" == "poweredOn" && "${state%% *}" != "true" ) ]]; then
    info "plan  govc device.connect -vm $DEPOT_VM $nic   (now: connected/startConnected = $state)"
    if [[ "$DRY" == "1" ]]; then echo "  [dry-run] not connected"
    elif confirm "Connect $nic of $DEPOT_VM (and at power-on)?"; then act govc device.connect -vm "$vm" "$nic"
    else warn "$nic left disconnected: ranges cannot reach the depot"; return 1; fi
  else
    info "ok    $nic connected, connects at power-on"
  fi
  DEPOT_RANGE_MAC="$(govc device.info -json -vm "$vm" "$nic" | jq -r "$JQ_CI"' ci("macAddress")[0] // empty')"
  info "ok    MAC $DEPOT_RANGE_MAC: depot.yml puts ${DEPOT_RANGE_IP}/24 on it, no gateway, firewalled to tcp/8081,3142"
}

phase_depot() {
  step "1/5 ubuntu-lts (TN-DEPOT01 is a clone of it)"
  if exists ubuntu-lts; then info "ok    ubuntu-lts exists"
  else
    pkrvars_check >/dev/null || die "run: sudo python3 $LAB_DIR/pkrvars-work.py"
    approve_phase "depot (ubuntu-lts first)" ubuntu-lts
    build_and_post ubuntu-lts customize || die "ubuntu-lts did not pass; the depot needs it"
  fi

  step "2/5 $DEPOT_VM (10.30.30.30 MGMT + ${DEPOT_BUILD_IP} BUILD + ${DEPOT_RANGE_IP} on $RANGE_UPLINK_PG, on $BUILD_HOST)"
  if exists_vm "$DEPOT_VM"; then info "ok    $DEPOT_VM exists (left alone; step 3/5 checks its range NIC)"
  else
    [[ -n "$SSH_PUB" ]] || die "--ssh-key <public key for tnadmin> is needed to deploy $DEPOT_VM"
    local deploy=("$LAB_DIR/deploy-mgmt-vm.sh" "$DEPOT_VM" --site work --ssh-key "$SSH_PUB" --data-disk "${TN_DEPOT_DATA_DISK:-500G}"
                  --range-pg "$RANGE_UPLINK_PG")
    "${deploy[@]}"   # plan only
    if [[ "$DRY" == "1" ]]; then echo "  [dry-run] ${deploy[*]} --apply"
    elif confirm "Deploy $DEPOT_VM as planned above?"; then "${deploy[@]}" --apply
    else die "$DEPOT_VM not deployed"; fi
  fi

  step "3/5 range uplink NIC (${DEPOT_RANGE_IP} on $RANGE_UPLINK_PG, #214)"
  local range_ok=1
  if exists_vm "$DEPOT_VM"; then depot_range_nic || range_ok=0
  else info "skip  $DEPOT_VM is not deployed yet"; range_ok=0; fi

  step "4/5 depot software (install/lab/depot.yml: apt-cacher-ng :3142, Nexus Chocolatey feed :8081, range NIC firewall, no forwarding)"
  local vault="$WORK_SITE/inventory/group_vars/all/vault.yml"
  depot_build_secret
  # --preserve-env: the secret reaches depot.yml in the environment, never on a command line.
  local as=(); [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]] && as=(sudo --preserve-env=TN_DEPOT_BUILD_READ_PASSWORD -u "$SUDO_USER" -H)
  local extra=(); [[ -n "$DEPOT_RANGE_MAC" ]] && extra=(-e "tn_depot_range_mac=$DEPOT_RANGE_MAC")
  if ! command -v ansible-playbook >/dev/null 2>&1; then
    warn "ansible-playbook is not on this host. From the control node: cd install/lab/work-site && ansible-playbook ../depot.yml --ask-vault-pass${DEPOT_RANGE_MAC:+ -e tn_depot_range_mac=$DEPOT_RANGE_MAC}, with TN_DEPOT_BUILD_READ_PASSWORD exported from $DEPOT_BUILD_ENV (install/lab/README.md; never print it)"
  elif [[ ! -f "$vault" ]]; then
    warn "no $vault: cp vault.yml.example vault.yml, fill two long random passwords, ansible-vault encrypt it; then re-run"
  elif [[ "$DRY" == "1" ]]; then echo "  [dry-run] (cd $WORK_SITE && ansible-playbook ../depot.yml --ask-vault-pass ${extra[*]-})"
  else
    (cd "$WORK_SITE" && ${as[@]+"${as[@]}"} ansible-galaxy collection install -r ../requirements.yml >/dev/null \
      && ${as[@]+"${as[@]}"} ansible-playbook ../depot.yml --ask-vault-pass ${extra[@]+"${extra[@]}"}) || die "depot.yml failed"
  fi

  step "5/5 the depot answers on the build network"
  local url ok=1 code
  # 401 counts as an answer: apt-cacher-ng's report page is behind AdminAuth.
  for url in "http://${DEPOT_BUILD_IP}:8081/repository/chocolatey/" "http://${DEPOT_BUILD_IP}:3142/acng-report.html"; do
    code="$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$code" == "200" || "$code" == "401" ]]; then info "ok    $url ($code)"; else warn "no answer: $url (${code:-none})"; ok=0; fi
  done
  # Windows builds bootstrap Chocolatey from the feed's own chocolatey .nupkg
  # (files/windows/choco-bootstrap.ps1), so the feed must list it.
  local probe="${DEPOT_URL_DEFAULT}/chocolatey/FindPackagesById()?id='chocolatey'"
  if curl -fsS -m 20 "$probe" 2>/dev/null | grep -q '<entry'; then info "ok    chocolatey package in ${DEPOT_URL_DEFAULT}/chocolatey"
  else warn "the depot feed has no 'chocolatey' package: Windows builds and variants cannot bootstrap Chocolatey (choco-bootstrap.ps1). Cache it: prefetch (tn_prefetch_choco_extra) or one install through chocolatey-proxy during an online window"; fi
  if [[ "$ok" == "1" ]]; then
    info "next: sudo python3 $LAB_DIR/pkrvars-work.py --depot   (depot_url = $DEPOT_URL_DEFAULT, apt_proxy = http://${DEPOT_BUILD_IP}:3142)"
  fi
  # TN-MGMT01 is not on the range uplink network and the depot's firewall admits only that
  # subnet there, so the range side is checked by depot.yml (on the depot) and on site from
  # a test range's firewall (install/lab/README.md, "Ranges reach the depot").
  if [[ "$range_ok" == "1" ]]; then
    info "ranges: TN_DEPOT_URL=http://${DEPOT_RANGE_IP}, TN_DEPOT_PORTS=8081,3142; set tn_vsphere_range_uplink_network=$RANGE_UPLINK_PG last (install/lab/README.md)"
  else
    warn "the range uplink NIC is not in place: ranges cannot reach the depot yet (step 3/5)"
  fi
}

phase_base() {
  local ids pair id var
  read -r -a ids <<<"$BASE_IDS"
  for pair in $BASE_PENDING; do
    id="${pair%%:*}"; var="${pair#*:}"
    if iso_ready "$var"; then ids+=("$id"); else info "skip  $id: $ISO_WHY"; fi
  done
  if depot_url_set; then warn "depot_url is set: ADR 0012 builds the bases offline. Re-run pkrvars-work.py without --depot first"; fi
  read -r -a ids <<<"$(filter_only "${ids[@]}")"
  run_phase "offline bases" ${ids[@]+"${ids[@]}"}
}

phase_appliances() {
  local ids id failed=0
  # shellcheck disable=SC2086 # the id lists are space-separated words
  read -r -a ids <<<"$(filter_only $APPLIANCE_IDS)"
  approve_phase "appliances (keystroke builds)" ${ids[@]+"${ids[@]}"} || return 0
  for id in "${ids[@]}"; do
    step "Heads-up: $id"
    info "$(appliance_note "$id")"
    if [[ "$DRY" != "1" && "${TN_NO_PAUSE:-0}" != "1" ]]; then
      local ans=""
      { read -r -p "  Open the console of '$id' in vCenter once it appears. Build $id now? [y/N] " ans </dev/tty; } 2>/dev/null || ans=""
      [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]] || { warn "$id skipped"; continue; }
    fi
    if [[ "$id" == "pfsense" ]] && ! { exists pfsense && [[ "$REBUILD" != "1" ]]; }; then
      pfsense_stage || { warn "pfsense: Open-VM-Tools packages not staged; not built"; failed=1; continue; }
    fi
    build_and_post "$id" "$(test_mode "$id")" || failed=1
  done
  return "$failed"
}

# pfSense: the template finish pkg-adds Open-VM-Tools from the build's second CD, offline
# (files/pfsense/pkgs.lock, sha256-pinned). Stage the .pkg files before building: from
# TN_PFSENSE_PKG_DIR when set, else the depot's installers repository (tn-build-read).
# Never from the internet. Without them the build would only fail later, at the console.
pfsense_stage() {
  local stage=(python3 "$LAB_DIR/stage_pfsense_pkgs.py")
  if [[ "$DRY" == "1" ]]; then
    echo "  [dry-run] ${stage[*]} check || ${stage[*]} stage --depot $DEPOT_URL_DEFAULT"
    return 0
  fi
  "${stage[@]}" check >/dev/null 2>&1 && { info "ok    pfSense packages staged (pkgs.lock)"; return 0; }
  if [[ -n "${TN_PFSENSE_PKG_DIR:-}" ]]; then
    "${stage[@]}" stage --from-dir "$TN_PFSENSE_PKG_DIR"
  else
    "${stage[@]}" stage --depot "$DEPOT_URL_DEFAULT" --depot-env "$DEPOT_BUILD_ENV"
  fi
}

phase_derived() {
  depot_url_set || die "derived images install packages: deploy the depot and run pkrvars-work.py --depot first"
  exists ubuntu-lts || die "ubuntu-lts is not built (base phase)"
  local ids
  # shellcheck disable=SC2086 # the id lists are space-separated words
  read -r -a ids <<<"$(filter_only $DERIVED_IDS)"
  run_phase "depot-dependent Linux images (clones of ubuntu-lts)" ${ids[@]+"${ids[@]}"}
}

phase_roles() {
  local ids=() pair id var ready
  if ! exists win10-22h2; then
    warn "win10-22h2 is not built (its ISO is pending): precomp-host and detonation-host clone it"
  else
    # shellcheck disable=SC2086 # the id lists are space-separated words
    read -r -a ids <<<"$(filter_only $ROLE_IDS)"
  fi
  # Server role images: srv2022 built, the depot set (prerequisites), every ISO ready.
  if ! exists srv2022; then
    warn "srv2022 is not built: the server role images clone it"
  elif ! depot_url_set; then
    warn "server role images install prerequisites from the depot: run pkrvars-work.py --depot first"
  else
    for pair in $ROLE_SERVER; do
      id="${pair%%:*}"; ready=1
      for var in $(tr ',' ' ' <<<"${pair#*:}"); do iso_ready "$var" || ready=0; done
      if [[ "$ready" == "1" ]]; then
        [[ -n "$(filter_only "$id")" ]] && ids+=("$id")
      else
        info "skip  $id: $(tr ',' ' ' <<<"${pair#*:}") PENDING in isos.conf, not on esx04-local, or not in the vars file yet"
      fi
    done
  fi
  info "server role images stage media and prerequisites only; the worker runs the domain-dependent setup at deploy (vsphere_products.py)."
  run_phase "Windows role images" ${ids[@]+"${ids[@]}"}
}

variant_meta() { sed -n -E "s/^[[:space:]]*$2[[:space:]]*=[[:space:]]*(.*[^[:space:]])[[:space:]]*$/\1/p" "$1" | head -n1; }

phase_variants() {
  local f name base fam ver role aliases names=() failed=0 args a
  for f in "$PACKER_DIR"/variants/*.pkrvars.hcl; do
    [[ -e "$f" ]] || continue
    name="$(basename "$f" .pkrvars.hcl)"
    [[ -n "$(filter_only "$name")" ]] || continue
    base="$(variant_meta "$f" variant_base)"; base="${base//\"/}"
    if exists "$base"; then names+=("$name"); else info "skip  $name: its base $base is not built"; fi
  done
  approve_phase "variants" ${names[@]+"${names[@]}"} || return 0
  for name in "${names[@]}"; do
    f="$PACKER_DIR/variants/$name.pkrvars.hcl"
    if [[ "$name" == *windows* || "$(variant_meta "$f" variant_os_family)" == '"windows"' ]] && ! depot_url_set; then
      warn "$name: Windows variant without depot_url installs from the community feed, which the build network cannot reach"
    fi
    if exists "$name" && [[ "$REBUILD" != "1" ]]; then info "$name: template exists; not rebuilt"
    elif [[ "$DRY" == "1" ]]; then echo "  [dry-run] (cd $PACKER_DIR && ./build.sh variant $name)"
    else
      env -u TN_API_URL -u TN_API_TOKEN "$PACKER_DIR/build.sh" variant "$name" || { warn "$name: build failed"; failed=1; continue; }
    fi
    fam="$(variant_meta "$f" variant_os_family)"; ver="$(variant_meta "$f" variant_version)"
    role="$(variant_meta "$f" variant_description)"; aliases="$(variant_meta "$f" variant_os_aliases)"
    args=(--variant --os-family "${fam//\"/}" --version "${ver//\"/}" --role "${role//\"/}")
    while IFS= read -r a; do [[ -n "$a" ]] && args+=(--alias "$a"); done < <(jq -r '.[]?' <<<"${aliases:-[]}" 2>/dev/null || true)
    post_build "$name" customize "${args[@]}" || failed=1
  done
  return "$failed"
}

[[ -n "$PHASE" ]] || usage 2
case "$PHASE" in
  status|prereq|depot|base|appliances|derived|roles|variants) ;;
  *) echo "unknown phase: $PHASE" >&2; usage 2 ;;
esac
step "Work-site build: phase $PHASE$([[ "$DRY" == "1" ]] && echo " (dry run)")"
preflight
case "$PHASE" in
  status) phase_status ;;
  prereq) phase_prereq ;;
  *)
    [[ "$PHASE" == "depot" ]] || pkrvars_check >/dev/null || die "run: sudo python3 $LAB_DIR/pkrvars-work.py"
    "phase_$PHASE" ;;
esac
