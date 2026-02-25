# Build a Windows onedir bundle (.exe inside dist\chatgpt_mirror).
# Optionally produces a zip archive for portable distribution.

param(
    [switch]$ZipPortable = $true
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Spec = Join-Path $Root "packaging\chatgpt_mirror.spec"
$DistDir = Join-Path $Root "dist"
$AppDir = Join-Path $DistDir "chatgpt_mirror"

Set-Location $Root

Write-Host "[1/2] Building PyInstaller bundle..."
python -m PyInstaller --noconfirm --clean $Spec

if (-not (Test-Path $AppDir)) {
    throw "PyInstaller output not found: $AppDir"
}

if ($ZipPortable) {
    Write-Host "[2/2] Creating portable zip..."
    $ZipPath = Join-Path $DistDir "ChatGPTMirror-win64-portable.zip"
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
    Compress-Archive -Path (Join-Path $AppDir '*') -DestinationPath $ZipPath
    Write-Host "Portable package: $ZipPath"
} else {
    Write-Host "Bundle ready: $AppDir"
}

