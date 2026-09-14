# 従来形式(bl_info を見る方式)のアドオン zip を作る。
# Blender 4.2 より前向け。4.2 以降は scripts\package_extension.ps1 の
# Extensions 形式を推奨する。
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
$Staged = Join-Path $StageDir "cloth_md"
Get-ChildItem -Path $Staged -Recurse -Directory -Filter "__pycache__" |
    ForEach-Object { Remove-Item -Recurse -Force $_.FullName }

# Extensions 形式でしか使わないものは、従来形式の zip からは外す。
# 残すと .pyd と wheel が二重に入り、Blender 4.2 以降では拡張として
# 解釈されてしまって紛らわしい。
$ExtensionOnly = @("blender_manifest.toml", "wheels")
foreach ($name in $ExtensionOnly) {
    $path = Join-Path $Staged $name
    if (Test-Path $path) { Remove-Item -Recurse -Force $path }
}

Write-Host "==> Zipping to $ZipPath"
Compress-Archive -Path (Join-Path $StageDir "cloth_md") -DestinationPath $ZipPath -Force

Remove-Item -Recurse -Force $StageDir

Write-Host "==> Done. Install via Blender: Preferences > Add-ons > Install... > select $ZipPath"
