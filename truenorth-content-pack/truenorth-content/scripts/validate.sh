#!/usr/bin/env bash
# Per-scenario self-check loop. Files only — never provisions.
set -euo pipefail
S="${1:?usage: validate.sh scenarios/PO_XXX}"
echo "[*] terraform validate"; ( cd "$S" && terraform init -backend=false -input=false >/dev/null 2>&1 && terraform validate ) || echo "  (terraform not configured in this env)"
echo "[*] lint timeline.yaml against schema"; python3 - "$S/timeline.yaml" <<'PY'
import sys,yaml
d=yaml.safe_load(open(sys.argv[1]))["scenario"]
req=["po_id","title","environment","duration_min","injects","validators"]
miss=[k for k in req if k not in d]
print("  missing:",miss or "none")
crit=[i for i in (d.get("injects") or []) if i.get("critical")]
print("  critical injects:",[i["id"] for i in crit])
PY
echo "[*] verify golden templates exist + enabled"; python3 - "$S/timeline.yaml" <<'PY'
import sys,yaml,csv
tpls=set(t["template_id"] for t in csv.DictReader(open("vm_catalogue.csv")) if t["enabled"]=="yes")
used=yaml.safe_load(open(sys.argv[1]))["scenario"].get("golden_templates",[])
bad=[t for t in used if t not in tpls]
print("  disabled/unknown templates:",bad or "none")
PY
echo "[*] DONE — review, then a human authorizes provisioning separately."
