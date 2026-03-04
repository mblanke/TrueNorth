$ErrorActionPreference = "Stop"
Write-Host "== TrueNorth Range DoD Gate =="
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
function Has-Command($name) { return $null -ne (Get-Command $name -ErrorAction SilentlyContinue) }

# Python checks
$hasPy = (Test-Path ".\control-plane\api\requirements.txt")
if ($hasPy) {
  if (Has-Command "ruff") {
    Write-Host "+ ruff check control-plane/ scenario-engine/ ai-orchestrator/ telemetry/ tools/ tests/"
    ruff check control-plane/ scenario-engine/ ai-orchestrator/ telemetry/ tools/ tests/ --exclude="control-plane/web"
    Write-Host "+ ruff format --check"
    ruff format --check control-plane/ scenario-engine/ ai-orchestrator/ telemetry/ tools/ tests/ --exclude="control-plane/web"
  }
  if (Has-Command "pytest") {
    Write-Host "+ pytest -q"
    pytest -q
  }
}

# Angular checks
$hasNg = (Test-Path ".\control-plane\web\angular.json")
if ($hasNg) {
  Push-Location ".\control-plane\web"
  Write-Host "+ ng lint"
  npx ng lint
  Write-Host "+ ng build"
  npx ng build --configuration=production
  Write-Host "+ ng test --watch=false"
  npx ng test --watch=false --browsers=ChromeHeadless
  Pop-Location
}

# Schema validation
if (Has-Command "python") {
  Write-Host "+ validating content YAML against schemas"
  python -c "
import json, yaml, pathlib
from jsonschema import validate, ValidationError
for schema_name, content_dir in [('template', 'content/ranges'), ('scenario', 'content/scenarios')]:
    schema = json.loads(pathlib.Path(f'scenario-engine/schemas/{schema_name}.schema.json').read_text())
    for f in pathlib.Path(content_dir).rglob('*.yaml'):
        data = yaml.safe_load(f.read_text())
        try:
            validate(data, schema)
            print(f'  OK: {f}')
        except ValidationError as e:
            print(f'  FAIL: {f}: {e.message}')
            exit(1)
"
}

Write-Host "DoD PASS"
