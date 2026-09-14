# Builds the Rust extension and packages the addon as a zip ready for
# Blender's "Install from Disk" (Preferences > Add-ons > Install...).
# Usage: powershell -ExecutionPolicy Bypass -File scripts\package_zip.ps1

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$AddonSrcDir = Join-Path $RepoRoot "addon\cloth_md"
$DistDir = Join-Path $RepoRoot "dist"
$ZipPath = Join-Path $DistDir "cloth_md.zip"

Write-Host "==> Building Rust extension first"
& (Join-Path $PSScriptRoot "build.ps1")

if (-not (Test-Path $DistDir)) { New-Item -ItemType Directory -Path $DistDir | Out-Null }
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }

# Stage into a temp dir so the zip's top-level entry is exactly "cloth_md/"
# (Blender's zip installer requires the module folder at the zip root).
$StageDir = Join-Path $env:TEMP "cloth_md_zip_stage"
if (Test-Path $StageDir) { Remove-Item -Recurse -Force $StageDir }
New-Item -ItemType Directory -Path $StageDir | Out-Null

Copy-Item -Path $AddonSrcDir -Destination (Join-Path $StageDir "cloth_md") -Recurse
Get-ChildItem -Path (Join-Path $StageDir "cloth_md") -Recurse -Directory -Filter "__pycache__" |
    ForEach-Object { Remove-Item -Recurse -Force $_.FullName }

Write-Host "==> Zipping to $ZipPath"
Compress-Archive -Path (Join-Path $StageDir "cloth_md") -DestinationPath $ZipPath -Force

Remove-Item -Recurse -Force $StageDir

Write-Host "==> Done. Install via Blender: Preferences > Add-ons > Install... > select $ZipPath"
