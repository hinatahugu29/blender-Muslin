# cloth_core (Rust) をビルドし、コンパイル済みモジュールをアドオンフォルダへ配置する。
# 使い方: powershell -ExecutionPolicy Bypass -File scripts\build.ps1
#         -SkipTests を付けると Rust 単体テストをスキップする

param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$CrateDir = Join-Path $RepoRoot "rust\cloth_core"
$AddonDir = Join-Path $RepoRoot "addon\cloth_md"
$WheelDir = Join-Path $CrateDir "target\wheels"

# --- Python インタプリタの解決 -------------------------------------------------
# PATH 先頭にある WindowsApps のスタブ python.exe は pyo3 から見つけられないため、
# 実体のあるインタプリタを明示的に指定する。
if (-not $env:PYO3_PYTHON) {
    $Candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    )
    $Found = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Found) {
        throw "Python 3.11+ が見つかりません。PYO3_PYTHON 環境変数で明示してください。"
    }
    $env:PYO3_PYTHON = $Found
}
Write-Host "==> PYO3_PYTHON = $env:PYO3_PYTHON"

Push-Location $CrateDir
try {
    if (-not $SkipTests) {
        # 物理コアは pyo3 非依存なので、Python 抜きでビルドしてテストできる
        Write-Host "==> Running core physics tests (cargo test --no-default-features)"
        cargo test --no-default-features --release
        if ($LASTEXITCODE -ne 0) { throw "cargo test failed" }
    }

    Write-Host "==> Building cloth_core with maturin (release, abi3-py311)"
    maturin build --release
    if ($LASTEXITCODE -ne 0) { throw "maturin build failed" }
} finally {
    Pop-Location
}

Write-Host "==> Locating built wheel"
$Wheel = Get-ChildItem -Path $WheelDir -Filter "cloth_core-*.whl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Wheel) { throw "No wheel found in $WheelDir" }
Write-Host "    $($Wheel.FullName)"

Write-Host "==> Extracting cloth_core.pyd"
$ExtractDir = Join-Path $env:TEMP "cloth_core_wheel_extract"
if (Test-Path $ExtractDir) { Remove-Item -Recurse -Force $ExtractDir }
New-Item -ItemType Directory -Path $ExtractDir | Out-Null

$WheelAsZip = Join-Path $env:TEMP "cloth_core_wheel.zip"
Copy-Item -Path $Wheel.FullName -Destination $WheelAsZip -Force
Expand-Archive -Path $WheelAsZip -DestinationPath $ExtractDir -Force
$Pyd = Get-ChildItem -Path $ExtractDir -Recurse -Filter "cloth_core.pyd" | Select-Object -First 1
if (-not $Pyd) { throw "cloth_core.pyd not found inside wheel" }

if (-not (Test-Path $AddonDir)) { New-Item -ItemType Directory -Path $AddonDir | Out-Null }
Copy-Item -Path $Pyd.FullName -Destination (Join-Path $AddonDir "cloth_core.pyd") -Force

Write-Host "==> Done. Installed to $AddonDir\cloth_core.pyd"
Write-Host "    次: scripts\verify_core.ps1 で Python から動作確認、scripts\package_zip.ps1 で配布 zip 作成"
