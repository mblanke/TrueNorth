#!/usr/bin/env bash
# Scaffold a new scenario dir from templates for a crosswalk PO. Files only.
set -euo pipefail
PO="${1:?usage: new_scenario.sh PO_007}"
D="scenarios/$PO"; mkdir -p "$D/validators" "$D/variant_B"
cp templates/range.tf.tmpl "$D/range.tf"
cp templates/timeline.yaml.tmpl "$D/timeline.yaml"
cp templates/rubric.md.tmpl "$D/rubric.md"
cp templates/xapi.json.tmpl "$D/xapi.json"
echo "scaffolded $D — Taz now fills from crosswalk row $PO (see CLAUDE.md)."
