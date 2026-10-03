#!/usr/bin/env bash
# fetch-sources.sh - download the authoritative xAPI / cmi5 / xAPI Profiles texts (Apache-2.0 repos)
# and assemble a flat corpus next to the reference, for reading or for local LLM ingestion.
#
# Where to run: any host with git and internet access (or a mirror). Re-run to update (fast-forward pulls).
# Usage: ./fetch-sources.sh [DEST_DIR]      (default ./sources)
#        IEEE=0 ./fetch-sources.sh          skip opensource.ieee.org (xAPI 2.0 and cmi5 IEEE working repos)
# Output: DEST/repos/<repo>  (shallow clones)
#         DEST/corpus/...    (selected spec files + LICENSE) and DEST/corpus/MANIFEST.tsv (file, repo, commit, fetched)
# Rollback: rm -rf DEST

set -uo pipefail
DEST="${1:-./sources}"; IEEE="${IEEE:-1}"
mkdir -p "$DEST/repos" "$DEST/corpus"
MAN="$DEST/corpus/MANIFEST.tsv"; printf 'file\trepo\tcommit\tfetched_utc\n' > "$MAN"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; FAILED=()

clone() {  # clone URL DIR [BRANCH]
  local url="$1" dir="$DEST/repos/$2" br="${3:-}"
  if [[ -d "$dir/.git" ]]; then timeout 300 git -C "$dir" pull -q --ff-only || { FAILED+=("$2 (pull)"); return 1; }
  else timeout 300 git clone -q --depth 1 ${br:+--branch "$br"} "$url" "$dir" || { FAILED+=("$2 ($url)"); return 1; }; fi
  echo "ok   $2 @ $(git -C "$dir" rev-parse --short HEAD)"
}
take() {  # take REPO_DIR SUBDIR FILE...   copies files into corpus/SUBDIR and records provenance
  local repo="$1" sub="$2"; shift 2
  mkdir -p "$DEST/corpus/$sub"
  local sha; sha="$(git -C "$DEST/repos/$repo" rev-parse HEAD 2>/dev/null || echo unknown)"
  local url; url="$(git -C "$DEST/repos/$repo" remote get-url origin 2>/dev/null || echo unknown)"
  for f in "$@"; do
    [[ -f "$DEST/repos/$repo/$f" ]] || { echo "warn $repo/$f not found"; continue; }
    cp "$DEST/repos/$repo/$f" "$DEST/corpus/$sub/"
    printf '%s\t%s\t%s\t%s\n' "$sub/$(basename "$f")" "$url" "$sha" "$NOW" >> "$MAN"
  done
}

clone https://github.com/adlnet/xAPI-Spec.git              xAPI-Spec        master  && \
  take xAPI-Spec xapi-1.0.3 xAPI.md xAPI-About.md xAPI-Data.md xAPI-Communication.md LICENSE
clone https://github.com/AICC/CMI-5_Spec_Current.git       cmi5-spec        quartz  && \
  take cmi5-spec cmi5-quartz cmi5_spec.md v1/CourseStructure.xsd v1/examples/simple-cmi5.xml \
       v1/examples/complex-cmi5.xml v1/examples/extended-cmi5.xml v1/examples/extended-cmi5.xsd LICENSE
clone https://github.com/adlnet/xapi-profiles.git          xapi-profiles    master  && \
  take xapi-profiles xapi-profiles-1.0 xapi-profiles-about.md xapi-profiles-structure.md xapi-profiles-communication.md LICENSE
clone https://github.com/adlnet/CATAPULT.git               CATAPULT         main    && \
  take CATAPULT cmi5-catapult requirements/requirements.json README.md LICENSE
clone https://github.com/adlnet/lrs-conformance-test-suite.git lrs-conformance-test-suite master && \
  take lrs-conformance-test-suite lrs-conformance README.md LICENSE

if [[ "$IEEE" == 1 ]]; then
  # File names inside these repos are not fixed here; every Markdown file is copied.
  for spec in "xapi/xapi-base-standard-documentation ieee-9274.1.1-docs xapi-2.0-ieee" \
              "xapi/xapi-base-standard-examples ieee-9274.1.1-examples xapi-2.0-ieee-examples" \
              "xapi-cmi5/9274.3.1 ieee-9274.3.1 cmi5-ieee-draft"; do
    set -- $spec
    if clone "https://opensource.ieee.org/$1.git" "$2"; then
      mapfile -t files < <(cd "$DEST/repos/$2" && find . -maxdepth 3 \( -name '*.md' -o -name 'LICENSE*' -o -name '*.json' \) -not -path './.git/*' | sed 's|^\./||')
      (( ${#files[@]} )) && take "$2" "$3" "${files[@]}"
    fi
  done
fi

echo; echo "corpus: $DEST/corpus ($(($(wc -l < "$MAN") - 1)) files)"
if (( ${#FAILED[@]} )); then printf 'not fetched: %s\n' "${FAILED[@]}"; exit 1; fi
