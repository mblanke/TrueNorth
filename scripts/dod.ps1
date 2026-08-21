# TrueNorth Range — Definition of Done gate (Windows).
#
# The PowerShell twin of scripts/dod.sh. dod.sh was rewritten on 2026-08-19 because
# `has ruff` / `has pytest` were both false — those tools live in the venv, not on
# PATH — so the whole Python block was skipped and the script printed PASS having
# run nothing. This file kept that exact pattern (`Has-Command "ruff"`) until now,
# which meant the Windows path still reported PASS on a box with no linter. Verified
# on 2026-08-20: `ruff --version` was not resolvable and the old gate was green.
#
# Rules, matching dod.sh: a missing tool is a FAILURE, never a silent skip.
$ErrorActionPreference = "Stop"
Write-Host "== TrueNorth Range DoD Gate =="
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Fail($msg) { Write-Host "DoD FAIL: $msg" -ForegroundColor Red; exit 1 }
function Run($label, [scriptblock]$block) {
  Write-Host "+ $label"
  & $block
  if ($LASTEXITCODE -ne 0) { Fail $label }
}

# Use the venv explicitly. PY can be overridden for CI images that install globally.
$PY = $env:PY
if (-not $PY) { $PY = Join-Path $root ".venv\Scripts\python.exe" }
if (-not (Test-Path $PY)) { Fail "no python at $PY (set `$env:PY to override)" }

& $PY -m ruff --version *> $null
if ($LASTEXITCODE -ne 0) { Fail "ruff not installed in $PY" }
& $PY -m pytest --version *> $null
if ($LASTEXITCODE -ne 0) { Fail "pytest not installed in $PY" }

$targets = @("control-plane/", "scenario-engine/", "ai-orchestrator/", "telemetry/", "tools/", "tests/")

# Ruff as a ratchet rather than a cliff — see dod.sh for the reasoning.
#   - breakage-class rules (undefined name, redefinition, syntax) ALWAYS fail;
#   - everything else may not get WORSE than the recorded baseline.
$baselineFile = Join-Path $root ".dod-ruff-baseline"

Write-Host "+ ruff check (breakage rules: F821,F811,E9)"
& $PY -m ruff check @targets --exclude="control-plane/web" --no-fix --select F821,F811,E9 --output-format=concise
if ($LASTEXITCODE -ne 0) {
  Fail "undefined names / redefinitions / syntax errors — these are real bugs, not style"
}

$findings = @(& $PY -m ruff check @targets --exclude="control-plane/web" --no-fix --output-format=concise 2>$null |
  Where-Object { $_ -match ':' })
$cur = $findings.Count
$base = $cur
if (Test-Path $baselineFile) { $base = [int](Get-Content $baselineFile -Raw).Trim() }
Write-Host "+ ruff debt: $cur (baseline $base)"
if ($cur -gt $base) {
  $findings | ForEach-Object { Write-Host "  $_" }
  Fail "ruff findings rose from $base to $cur — fix the new ones (or lower the baseline deliberately)"
}
# Ratchet down automatically when the count improves, so cleanup sticks.
if ($cur -lt $base) { Write-Host "  ratcheting baseline down: $base -> $cur" }
# Write only on change. .dod-ruff-baseline is tracked, so rewriting it every run
# churns the file (and, with LF written under core.autocrlf, makes git warn on the
# next command). LF rather than CRLF keeps it identical to what dod.sh produces.
if ($cur -ne $base) { [System.IO.File]::WriteAllText($baselineFile, "$cur`n") }

# Match .github/workflows/ci.yml:37 exactly, so local green and CI green mean the
# same thing. tests/integration needs OpenSearch and live provisioners and runs as a
# separate CI job (ci.yml:137).
Run "pytest" { & $PY -m pytest tests/ --tb=short -q --ignore=tests/integration }

# Angular is opt-in until the repo carries a karma.conf.js and CHROME_BIN is set.
# Leaving an unrunnable step in the default path is what got the gate abandoned.
if ($env:DOD_WEB -eq "1") {
  if (-not (Test-Path "control-plane\web\angular.json")) { Fail "DOD_WEB=1 but no control-plane/web/angular.json" }
  Push-Location "control-plane\web"
  try {
    Run "ng lint"  { & npx ng lint }
    Run "ng build" { & npx ng build --configuration=production }
    Run "ng test"  { & npx ng test --watch=false --browsers=ChromeHeadless }
  } finally { Pop-Location }
} else {
  Write-Host "  (web checks skipped; set `$env:DOD_WEB=1 to include them)"
}

# Record WHICH STATE passed, byte-identical in format to dod.sh so either platform's
# marker is readable by the other. HEAD + a hash of the full diff against HEAD covers
# staged and unstaged changes alike; `git write-tree` would hash only the index and
# was blind to precisely the unstaged damage this marker exists to catch.
#
# `git diff --output=` writes raw bytes, avoiding PowerShell's line-ending rewriting,
# so an empty diff hashes to e3b0c44298fc1c14 on Windows exactly as it does on Linux.
# Do NOT redirect git's stderr here. In PowerShell 5.1 redirecting a native
# command's stderr wraps every line in a NativeCommandError, which under
# $ErrorActionPreference = "Stop" is terminating — so git's harmless
# "LF will be replaced by CRLF" notice killed the gate after a clean run and it
# exited 1 without ever printing PASS. Relax the preference instead.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  $head = (& git rev-parse HEAD)
  if ($LASTEXITCODE -ne 0 -or -not $head) { $head = "nohead" }
  $tmp = [System.IO.Path]::GetTempFileName()
  try {
    & git diff HEAD --output="$tmp" | Out-Null
    $sha = (Get-FileHash -Path $tmp -Algorithm SHA256).Hash.ToLower().Substring(0, 16)
  } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
} finally { $ErrorActionPreference = $prevEAP }
$stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
[System.IO.File]::WriteAllText((Join-Path $root ".dod-pass"), "${head}:${sha}`n$stamp`n")

Write-Host "DoD PASS" -ForegroundColor Green
