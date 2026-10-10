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
#
# For each template: packer build -force -only='*.<builder>.<name>' . with a per-template log
# under logs/. A variant adds -var-file=variants/<name>.pkrvars.hcl and builds the
# variant-windows or variant-linux source (see variants.pkr.hcl).
#
# Register-back, when TN_API_URL and TN_API_TOKEN are set:
#   base/derived  PATCH the catalogue image to build_status=built, template_name=<name>
#   variant       POST /golden-images (create or update) from the variant file's metadata,
#                 so it is offered in the Range Designer
#
# packer init + packer validate run once up front.
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs

# Build-sheet order. ISO builds first, then the vsphere-clone derived images.
ORDER=(
  srv2022 win10-22h2 ubuntu-lts pfsense securityonion kali win11-24h2
  srv2019 srv2016 rocky vyos srv2025 debian13 parrot win10-ltsc
  remnux sift svc-emulators greyspace-host ca-host usersim cloudlog-emu c2-server
  precomp-host detonation-host
)

VARIANT_DIR="variants"

# Names built by the vsphere-clone builder (everything else is vsphere-iso).
is_clone() {
  case "$1" in
    remnux|sift|svc-emulators|greyspace-host|ca-host|usersim|cloudlog-emu|c2-server|precomp-host|detonation-host) return 0 ;;
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
  for n in "${ORDER[@]}"; do printf '  %-16s %s\n' "$n" "$(builder_of "$n")"; done
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

# --- register-back: look up the golden image by catalogue_id and PATCH it -------------
register_back() {
  local name="$1"
  if [ -z "${TN_API_URL:-}" ] || [ -z "${TN_API_TOKEN:-}" ]; then
    return 0
  fi
  if ! command -v jq >/dev/null 2>&1; then
    echo "WARN: jq not found; skipping register-back for $name" >&2
    return 0
  fi
  local base id
  base="${TN_API_URL%/}"
  echo "register-back: resolving golden image for catalogue_id=$name"
  id="$(curl -fsS -H "Authorization: Bearer ${TN_API_TOKEN}" \
          "${base}/golden-images?hypervisor=vsphere" \
        | jq -r --arg c "$name" '.[] | select(.catalogue_id == $c) | .id' | head -n1)"
  if [ -z "$id" ] || [ "$id" = "null" ]; then
    echo "WARN: no golden image with catalogue_id=$name on vsphere; skipping register-back" >&2
    return 0
  fi
  curl -fsS -X PATCH \
    -H "Authorization: Bearer ${TN_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg t "$name" '{build_status:"built", template_name:$t}')" \
    "${base}/golden-images/${id}" >/dev/null
  echo "register-back: ${name} -> built (id=$id)"
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

build_one() {
  local name="$1" builder log ts
  builder="$(builder_of "$name")"
  ts="$(date +%Y%m%d-%H%M%S)"
  log="logs/${name}-${ts}.log"
  echo "============================================================"
  echo "Building ${name} (${builder})  ->  ${log}"
  echo "============================================================"
  if packer build -force -only="*.${builder}.${name}" . 2>&1 | tee "$log"; then
    echo "OK: ${name}"
    register_back "$name"
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
    register_variant "$name"
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
    echo "Pass 'all', 'variants', 'variant <name>...', 'register <name>...', or template names." >&2
    exit 2
  fi

  local failed=0 n VARIANTS=()
  case "$1" in
    list) print_list; exit 0 ;;
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
