#!/usr/bin/env bash
# TrueNorth work site - copy the build ISOs from [esx01-local] ISO/ to [esx04-local] ISO/.
#
#   scripts/lab/iso-sync-esx04.sh                  # plan: what is missing or differs
#   scripts/lab/iso-sync-esx04.sh --apply          # copy (asks y/N first)
#   scripts/lab/iso-sync-esx04.sh --apply --yes    # copy without asking
#   scripts/lab/iso-sync-esx04.sh --verify         # sha256 of the copies with a pinned sum
#
# Options:
#   --list FILE      media list (default install/lab/work-site/isos.conf; PENDING lines skipped)
#   --src DS         source datastore (default esx01-local)
#   --dst DS         destination datastore (default esx04-local)
#   --folder DIR     folder on both datastores (default ISO)
#   --stream         copy through this machine (datastore.download | datastore.upload)
#                    instead of vCenter's server-side copy; use it if the server-side copy
#                    between the two hosts' local datastores is refused
#   --verify         for each ISO whose isos.conf line pins sha256=<hex>: stream the
#                    esx04 copy through sha256sum and record a match in --state (build-phase
#                    builds such an ISO only once it is recorded). Reads the whole file.
#   --state DIR      verified-checksum records (default /var/lib/truenorth/iso-verified)
#   --env-file FILE  vCenter credentials (default /srv/truenorth/config/.env.production)
#
# Why: the hosts have local VMFS only and builds run on esx04 (next to TN-MGMT01, ADR 0012
# decision 7), so the ISOs must be on esx04-local. The default copy is server-side
# (govc datastore.cp: vCenter FileManager CopyDatastoreFile_Task); nothing passes through
# the machine running this unless --stream.
#
# Idempotent: a file that already exists on the destination with the source's size is
# skipped; one with another size is replaced. A file missing on the source is a warning.
# Never leaves a partial ISO under the real name: each copy goes to <name>.tn-partial and
# is renamed over <name> only once its size matches the source, so an interrupted copy
# leaves the previous good ISO (or nothing) in place, never a truncated one.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
REPO="$(cd "$LAB_DIR/../.." && pwd)"
LIST="$REPO/install/lab/work-site/isos.conf"
SRC_DS="esx01-local"; DST_DS="esx04-local"; FOLDER="ISO"; STREAM=0; VERIFY=0
STATE_DIR="${TN_ISO_STATE:-/var/lib/truenorth/iso-verified}"
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
    --verify) VERIFY=1 ;;
    --state) STATE_DIR="$2"; shift ;;
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

# sha256 pinned on an isos.conf line ("<var> <path> sha256=<hex>"), or empty.
pinned_sum() {
  awk -v r="$1" '{sub(/#.*/, "")} $2 == r {for (i = 3; i <= NF; i++) if ($i ~ /^sha256=/) {sub(/^sha256=/, "", $i); print tolower($i)}}' "$LIST" | head -n1
}

# --verify: hash the destination copy; record "<sha256> <size>" when it matches the pin.
verify_one() {
  local path="$1" rel="${1#"$FOLDER"/}" want size got
  want="$(pinned_sum "$rel")"; [[ -n "$want" ]] || return 0
  size="$(size_of "$DST_DS" "$path")"
  [[ -n "$size" ]] || { warn "verify: $path is not on $DST_DS"; return 1; }
  echo "  + govc datastore.download -ds $DST_DS $path - | sha256sum"
  got="$(govc datastore.download -ds "$DST_DS" "$path" - | sha256sum | awk '{print $1}')"
  if [[ "$got" == "$want" ]]; then
    mkdir -p "$(dirname "$STATE_DIR/$rel")"
    printf '%s %s\n' "$got" "$size" > "$STATE_DIR/$rel.sha256"
    info "verified $path (sha256 matches isos.conf)"
  else
    rm -f "$STATE_DIR/$rel.sha256"
    warn "CHECKSUM MISMATCH $path: $got, isos.conf pins $want (not buildable)"
    return 1
  fi
}

verify_all() {
  local rc=0 line rel
  [[ "$VERIFY" == "1" ]] || return 0
  step "Verify (sha256)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    read -r _var rel _rest <<<"$line" || true
    [[ -n "${rel:-}" && "$rel" != "PENDING" ]] || continue
    verify_one "$FOLDER/$rel" || rc=1
  done < "$LIST"
  return "$rc"
}

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

if [[ "${#plan[@]}" -eq 0 ]]; then
  echo "Nothing to copy."
  verify_all
  exit $?
fi
if [[ "$APPLY" != "1" ]]; then echo; echo "Plan only. Re-run with --apply to copy."; exit 0; fi
confirm "Copy ${#plan[@]} ISO(s) to [$DST_DS] $FOLDER/?" || die "not confirmed; nothing copied"

step "Copy"
failed=0
for item in "${plan[@]}"; do
  IFS='|' read -r path want _how <<<"$item"
  act govc datastore.mkdir -p -ds "$DST_DS" "$(dirname "$path")"
  # Copy to a temporary name, check it, then rename over the real one: an interrupted or
  # short copy never replaces a good ISO. -f: a leftover .tn-partial from an earlier attempt.
  part="$path.tn-partial"
  force=(-f)
  if [[ "$STREAM" == "1" ]]; then
    echo "  + govc datastore.download -ds $SRC_DS $path - | govc datastore.upload -ds $DST_DS - $part"
    govc datastore.download -ds "$SRC_DS" "$path" - | govc datastore.upload -ds "$DST_DS" - "$part" || true
  else
    act govc datastore.cp ${force[@]+"${force[@]}"} -ds "$SRC_DS" -ds-target "$DST_DS" "$path" "$part" || true
  fi
  got="$(size_of "$DST_DS" "$part")"
  if [[ "$got" == "$want" ]] && act govc datastore.mv -f -ds "$DST_DS" "$part" "$path" \
      && [[ "$(size_of "$DST_DS" "$path")" == "$want" ]]; then
    info "copied  $path ($got bytes)"
    rm -f "$STATE_DIR/${path#"$FOLDER"/}.sha256" 2>/dev/null || true   # a new copy is unverified
  else
    warn "FAILED  $path (copy has ${got:-nothing} bytes, want $want; $DST_DS keeps its previous file)"
    act govc datastore.rm -f -ds "$DST_DS" "$part" >/dev/null 2>&1 || true
    failed=1
  fi
done
verify_all || failed=1
exit "$failed"
