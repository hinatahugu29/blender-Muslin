# Blender 4.2 以降の Extensions Platform 向けパッケージを作る。
#
# 従来形式(package_zip.ps1)との違い:
#   - cloth_core.pyd を直接入れず、maturin が作った wheel を wheels/ に入れる
#   - blender_manifest.toml を同梱する
#   - Blender 自身の `--command extension build` で検証してから固める
#
# 使い方: powershell -ExecutionPolicy Bypass -File scripts\package_extension.ps1

param(
    [string]$Blender = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$AddonDir = Join-Path $RepoRoot "addon\muslin"
$WheelDir = Join-Path $AddonDir "wheels"
$DistDir = Join-Path $RepoRoot "dist"

# Blender の実体を探す(新しい版から順に)
if (-not $Blender) {
    $Candidates = Get-ChildItem "C:\Program Files\Blender Foundation" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName "blender.exe" } |
        Where-Object { Test-Path $_ }
    $Blender = $Candidates | Select-Object -First 1
}
if (-not $Blender) {
    throw "blender.exe が見つかりません。-Blender で指定してください。"
}
Write-Host "==> Blender: $Blender"

# wheel を用意する
$Wheel = Get-ChildItem (Join-Path $RepoRoot "rust\cloth_core\target\wheels") -Filter "*.whl" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $Wheel) {
    throw "wheel がありません。先に scripts\build.ps1 を実行してください。"
}

if (Test-Path $WheelDir) { Remove-Item $WheelDir -Recurse -Force }
New-Item -ItemType Directory -Path $WheelDir | Out-Null
Copy-Item $Wheel.FullName -Destination $WheelDir
Write-Host "==> wheel: $($Wheel.Name)"

# マニフェストの wheel 名を実物に合わせる
$ManifestPath = Join-Path $AddonDir "blender_manifest.toml"
$Manifest = Get-Content $ManifestPath -Raw -Encoding UTF8
$Manifest = $Manifest -replace 'wheels = \["\./wheels/[^"]+"\]', "wheels = [`"./wheels/$($Wheel.Name)`"]"
# PowerShell 5.1 の -Encoding UTF8 は BOM を付ける。TOML パーサが弾くので BOM 無しで書く
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($ManifestPath, $Manifest, $Utf8NoBom)

# .pyd は Extensions 形式では wheel 側に入るので、パッケージには含めない
$Pyd = Join-Path $AddonDir "cloth_core.pyd"
$PydBackup = $null
if (Test-Path $Pyd) {
    $PydBackup = Join-Path $env:TEMP "cloth_core_backup.pyd"
    Move-Item $Pyd $PydBackup -Force
}

try {
    if (-not (Test-Path $DistDir)) { New-Item -ItemType Directory -Path $DistDir | Out-Null }

    Write-Host "==> 検証"
    & $Blender --factory-startup --command extension validate $AddonDir
    if ($LASTEXITCODE -ne 0) { throw "マニフェストの検証に失敗しました" }

    Write-Host "==> ビルド"
    & $Blender --factory-startup --command extension build --source-dir $AddonDir --output-dir $DistDir
    if ($LASTEXITCODE -ne 0) { throw "パッケージのビルドに失敗しました" }
}
finally {
    if ($PydBackup -and (Test-Path $PydBackup)) {
        Move-Item $PydBackup $Pyd -Force
    }
}

# --- 出来上がったパッケージを一時領域に入れて動作を確かめる ---
# ユーザーの Blender 設定を汚さないよう BLENDER_USER_EXTENSIONS を差し替える
$Built = Get-ChildItem $DistDir -Filter "muslin-*.zip" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
$TestDir = Join-Path $env:TEMP "muslin_ext_verify"
if (Test-Path $TestDir) { Remove-Item $TestDir -Recurse -Force }
New-Item -ItemType Directory -Path $TestDir | Out-Null

Write-Host "==> 一時領域に入れて動作確認"
$env:BLENDER_USER_EXTENSIONS = $TestDir
try {
    & $Blender --factory-startup --command extension install-file --repo user_default --enable $Built.FullName
    if ($LASTEXITCODE -ne 0) { throw "インストールに失敗しました" }

    $Probe = @'
import sys
import bpy

mod = "bl_ext.user_default.muslin"
bpy.ops.preferences.addon_enable(module=mod)
core = sys.modules[mod].cloth_core
print("VERIFY core_version", core.core_version())

bpy.ops.mesh.primitive_grid_add(x_subdivisions=20, y_subdivisions=20, size=1.0)
obj = bpy.context.active_object
obj.location = (0.0, 0.0, 1.0)
obj.muslin.collision_enabled = False   # 設定は布ごとに持つ
before = obj.data.vertices[0].co.z
assert bpy.ops.muslin.start_sim() == {"FINISHED"}, "開始できない"
for f in range(1, 13):
    bpy.context.scene.frame_set(f)
after = obj.data.vertices[0].co.z
assert before - after > 0.05, f"布が落下していない: {before} -> {after}"
assert hasattr(bpy.types, "MUSLIN_PT_main"), "パネルが登録されていない"
print("VERIFY ok")
'@
    $ProbePath = Join-Path $env:TEMP "muslin_ext_probe.py"
    [System.IO.File]::WriteAllText($ProbePath, $Probe, $Utf8NoBom)
    & $Blender --factory-startup --background --python-expr "import sys; exec(open(r'$ProbePath', encoding='utf-8').read())"
    if ($LASTEXITCODE -ne 0) { throw "拡張として動作しませんでした" }
}
finally {
    Remove-Item Env:\BLENDER_USER_EXTENSIONS -ErrorAction SilentlyContinue
    Remove-Item $TestDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "==> 完了。Blender の Preferences > Get Extensions > Install from Disk で入れられます"
Get-ChildItem $DistDir -Filter "*.zip" | ForEach-Object {
    Write-Host ("    {0}  ({1:N0} KB)" -f $_.Name, ($_.Length / 1KB))
}
