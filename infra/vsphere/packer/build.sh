#!/usr/bin/env bash
# TrueNorth Range — vSphere Packer build driver.
#
# Usage:
#   ./build.sh list                     # templates in build order, then the variants
#   ./build.sh all                      # build every base + derived template, in order
#   ./build.sh srv2022 ubuntu-lts       # build the named templates (still in build order)
#   ./build.sh variant win11-analyst    # build one or more custom variants
#   ./build.sh variants                 # build every variant under variants/
#   ./build.sh register win11-analyst   # (re-)register a built variant with the API
#   ./build.sh replicate srv2022 [esx02 esx03]   # copy a built template to each range host
#   ./build.sh manifest ubuntu-lts      # adopt an existing template as a build (no rebuild)
#
# For each template: packer build -force -only='*.<builder>.<name>' . with a per-template log
# under logs/. A variant adds -var-file=variants/<name>.pkrvars.hcl and builds the
# variant-windows or variant-linux source (see variants.pkr.hcl).
#
# Build record (ADR 0013 §2): each successful build writes logs/<name>.build.json: build id
# (<name>-<UTC timestamp>, or TN_BUILD_ID), build date, ISO checksum (TN_ISO_CHECKSUM, e.g.
# sha256:<hex> of the install ISO; empty when not given), the sha256 of every Packer input
# file, and the build's checksum: sha256 of that identity (build id, template, ISO checksum,
# inputs). It fingerprints the build; it is not a hash of the disks. Replicate stamps each
# copy's annotation "tn-build: <id> <build id> sha256:<checksum>" and adds it to the record.
# Ranges clone only a PROMOTED build's stamped copies: the platform administrator promotes
# a registered build on the Golden Images page, and only a build that names host copies
# (after replicate) can be promoted. `manifest <id>` writes a record (not registered) for a
# template built before records existed; replicate it again (a new REPLICA_DATE) to stamp,
# record and register its copies. Recording a build needs a platform-admin API token.
#
# Register-back, when TN_API_URL and TN_API_TOKEN are set:
#   base/derived  PATCH the catalogue image to build_status=built, template_name=<name>, and
#                 the build record (build); replicate PATCHes the record again with its copies
#   variant       POST /golden-images (create or update) from the variant file's metadata,
#                 so it is offered in the Range Designer, then PATCH its build record
#
# Replicate (ADR 0012 decision 6; needs govc and GOVC_URL pointing at vCenter): clone the
# inventory template <id> to each range host's local datastore as <id>@<host>-<yyyymmdd>
# and mark it a template, stamped as the template's recorded build. A copy of that name that
# already exists must carry the same stamp (else replicate fails: set REPLICA_DATE).
#   RANGE_HOSTS          hosts when none are named (default "esx02 esx03 esx04")
#   RANGE_DATASTORE_FMT  printf format for a host's datastore (default "%s-local")
#   TEMPLATE_FOLDER      inventory folder path for the copies (default: the template's own folder)
#   REPLICATE_AFTER_BUILD=1  replicate every template right after it builds
#   DRY_RUN=1            print the govc changes instead of making them
#
# Tags (vCenter, best effort; scripts/lab/vcenter_organize.py tag): a built or replicated
# template gets tn-type=golden-template, each copy tn-type=template-copy, both with
# tn-template=<id> and tn-os from the catalogue. A failure is a warning, never a failed
# build (scripts/lab/vcenter-organize.sh tags anything missed). TN_TAGS=0 turns it off.
#
# packer init + packer validate run once up front.
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs

RANGE_HOSTS="${RANGE_HOSTS:-esx02 esx03 esx04}"
RANGE_DATASTORE_FMT="${RANGE_DATASTORE_FMT:-%s-local}"
TEMPLATE_FOLDER="${TEMPLATE_FOLDER:-}"

# Build-sheet order. ISO builds first, then the vsphere-clone derived images.
ORDER=(
  srv2022 win10-22h2 ubuntu-lts pfsense securityonion kali win11-24h2
  srv2019 srv2016 rocky vyos srv2025 debian13 parrot win10-ltsc
  remnux sift svc-emulators greyspace-host ca-host usersim cloudlog-emu c2-server
  precomp-host detonation-host
  srv2022-exchange2019 srv2022-sharepointse srv2022-sharepoint2019 srv2022-mecm
)

VARIANT_DIR="variants"

