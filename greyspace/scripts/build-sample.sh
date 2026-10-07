#!/usr/bin/env bash
# Build a Greyspace corpus sample by shallow-mirroring permissive-licence sites.
#
#   greyspace/scripts/build-sample.sh --tier mac [--dest DIR] [--seeds FILE] [--per-site-mb N] [--dry-run]
#   greyspace/scripts/build-sample.sh --tier t0 [--dest DIR]       (the generated CI fixture; no download)
#
# --tier mac (T1): mirrors each seed (greyspace/corpus/seeds-mac.txt) to depth 2 with its
# page requisites into DEST/sites/<fqdn>/, keeps a WARC per site in DEST/warc/ (provenance,
# and the input format the lab tiers use), then writes DEST/manifest.json and
# DEST/checksums.sha256 with corpus.py index. Default DEST is ~/greyspace-corpus.
#
# HARD CAP: 5 GB (5,000,000,000 bytes) for the whole of DEST. Before each site the
# remaining budget is measured and passed to wget as --quota; after each site the total is
# measured again, and a site that pushed DEST over the cap is deleted and the run stops.
# The cap is not configurable from the command line on purpose (ADR 0007).
#
# Polite by construction: robots.txt honoured, one request per second (randomised),
# 2 MB/s rate limit, a descriptive User-Agent. Re-running resumes: a site whose WARC
# already exists is skipped. Needs: wget (GNU, with WARC support), python3, du.
#
# Layout and manifest format are identical across tiers (docs/greyspace-corpus.md), so
# `corpus.py render --corpus DEST` and the web farm serve this sample unchanged.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

CAP_BYTES=5000000000
MIN_BUDGET=$((50 * 1000 * 1000))   # stop when less than 50 MB of the cap is left
TIER=""
DEST=""
SEEDS="$ROOT/greyspace/corpus/seeds-mac.txt"
PER_SITE_MB=600
DRY_RUN=0
PY="${PY:-python3}"
UA="TrueNorth-Greyspace-sample/1 (offline training range corpus; shallow mirror)"

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tier) TIER="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    --seeds) SEEDS="$2"; shift 2 ;;
    --per-site-mb) PER_SITE_MB="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "build-sample: unknown argument $1" >&2; usage 2 ;;
  esac
done

case "$TIER" in
  t0)
    exec "$PY" "$ROOT/greyspace/scripts/corpus.py" t0 --out "${DEST:-$ROOT/build/greyspace/corpus-t0}" ;;
  mac|t1) ;;
  *) echo "build-sample: --tier mac (T1) or --tier t0; T2 and full are planned in docs/greyspace-corpus.md" >&2; exit 2 ;;
esac

DEST="${DEST:-$HOME/greyspace-corpus}"
command -v wget >/dev/null || { echo "build-sample: GNU wget is required (brew install wget)" >&2; exit 2; }
wget --help 2>/dev/null | grep -q -- '--warc-file' || { echo "build-sample: this wget has no WARC support" >&2; exit 2; }

used_bytes() {
  # du -sk is portable across macOS and Linux; kilobytes -> bytes (over-estimates slightly: safe).
  if [[ -d "$DEST" ]]; then echo $(( $(du -sk "$DEST" | cut -f1) * 1024 )); else echo 0; fi
}

say() { echo "== build-sample: $*"; }

say "tier T1 (mac) -> $DEST, hard cap $CAP_BYTES bytes, seeds $SEEDS"
[[ "$DRY_RUN" == 1 ]] || mkdir -p "$DEST/sites" "$DEST/warc"

stopped=""
while read -r fqdn category licence url includes _rest; do
  [[ -z "${fqdn:-}" || "$fqdn" == \#* ]] && continue
  used=$(used_bytes)
  budget=$(( CAP_BYTES - used ))
  if (( budget < MIN_BUDGET )); then stopped="cap reached before $fqdn"; break; fi
  quota=$(( PER_SITE_MB * 1000 * 1000 ))
  (( quota > budget )) && quota=$budget
  if [[ -f "$DEST/warc/$fqdn.warc.gz" ]]; then say "$fqdn: already mirrored, skipping"; continue; fi
  args=(--recursive --level=2 --no-parent --page-requisites --adjust-extension --convert-links
        --span-hosts --domains="$fqdn" --wait=1 --random-wait --limit-rate=2m --tries=2 --timeout=30
        -e robots=on --user-agent="$UA" --quota="$quota" --no-verbose
        --reject-regex='(action=|Special:|oldid=|diff=|printable=|index\.php\?)'
        --directory-prefix="$DEST/sites" --warc-file="$DEST/warc/$fqdn" --warc-cdx)
  [[ -n "${includes:-}" ]] && args+=(--include-directories="$includes")
  say "$fqdn ($category, $licence): quota $quota bytes, $used of $CAP_BYTES used"
  if [[ "$DRY_RUN" == 1 ]]; then echo "wget ${args[*]} $url"; continue; fi
  wget "${args[@]}" "$url" || say "$fqdn: wget exited $? (partial mirrors are kept)"
  if (( $(used_bytes) > CAP_BYTES )); then
    say "$fqdn pushed the sample over the cap; removing it"
    rm -rf "${DEST:?}/sites/$fqdn" "$DEST/warc/$fqdn".*
    stopped="cap reached during $fqdn"
    break
  fi
done < "$SEEDS"

[[ -n "$stopped" ]] && say "stopped: $stopped"
if [[ "$DRY_RUN" == 1 ]]; then say "dry run: nothing downloaded"; exit 0; fi

"$PY" "$ROOT/greyspace/scripts/corpus.py" index --root "$DEST" --tier t1 \
  --version "$(date -u +%Y.%m.%d)-mac" --seeds "$SEEDS"
"$PY" "$ROOT/greyspace/scripts/corpus.py" verify --root "$DEST"
say "done: $(used_bytes) bytes in $DEST"
