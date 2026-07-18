#requires -Version 5
<#
.SYNOPSIS
    Build RUMBLE into a per-user Windows installer.

.DESCRIPTION
    Two stages:
      1. PyInstaller bundles the app + Python into dist\RUMBLE\RUMBLE.exe
      2. Inno Setup (ISCC) wraps that into dist\RUMBLE-Setup.exe

    Run from anywhere; paths are resolved relative to this script.

.PARAMETER SkipInstaller
    Build only the PyInstaller exe, skip the Inno Setup step.

.EXAMPLE
    pwsh packaging\build.ps1
#>
param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot          # repo root
$spec = Join-Path $PSScriptRoot 'rumble.spec'
$iss  = Join-Path $PSScriptRoot 'installer.iss'

Write-Host '== Stage 1: PyInstaller ==' -ForegroundColor Cyan
Push-Location $root
try {
    pyinstaller --clean --noconfirm $spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
    Write-Host "  -> dist\RUMBLE\RUMBLE.exe" -ForegroundColor Green
}
finally { Pop-Location }

if ($SkipInstaller) {
    Write-Host 'Skipping installer (-SkipInstaller).' -ForegroundColor Yellow
    return
}

Write-Host '== Stage 2: Inno Setup ==' -ForegroundColor Cyan
# Locate the Inno Setup compiler (ISCC). It is a separate install:
# https://jrsoftware.org/isdl.php
$iscc = (Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue)?.Source
if (-not $iscc) {
    foreach ($p in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $p) { $iscc = $p; break }
    }
}
if (-not $iscc) {
    Write-Warning 'Inno Setup (ISCC.exe) not found. The exe is built in dist\RUMBLE\.'
    Write-Warning 'Install Inno Setup from https://jrsoftware.org/isdl.php then re-run, or run ISCC on packaging\installer.iss yourself.'
    return
}

& $iscc $iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
Write-Host "  -> dist\RUMBLE-Setup.exe" -ForegroundColor Green
Write-Host 'Done.' -ForegroundColor Green
