# HORUS one-line installer for Windows (PowerShell 5.1+ / pwsh; zero Python/uv/git prerequisites).
# Usage: irm https://seros-cimenstein.github.io/horus-drone/install.ps1 | iex

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$HorusRef = if ($env:HORUS_REF) { $env:HORUS_REF } else { "main" }
$HorusPkg = if ($env:HORUS_PKG) { $env:HORUS_PKG } else { "https://github.com/seros-cimenstein/horus-drone/archive/refs/heads/$HorusRef.zip" }
$HorusPython = if ($env:HORUS_PYTHON) { $env:HORUS_PYTHON } else { "3.12" }

Write-Host "==> Installing HORUS ($HorusPkg, Python $HorusPython)"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "==> Bootstrapping uv (and managed Python $HorusPython)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    foreach ($candidate in @(
        (Join-Path $env:USERPROFILE ".local\bin"),
        (Join-Path $env:USERPROFILE ".cargo\bin")
    )) {
        if (Test-Path $candidate) {
            $env:PATH = "$candidate;$env:PATH"
        }
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv was not found on PATH after installation."
}

Write-Host "==> Installing $HorusPkg via uv tool install..."
& uv tool install --force --upgrade --reinstall-package horus-drone --python $HorusPython $HorusPkg
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed with exit code $LASTEXITCODE" }

foreach ($candidate in @(
    (Join-Path $env:USERPROFILE ".local\bin"),
    (Join-Path $env:USERPROFILE ".cargo\bin")
)) {
    if (Test-Path $candidate) {
        $env:PATH = "$candidate;$env:PATH"
    }
}

try { & uv tool update-shell 2>$null | Out-Null } catch {}

if ($env:HORUS_SKIP_SETUP -ne "1") {
    Write-Host "==> Fetching ArduCopter SITL and vision models (horus setup)..."
    & horus setup
    if ($LASTEXITCODE -ne 0) { throw "horus setup failed with exit code $LASTEXITCODE" }
} else {
    Write-Host "==> Skipping asset download (HORUS_SKIP_SETUP=1)."
}

& horus --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "horus --help failed with exit code $LASTEXITCODE" }

if (($env:HORUS_NO_START -ne "1") -and (-not $env:CI)) {
    Write-Host ""
    Write-Host "==> Starting HORUS Studio..."
    & horus
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "HORUS installed successfully! Run 'horus' to launch HORUS Studio."
