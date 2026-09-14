# Runs the Blender-free verification harness against the built cloth_core.pyd.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\verify_core.ps1 [-Bench]

param(
    [switch]$Bench
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $RepoRoot "scripts\verify_core.py"

$Python = $env:PYO3_PYTHON
if (-not $Python) {
    $Candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    )
    $Python = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Python) { throw "Python 3.11+ not found. Set PYO3_PYTHON." }

Write-Host "==> Using $Python"
if ($Bench) {
    & $Python $Script --bench
} else {
    & $Python $Script
}
exit $LASTEXITCODE
