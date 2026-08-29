# Build the Saturday Windows installer end-to-end (NSIS 3, per-user, no admin).
# Usage:  powershell -File scripts\build_windows.ps1 [-Version 1.2.3]
# Output: installer\_output\Saturday-Setup-<version>.exe
param(
    [string]$Version = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:PYTHONIOENCODING = "utf-8"
python -m PyInstaller saturday.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# makensis: installed copy, or a portable zip under tools\ (no admin needed).
$makensis = @(
    "${env:ProgramFiles(x86)}\NSIS\makensis.exe",
    "${env:ProgramFiles}\NSIS\makensis.exe",
    (Join-Path $root "tools\nsis-3.11\makensis.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $makensis) {
    New-Item -ItemType Directory -Force -Path (Join-Path $root "tools") | Out-Null
    Write-Host "Downloading portable NSIS 3.11..."
    $zip = Join-Path $root "tools\nsis.zip"
    Invoke-WebRequest -Uri "https://downloads.sourceforge.net/project/nsis/NSIS%203/3.11/nsis-3.11.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath (Join-Path $root "tools") -Force
    Remove-Item $zip
    $makensis = (Join-Path $root "tools\nsis-3.11\makensis.exe")
}

$v = if ($Version) { $Version } else { python -c "from saturday import __version__; print(__version__)" }
& $makensis "-DVERSION=$v" "installer\saturday.nsi"
if ($LASTEXITCODE -ne 0) { throw "makensis failed" }

Get-ChildItem (Join-Path $root "installer\_output")
