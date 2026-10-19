#!/usr/bin/env bash
# TrueNorth work site - copy the build ISOs from [esx01-local] ISO/ to [esx04-local] ISO/.
#
#   scripts/lab/iso-sync-esx04.sh                  # plan: what is missing or differs
#   scripts/lab/iso-sync-esx04.sh --apply          # copy (asks y/N first)
#   scripts/lab/iso-sync-esx04.sh --apply --yes    # copy without asking
#
# Options:
#   --list FILE      media list (default install/lab/work-site/isos.conf; PENDING lines skipped)
#   --src DS         source datastore (default esx01-local)
#   --dst DS         destination datastore (default esx04-local)
#   --folder DIR     folder on both datastores (default ISO)
#   --stream         copy through this machine (datastore.download | datastore.upload)
#                    instead of vCenter's server-side copy; use it if the server-side copy
#                    between the two hosts' local datastores is refused
#   --env-file FILE  vCenter credentials (default /srv/truenorth/config/.env.production)
#
# Why: the hosts have local VMFS only and builds run on esx04 (next to TN-MGMT01, ADR 0012
# decision 7), so the ISOs must be on esx04-local. The default copy is server-side
# (govc datastore.cp: vCenter FileManager CopyDatastoreFile_Task); nothing passes through
# the machine running this unless --stream.
#
# Idempotent: a file that already exists on the destination with the source's size is
# skipped; one with another size is replaced. A file missing on the source is a warning.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
REPO="$(cd "$LAB_DIR/../.." && pwd)"
LIST="$REPO/install/lab/work-site/isos.conf"
SRC_DS="esx01-local"; DST_DS="esx04-local"; FOLDER="ISO"; STREAM=0
ENV_FILE="${TN_ENV_FILE:-/srv/truenorth/config/.env.production}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --yes) ASSUME_YES=1 ;;
    --list) LIST="$2"; shift ;;
    --src) SRC_DS="$2"; shift ;;
    --dst) DST_DS="$2"; shift ;;
    --folder) FOLDER="$2"; shift ;;
    --stream) STREAM=1 ;;
    --env-file) ENV_FILE="$2"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done
[[ -r "$LIST" ]] || die "cannot read $LIST"
LIST_NAME="${LIST##*/}"
if [[ -z "${GOVC_USERNAME:-}" || -z "${GOVC_PASSWORD:-}" ]]; then govc_from_env_file "$ENV_FILE"; fi

# Size in bytes of [ds] path, or empty when it does not exist.
size_of() {
  govc datastore.ls -json -l -ds "$1" "$2" 2>/dev/null \
    | jq -r "$JQ_CI"' [ci("fileSize")[] | select(type == "number")][0] // empty' 2>/dev/null || true
}

step "Preflight"
lab_preflight
for ds in "$SRC_DS" "$DST_DS"; do
  [[ -n "$(govc find / -type s -name "$ds")" ]] || die "datastore $ds not found"
done
info "copy [$SRC_DS] $FOLDER/ -> [$DST_DS] $FOLDER/ ($([[ "$STREAM" == 1 ]] && echo "streamed through this host" || echo "server-side"))"

step "Plan"
plan=(); missing=0; same=0
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  read -r _var rel _rest <<<"$line" || true
  [[ -n "${rel:-}" && "$rel" != "PENDING" ]] || continue
  path="$FOLDER/$rel"
  src_size="$(size_of "$SRC_DS" "$path")"
  if [[ -z "$src_size" ]]; then
    warn "missing on $SRC_DS: $path (download it, or mark it PENDING in $LIST_NAME)"
    missing=$((missing + 1)); continue
  fi
  dst_size="$(size_of "$DST_DS" "$path")"
  if [[ "$dst_size" == "$src_size" ]]; then
    info "ok      $path ($src_size bytes, already on $DST_DS)"
    same=$((same + 1))
  elif [[ -n "$dst_size" ]]; then
    info "REPLACE $path ($DST_DS has $dst_size bytes, $SRC_DS $src_size)"
    plan+=("$path|$src_size|replace")
  else
    info "COPY    $path ($src_size bytes)"
    plan+=("$path|$src_size|copy")
  fi
done < "$LIST"
info "${#plan[@]} to copy, $same already there, $missing missing on $SRC_DS"

if [[ "${#plan[@]}" -eq 0 ]]; then echo "Nothing to copy."; exit 0; fi
if [[ "$APPLY" != "1" ]]; then echo; echo "Plan only. Re-run with --apply to copy."; exit 0; fi
confirm "Copy ${#plan[@]} ISO(s) to [$DST_DS] $FOLDER/?" || die "not confirmed; nothing copied"

step "Copy"
failed=0
for item in "${plan[@]}"; do
  IFS='|' read -r path want how <<<"$item"
  act govc datastore.mkdir -p -ds "$DST_DS" "$(dirname "$path")"
  force=(); [[ "$how" == "replace" ]] && force=(-f)
  if [[ "$STREAM" == "1" ]]; then
    echo "  + govc datastore.download -ds $SRC_DS $path - | govc datastore.upload -ds $DST_DS - $path"
    govc datastore.download -ds "$SRC_DS" "$path" - | govc datastore.upload -ds "$DST_DS" - "$path" || true
  else
    act govc datastore.cp ${force[@]+"${force[@]}"} -ds "$SRC_DS" -ds-target "$DST_DS" "$path" "$path" || true
  fi
  got="$(size_of "$DST_DS" "$path")"
  if [[ "$got" == "$want" ]]; then info "copied  $path ($got bytes)"
  else warn "FAILED  $path (destination has ${got:-nothing}, want $want)"; failed=1; fi
done
exit "$failed"