# Names built by the vsphere-clone builder (everything else is vsphere-iso).
is_clone() {
  case "$1" in
    remnux|sift|svc-emulators|greyspace-host|ca-host|usersim|cloudlog-emu|c2-server|precomp-host|detonation-host|srv2022-exchange2019|srv2022-sharepointse|srv2022-sharepoint2019|srv2022-mecm) return 0 ;;
    *) return 1 ;;
  esac
}

builder_of() { if is_clone "$1"; then echo "vsphere-clone"; else echo "vsphere-iso"; fi; }

# --- variants: one variants/<name>.pkrvars.hcl each ------------------------------------
variant_file() { echo "${VARIANT_DIR}/$1.pkrvars.hcl"; }

list_variants() {
  local f
  for f in "${VARIANT_DIR}"/*.pkrvars.hcl; do
    [ -e "$f" ] || continue
    basename "$f" .pkrvars.hcl
  done
}

# Raw right-hand side of a one-line `key = value` in a variant file. String and list
# values are written as JSON-compatible HCL ("x", ["a", "b"]), so the result feeds jq.
variant_meta() {
  local file="$1" key="$2"
  sed -n -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*(.*[^[:space:]])[[:space:]]*$/\1/p" "$file" | head -n1
}

variant_family() {
  local raw
  raw="$(variant_meta "$(variant_file "$1")" variant_os_family)"
  raw="${raw//\"/}"
  echo "${raw:-linux}"
}

print_list() {
  echo "Templates (build-sheet order):"
  for n in "${ORDER[@]}"; do printf '  %-22s %s\n' "$n" "$(builder_of "$n")"; done
  echo
  echo "Variants (${VARIANT_DIR}/, build with: ./build.sh variant <name>):"
  local v base
  while IFS= read -r v; do
    [ -n "$v" ] || continue
    base="$(variant_meta "$(variant_file "$v")" variant_base)"
    printf '  %-16s %-8s from %s\n' "$v" "$(variant_family "$v")" "${base//\"/}"
  done < <(list_variants)
}

in_order() {
  # Echo the requested names, reordered to match ORDER.
  local want=("$@") n w
  for n in "${ORDER[@]}"; do
    for w in "${want[@]}"; do
      if [ "$n" = "$w" ]; then echo "$n"; fi
    done
  done
}

# --- build records (ADR 0013 §2): logs/<id>.build.json ---------------------------------
BUILD_RECORDS="${TN_BUILD_RECORDS:-logs}"  # where build records live (tests point it elsewhere)
manifest_file() { mkdir -p "$BUILD_RECORDS"; echo "${BUILD_RECORDS%/}/$1.build.json"; }

sha256_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi
}

# sha256 over "<sha256>  <path>" of every Packer input (*.pkr.hcl, http/, files/, variants/), in name order.
inputs_digest() {
  local f
  { find . -maxdepth 1 -name '*.pkr.hcl' -type f; find http files variants -type f 2>/dev/null; } | LC_ALL=C sort |
    while IFS= read -r f; do printf '%s  %s\n' "$(sha256_stdin < "$f")" "$f"; done | sha256_stdin
}

# write_manifest <id>: the record of the build just made (or adopted). Needs jq.
write_manifest() {
  local id="$1" build_id core sum
  if ! command -v jq >/dev/null 2>&1; then
    echo "WARN: jq not found; no build record for ${id} (its copies will be unstamped and cannot be promoted)" >&2
    return 0
  fi
  build_id="${TN_BUILD_ID:-${id}-$(date -u +%Y%m%dT%H%M%SZ)}"
  core="$(jq -cn --arg b "$build_id" --arg t "$id" --arg i "${TN_ISO_CHECKSUM:-}" --arg p "$(inputs_digest)" \
    '{build_id:$b, template:$t, iso_checksum:$i, inputs_sha256:$p}')"
  sum="$(printf '%s' "$core" | sha256_stdin)"
  jq -n --argjson c "$core" --arg s "$sum" --arg d "$(date -u +%Y-%m-%d)" \
    '$c + {checksum:$s, build_date:$d, copies:{}}' > "$(manifest_file "$id")"
  echo "build record: ${id} = ${build_id} sha256:${sum} ($(manifest_file "$id"))"
}

# The "tn-build: <id> <build id> sha256:<checksum>" stamp of <id>'s record, or nothing.
build_stamp() {
  local m
  m="$(manifest_file "$1")"
  [ -f "$m" ] && command -v jq >/dev/null 2>&1 || return 0
  jq -r '"tn-build: \(.template) \(.build_id) sha256:\(.checksum)"' "$m"
}

record_copy() { # record_copy <id> <host short> <copy name>
  local m tmp
  m="$(manifest_file "$1")"
  [ -f "$m" ] && [ -z "${DRY_RUN:-}" ] || return 0
  tmp="$(mktemp)"
  jq --arg h "$2" --arg c "$3" '.copies[$h] = $c' "$m" > "$tmp" && mv "$tmp" "$m"
}

# The API's `build` body from <id>'s record, or nothing.
build_body() {
  local m
  m="$(manifest_file "$1")"
  [ -f "$m" ] || return 0
  jq -c '{build_id, checksum, build_date, iso_checksum, template_name: .template, copies}' "$m"
}

# --- register-back: look up the golden image by catalogue_id and PATCH it -------------
golden_image_id() {
  curl -fsS -H "Authorization: Bearer ${TN_API_TOKEN}" "${TN_API_URL%/}/golden-images?hypervisor=vsphere" \
    | jq -r --arg c "$1" '.[] | select(.catalogue_id == $c) | .id' | head -n1
}

# register_back <name> [build-only]: built + template name + the build record; with
# "build-only" the build record alone (after replicate added its copies).
register_back() {
  local name="$1" only="${2:-}"
  if [ -z "${TN_API_URL:-}" ] || [ -z "${TN_API_TOKEN:-}" ]; then
    return 0
  fi
  if ! command -v jq >/dev/null 2>&1; then
    echo "WARN: jq not found; skipping register-back for $name" >&2
    return 0
  fi
  local id build body
  echo "register-back: resolving golden image for catalogue_id=$name"
  id="$(golden_image_id "$name")"
  if [ -z "$id" ] || [ "$id" = "null" ]; then
    echo "WARN: no golden image with catalogue_id=$name on vsphere; skipping register-back" >&2
    return 0
  fi
  build="$(build_body "$name")"
  if [ -n "$only" ]; then
    [ -n "$build" ] || return 0
    body="$(jq -n --argjson b "$build" '{build:$b}')"
  else
    body="$(jq -n --arg t "$name" --argjson b "${build:-null}" \
      '{build_status:"built", template_name:$t} + (if $b then {build:$b} else {} end)')"
  fi
  curl -fsS -X PATCH \
    -H "Authorization: Bearer ${TN_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$body" \
    "${TN_API_URL%/}/golden-images/${id}" >/dev/null
  echo "register-back: ${name} -> ${only:+build record}${only:-built} (id=$id)${build:+; promote it on the Golden Images page}"
}

# --- variant registration: POST /golden-images (create or update) ---------------------
# Usage: register_variant <name> [required]. "required" makes missing API settings an
# error (the explicit `register` command); after a build they only skip.
register_variant() {
  local name="$1" required="${2:-}" file payload
  file="$(variant_file "$name")"
  if [ -z "${TN_API_URL:-}" ] || [ -z "${TN_API_TOKEN:-}" ]; then
    if [ -n "$required" ]; then
      echo "register: set TN_API_URL and TN_API_TOKEN (a token with infra:write)" >&2
      return 1
    fi
    return 0
  fi
  if ! command -v jq >/dev/null 2>&1; then
    echo "register: jq is required to register ${name}" >&2
    return 1
  fi
  local cid fam ver role aliases
  cid="$(variant_meta "$file" variant_name)"
  fam="$(variant_meta "$file" variant_os_family)"
  ver="$(variant_meta "$file" variant_version)"
  role="$(variant_meta "$file" variant_description)"
  aliases="$(variant_meta "$file" variant_os_aliases)"
  payload="$(jq -n \
    --argjson cid "${cid:-null}" \
    --argjson fam "${fam:-\"linux\"}" \
    --argjson ver "${ver:-\"\"}" \
    --argjson role "${role:-\"\"}" \
    --argjson aliases "${aliases:-[]}" \
    --arg status "${TN_VARIANT_BUILD_STATUS:-built}" \
    '{catalogue_id:$cid, os_family:$fam, version:$ver, role:$role, hypervisor:"vsphere",
      template_name:$cid, os_aliases:$aliases, build_status:$status, enabled:true}')" || {
    echo "register: could not read metadata from ${file}; keep each variant_* metadata key on one line" >&2
    return 1
  }
  curl -fsS -X POST \
    -H "Authorization: Bearer ${TN_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "${TN_API_URL%/}/golden-images" >/dev/null
  echo "register: ${name} -> golden image registered (selectable in the Range Designer)"
}

# --- base check: the variant clones an inventory template; fail early if it is absent --
check_variant_base() {
  local name="$1" base
  base="$(variant_meta "$(variant_file "$name")" variant_base)"
  base="${base//\"/}"
  if [ -z "$base" ]; then
    echo "FAIL: ${name}: variant_base is not set" >&2
    return 1
  fi
  if command -v govc >/dev/null 2>&1 && [ -n "${GOVC_URL:-}" ]; then
    if [ -z "$(govc find / -type m -name "$base" 2>/dev/null | head -n1)" ]; then
      echo "FAIL: ${name}: base template '${base}' is not in vCenter inventory; build it first (./build.sh ${base})" >&2
      return 1
    fi
    echo "base: ${base} found in inventory"
  else
    echo "NOTE: govc/GOVC_URL not set; not checking that base template '${base}' exists (the clone fails if it does not)"
  fi
}

# --- replicate: a copy of the template on every range host's local datastore ----------
# The hosts have local VMFS only, so a clone from a template on another host copies its
# disks across the network. Idempotent: today's copy that exists is left alone (a copy
# whose clone landed but whose mark-as-template did not is marked now).
run_govc() {
  if [ -n "${DRY_RUN:-}" ]; then
    echo "DRY_RUN: govc $*"
  else
    govc "$@"
  fi
}

# The inventory path of the one VM or template called exactly $1 (empty: none). Searched
# from the root, so it is found in any folder (TrueNorth/Templates or elsewhere) except
# */_retired/ (old hand-made templates moved aside by vcenter-organize.sh).
find_vm() {
  govc find / -type m -name "$1" 2>/dev/null | awk '!/\/_retired\//' | head -n1
}

# tn-* tags on a template or copy: tag_vm <inventory path> <tn-type> <catalogue id>.
TAG_TOOL="../../../scripts/lab/vcenter_organize.py"
tag_vm() {
  local path="$1" type="$2" id="$3"
  [ "${TN_TAGS:-1}" != "0" ] && [ -n "$path" ] || return 0
  if [ -n "${DRY_RUN:-}" ]; then
    echo "DRY_RUN: tag ${path} tn-type=${type} tn-template=${id}"
    return 0
  fi
  if [ ! -f "$TAG_TOOL" ] || ! command -v python3 >/dev/null 2>&1; then
    echo "WARN: ${path##*/} not tagged (python3 or ${TAG_TOOL} missing); scripts/lab/vcenter-organize.sh tags it later" >&2
    return 0
  fi
  python3 "$TAG_TOOL" tag "$path" --type "$type" --template "$id" \
    || echo "WARN: ${path##*/} not tagged (vCenter tagging failed); scripts/lab/vcenter-organize.sh tags it later" >&2
}

# The inventory path of host $1: its exact name, else <name>.<domain> (esx03 -> esx03.truenorth.lab).
find_host() {
  local h="$1" path
  path="$(govc find / -type h -name "$h" 2>/dev/null | head -n1)"
  [ -n "$path" ] || path="$(govc find / -type h -name "${h}.*" 2>/dev/null | head -n1)"
  echo "$path"
}

replicate_one() {
  local id="$1" host="$2" date="$3" source="$4" folder="$5" stamp="$6" short ds copy existing host_path have
  local annotation=()
  short="${host%%.*}"
  copy="${id}@${short}-${date}"
  # shellcheck disable=SC2059 # the format is the operator's RANGE_DATASTORE_FMT
  ds="$(printf "$RANGE_DATASTORE_FMT" "$short")"
  existing="$(find_vm "$copy")"
  if [ -n "$existing" ]; then
    # A copy of this name is reused only when it is this build (ADR 0013 §2).
    if [ -n "$stamp" ]; then
      have="$(govc object.collect -s "$existing" config.annotation 2>/dev/null | grep '^tn-build:' || true)"
      if [ "$have" != "$stamp" ]; then
        echo "FAIL: replicate ${id}: ${copy} exists as ${have:-an unstamped copy}, not ${stamp}; set REPLICA_DATE (or remove it)" >&2
        return 1
      fi
    fi
    if [ "$(govc object.collect -s "$existing" config.template 2>/dev/null)" = "true" ]; then
      echo "replicate: ${copy} already exists; skipped"
    else
      echo "replicate: ${copy} exists but is not a template yet; marking it"
      run_govc vm.markastemplate "$existing"
    fi
    tag_vm "$existing" template-copy "$id"
    record_copy "$id" "$short" "$copy"
    return 0
  fi
  host_path="$(find_host "$short")"
  if [ -z "$host_path" ]; then
    echo "FAIL: replicate ${id}: host '${short}' is not in vCenter inventory" >&2
    return 1
  fi
  echo "replicate: ${id} -> ${copy} on ${host_path} [${ds}]${stamp:+ ($stamp)}"
  [ -z "$stamp" ] || annotation=(-annotation="TrueNorth golden copy of ${id}"$'\n'"${stamp}")
  run_govc vm.clone -vm "$source" -on=false -template=false -link=false \
    -host="$host_path" -ds="$ds" -folder="$folder" ${annotation[@]+"${annotation[@]}"} "$copy" || {
    echo "FAIL: replicate ${id}: clone to ${short} failed" >&2
    return 1
  }
  run_govc vm.markastemplate "${folder%/}/${copy}" || {
    echo "FAIL: replicate ${id}: ${copy} cloned but not marked a template (re-run replicate to finish)" >&2
    return 1
  }
  tag_vm "${folder%/}/${copy}" template-copy "$id"
  record_copy "$id" "$short" "$copy"
  echo "replicate: OK ${copy}"
}

replicate() {
  local id="$1" date source folder stamp failed=0 h hosts=()
  shift
  if [ "$#" -gt 0 ]; then hosts=("$@"); else read -r -a hosts <<< "$RANGE_HOSTS"; fi
  if ! command -v govc >/dev/null 2>&1 || [ -z "${GOVC_URL:-}" ]; then
    echo "replicate: govc and GOVC_URL (the vCenter) are required" >&2
    return 1
  fi
  # Against vCenter only: vm.markastemplate on a vCenter-managed ESXi host desyncs inventory.
  if [ "$(govc about 2>/dev/null | sed -n -E 's/^API type:[[:space:]]*([^[:space:]]+).*/\1/p' | head -n1)" != "VirtualCenter" ]; then
    echo "replicate: GOVC_URL must point at vCenter, not an ESXi host" >&2
    return 1
  fi
  source="$(find_vm "$id")"
  if [ -z "$source" ]; then
    echo "FAIL: replicate: template '${id}' is not in vCenter inventory; build it first (./build.sh ${id})" >&2
    return 1
  fi
  folder="${TEMPLATE_FOLDER:-$(dirname "$source")}"  # default: next to the template it copies
  date="${REPLICA_DATE:-$(date +%Y%m%d)}"
  stamp="$(build_stamp "$id")"
  if [ -z "$stamp" ]; then
    echo "WARN: replicate ${id}: no build record ($(manifest_file "$id")): the copies are unstamped and no range" \
      "will clone them once a build is promoted. Rebuild, or './build.sh manifest ${id}' then replicate with a new REPLICA_DATE." >&2
  fi
  tag_vm "$source" golden-template "$id"
  for h in "${hosts[@]}"; do
    replicate_one "$id" "$h" "$date" "$source" "$folder" "$stamp" || failed=1
  done
  [ -z "$stamp" ] || [ -n "${DRY_RUN:-}" ] || register_back "$id" build-only || failed=1
  return "$failed"
}

# cd_files/cd_content become an ISO that Packer uploads to [vsphere_datastore] packer_cache/
# (Linux seeds with the build password hash, the Windows autounattend with the build
# Administrator password, pfSense/VyOS finish files). The plugin deletes it when the build
# ends (remote_cache_cleanup = true removes the cache as well); this sweep catches what a
# killed or crashed Packer left behind. After every build, success or failure; needs govc.
sweep_cd_cache() {
  local ds f
  if ! command -v govc >/dev/null 2>&1 || [ -z "${GOVC_URL:-}" ]; then
    echo "NOTE: govc/GOVC_URL not set; not sweeping packer_cache for leftover CD images"
    return 0
  fi
  ds="${BUILD_DATASTORE:-$(sed -n -E 's/^vsphere_datastore[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' lab.auto.pkrvars.hcl 2>/dev/null | head -n1)}"
  [ -n "$ds" ] || { echo "NOTE: no vsphere_datastore in lab.auto.pkrvars.hcl; not sweeping packer_cache"; return 0; }
  for f in $(govc datastore.ls -ds "$ds" packer_cache 2>/dev/null | grep -E '\.iso$' || true); do
    run_govc datastore.rm -f -ds "$ds" "packer_cache/$f" || echo "WARN: could not delete [$ds] packer_cache/$f" >&2
  done
}

build_one() {
  local name="$1" builder log ts rc=0
  builder="$(builder_of "$name")"
  ts="$(date +%Y%m%d-%H%M%S)"
  log="logs/${name}-${ts}.log"
  echo "============================================================"
  echo "Building ${name} (${builder})  ->  ${log}"
  echo "============================================================"
  packer build -force -only="*.${builder}.${name}" . 2>&1 | tee "$log" || rc=1
  sweep_cd_cache
  if [ "$rc" = 0 ]; then
    echo "OK: ${name}"
    write_manifest "$name"
    register_back "$name"
    if command -v govc >/dev/null 2>&1 && [ -n "${GOVC_URL:-}" ]; then
      tag_vm "$(find_vm "$name")" golden-template "$name"
    fi
    if [ "${REPLICATE_AFTER_BUILD:-}" = "1" ]; then
      replicate "$name" || return 1
    fi
  else
    echo "FAIL: ${name} (see ${log})" >&2
    return 1
  fi
}

build_variant() {
  local name="$1" file family log ts
  file="$(variant_file "$name")"
  family="$(variant_family "$name")"
  ts="$(date +%Y%m%d-%H%M%S)"
  log="logs/variant-${name}-${ts}.log"
  check_variant_base "$name" || return 1
  echo "============================================================"
  echo "Building variant ${name} (vsphere-clone.variant-${family})  ->  ${log}"
  echo "============================================================"
  packer validate -var-file="$file" .
  if packer build -force -var-file="$file" -only="variants.vsphere-clone.variant-${family}" . 2>&1 | tee "$log"; then
    echo "OK: variant ${name}"
    write_manifest "$name"
    register_variant "$name" && register_back "$name" build-only
  else
    echo "FAIL: variant ${name} (see ${log})" >&2
    return 1
  fi
}

require_variants() {
  local n
  for n in "$@"; do
    if [ ! -f "$(variant_file "$n")" ]; then
      echo "No variant file $(variant_file "$n"). Known variants:" >&2
      list_variants | sed 's/^/  /' >&2
      exit 2
    fi
  done
}

main() {
  if [ "$#" -eq 0 ]; then
    print_list
    echo
    echo "Pass 'all', 'variants', 'variant <name>...', 'register <name>...', 'replicate <id> [host...]'," \
      "'manifest <id>...', or template names." >&2
    exit 2
  fi

  local failed=0 n VARIANTS=()
  case "$1" in
    list) print_list; exit 0 ;;
    manifest)
      shift
      [ "$#" -gt 0 ] || { echo "manifest: name at least one template" >&2; exit 2; }
      command -v jq >/dev/null 2>&1 || { echo "manifest: jq is required" >&2; exit 2; }
      # Record only: a build with no host copies is incomplete (not promotable); replicate
      # stamps the copies and registers the record then.
      for n in "$@"; do write_manifest "$n" || failed=1; done
      echo "manifest: now './build.sh replicate <id>' (a new REPLICA_DATE if today's copies exist) to stamp," \
        "record and register the copies"
      exit "$failed" ;;
    replicate)
      shift
      [ "$#" -gt 0 ] || { echo "replicate: name a template id, then optionally hosts" >&2; exit 2; }
      replicate "$@" || failed=1
      exit "$failed" ;;
    register)
      shift
      [ "$#" -gt 0 ] || { echo "register: name at least one variant" >&2; exit 2; }
      require_variants "$@"
      for n in "$@"; do register_variant "$n" required || failed=1; done
      exit "$failed" ;;
    variant|variants)
      if [ "$1" = "variants" ]; then
        mapfile -t VARIANTS < <(list_variants)
      else
        shift
        [ "$#" -gt 0 ] || { echo "variant: name at least one variant" >&2; exit 2; }
        VARIANTS=("$@")
      fi
      require_variants "${VARIANTS[@]}"
      echo "=== packer init"
      packer init .
      for n in "${VARIANTS[@]}"; do build_variant "$n" || failed=1; done
      exit "$failed" ;;
    all)  mapfile -t TARGETS < <(printf '%s\n' "${ORDER[@]}") ;;
    *)    mapfile -t TARGETS < <(in_order "$@") ;;
  esac

  if [ "${#TARGETS[@]}" -eq 0 ]; then
    echo "No known templates matched: $* (variants: ./build.sh variant <name>)" >&2
    exit 2
  fi

  echo "=== packer init"
  packer init .
  echo "=== packer validate"
  packer validate .

  for n in "${TARGETS[@]}"; do
    build_one "$n" || failed=1
  done
  exit "$failed"
}

main "$@"
