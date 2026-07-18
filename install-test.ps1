# Builds MicGuard from the working tree and installs it over the local copy
# at %LOCALAPPDATA%\Programs\MicGuard — the TEST-BUILD path. No version bump,
# no tag, no GitHub: releasing stays release.ps1's job (see RELEASING.md).
#
# Usage:  .\install-test.ps1            # build + install + relaunch + smoke
#         .\install-test.ps1 -SkipBuild # reinstall the existing dist\MicGuard.exe
param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not $SkipBuild) {
    uv run pyinstaller --onefile --noconsole --name MicGuard `
        --icon assets\icon.ico --collect-all webview micguard.py
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
}
if (-not (Test-Path dist\MicGuard.exe)) { throw 'No dist\MicGuard.exe - build first' }

$target = Join-Path $env:LOCALAPPDATA 'Programs\MicGuard\MicGuard.exe'
Stop-Process -Name MicGuard -Force -ErrorAction SilentlyContinue
# Wait for the old instance AND its WebView2 children to fully exit before
# relaunching. Relaunching while msedgewebview2 still holds the user-data
# folder makes the new instance's WebView2 init fail with 0x8007139F — the
# tray then runs with a dead GUI loop (every menu/settings click times out
# 20 s and fails). Bit us 2026-07-18.
for ($i = 0; $i -lt 20 -and (Get-Process MicGuard -ErrorAction SilentlyContinue); $i++) {
    Start-Sleep -Milliseconds 500
}
Start-Sleep -Seconds 3   # grace for WebView2 children to release the data dir
New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
Copy-Item dist\MicGuard.exe $target -Force
Start-Process $target
Start-Sleep -Seconds 4

Write-Host "`nLog tail:" -ForegroundColor Cyan
Get-Content "$env:APPDATA\MicGuard\micguard.log" -Tail 2
Write-Host "`nSabotage smoke (should print 'restored to <your %>'):" -ForegroundColor Cyan
uv run python -c "import time, micguard as m; did,_=m.autodetect_device(); v=m.get_endpoint_volume(did); v.SetMasterVolumeLevelScalar(0.47,None); time.sleep(1); print('restored to', round(v.GetMasterVolumeLevelScalar()*100))"
