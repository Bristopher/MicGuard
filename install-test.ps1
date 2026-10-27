# TEST-BUILD path: builds MicGuard from the working tree, packs it with
# Velopack at the CURRENT VERSION (no bump), installs it over the Velopack copy
# at %LOCALAPPDATA%\com.cbuzi.micguard (the one the Run key autostarts) and
# relaunches. No version bump, no tag, no GitHub: releasing stays release.ps1's
# job (see RELEASING.md).
#
# Usage:  .\install-test.ps1            # build + pack + install + relaunch + smoke
#         .\install-test.ps1 -SkipBuild # reinstall the last Releases\local-test Setup
param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
. "$PSScriptRoot\build-common.ps1"

$version = Get-MicGuardVersion
# build\ is wiped by every onedir build, so the pack lives in Releases\ (git-ignored)
$packDir = Join-Path $PSScriptRoot "Releases\local-test"
$setup = Join-Path $packDir "MicGuard_${version}_x64-setup.exe"

if (-not $SkipBuild) {
    Build-MicGuardOnedir
    $setup = New-MicGuardPackage -Version $version -OutDir $packDir
}
Install-MicGuardLocal -SetupPath $setup
Start-Sleep -Seconds 4

Write-Host "`nLog tail:" -ForegroundColor Cyan
Get-Content "$env:APPDATA\MicGuard\micguard.log" -Tail 3
Write-Host "`nSabotage smoke (should print 'restored to <your %>'):" -ForegroundColor Cyan
uv run python -c "import time, micguard as m; did,_=m.autodetect_device(); v=m.get_endpoint_volume(did); v.SetMasterVolumeLevelScalar(0.47,None); time.sleep(1); print('restored to', round(v.GetMasterVolumeLevelScalar()*100))"
