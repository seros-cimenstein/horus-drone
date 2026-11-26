# HORUS one-line installer for Windows (PowerShell 5.1+ / pwsh).
# Usage: irm https://seros-cimenstein.github.io/horus-drone/install.ps1 | iex

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$HorusPkg = if ($env:HORUS_PKG) { $env:HORUS_PKG } else { "git+https://github.com/seros-cimenstein/horus-drone.git" }
$HorusPython = if ($env:HORUS_PYTHON) { $env:HORUS_PYTHON } else { "3.12" }

Write-Host "==> Installing HORUS ($HorusPkg, Python $HorusPython)"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "==> Bootstrapping uv..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $uvLocal = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path $uvLocal) {
        $env:PATH = "$uvLocal;$env:PATH"
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv was not found on PATH after installation."
}

Write-Host "==> Installing $HorusPkg via uv tool install..."
& uv tool install --upgrade --python $HorusPython $HorusPkg
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed with exit code $LASTEXITCODE" }

$toolBin = Join-Path $env:USERPROFILE ".local\bin"
if (Test-Path $toolBin) {
    $env:PATH = "$toolBin;$env:PATH"
}

if ($env:HORUS_SKIP_SETUP -ne "1") {
    Write-Host "==> Fetching ArduCopter SITL and vision models (horus setup)..."
    & horus setup
    if ($LASTEXITCODE -ne 0) { throw "horus setup failed with exit code $LASTEXITCODE" }
} else {
    Write-Host "==> Skipping asset download (HORUS_SKIP_SETUP=1)."
}

& horus --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "horus --help failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "HORUS installed successfully! Run 'horus' to launch HORUS Studio."
