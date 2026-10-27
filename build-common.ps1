# Shared build/pack/install/publish steps for release.ps1, install-test.ps1 and
# build-release-FULL_Build.ps1. Dot-source it:  . "$PSScriptRoot\build-common.ps1"
# Every function throws on failure (callers run with $ErrorActionPreference = 'Stop').

$script:PackId     = 'com.cbuzi.micguard'   # IMMUTABLE identity - see AI-Development-Guide
$script:GithubRepo = 'Bristopher/MicGuard'
$script:VelopackDir = Join-Path $env:LOCALAPPDATA $script:PackId
$script:StubExe     = Join-Path $script:VelopackDir 'MicGuard.exe'   # what the Run key starts

function Get-MicGuardVersion {
    $src = Get-Content (Join-Path $PSScriptRoot 'micguard.py') -Raw
    if ($src -notmatch 'VERSION = "(\d+\.\d+\.\d+)"') { throw 'VERSION = "x.y.z" not found in micguard.py' }
    $Matches[1]
}

function Assert-Vpk {
    if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
        throw "dotnet SDK not found - install .NET (required for the vpk CLI) first."
    }
    if (-not (Get-Command vpk -ErrorAction SilentlyContinue)) {
        Write-Host "vpk CLI not found - installing via 'dotnet tool install -g vpk'..." -ForegroundColor Yellow
        dotnet tool install -g vpk
        if ($LASTEXITCODE -ne 0) { throw "dotnet tool install -g vpk failed" }
        $tools = Join-Path $env:USERPROFILE ".dotnet\tools"
        if ($env:Path -notlike "*$tools*") { $env:Path = "$tools;$env:Path" }
        if (-not (Get-Command vpk -ErrorAction SilentlyContinue)) {
            throw "vpk still not found on PATH after install - open a new shell and re-run."
        }
    }
}

function Build-MicGuardOnedir {
    # Wipe dist/build first: a stale dist\MicGuard\MicGuard.exe would pass the
    # check below even when the rebuild failed (bit us on the first v2.0.0 pack).
    Push-Location $PSScriptRoot
    try {
        if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }
        if (Test-Path build) { Remove-Item build -Recurse -Force }
        uv run pyinstaller --noconfirm --onedir --noconsole --name MicGuard `
            --icon assets\icon.ico --collect-all webview micguard.py
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed (exit $LASTEXITCODE)" }
        if (-not (Test-Path dist\MicGuard\MicGuard.exe)) { throw 'Build failed - no dist\MicGuard\MicGuard.exe' }
    } finally { Pop-Location }
}

function New-MicGuardPackage {
    # vpk pack dist\MicGuard into $OutDir; -WithDelta first downloads the last
    # GitHub release so vpk can emit a small delta package for updaters.
    param([Parameter(Mandatory)][string]$Version,
          [Parameter(Mandatory)][string]$OutDir,
          [switch]$WithDelta)
    Assert-Vpk
    Push-Location $PSScriptRoot
    try {
        if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
        New-Item -ItemType Directory -Force $OutDir | Out-Null
        if ($WithDelta) {
            gh release view --repo $script:GithubRepo *> $null
            if ($LASTEXITCODE -eq 0) {
                vpk download github --repoUrl "https://github.com/$script:GithubRepo" --outputDir $OutDir | Out-Host
                if ($LASTEXITCODE -ne 0) { Write-Host "  (delta download failed - full package only)" -ForegroundColor DarkGray }
            }
        }
        vpk pack --packId $script:PackId --packTitle "MicGuard" --packAuthors "Bristopher" `
            --packVersion $Version --packDir dist\MicGuard --mainExe MicGuard.exe `
            --icon assets\icon.ico --outputDir $OutDir | Out-Host   # keep stdout out of the return value
        if ($LASTEXITCODE -ne 0) { throw "vpk pack failed" }
        # human-facing exes get the Gkey-convention names; feed files keep
        # vpk's names (the in-app updater fetches those by fixed name)
        $setup = Join-Path $OutDir "$script:PackId-win-Setup.exe"
        if (Test-Path $setup) { Rename-Item $setup (Join-Path $OutDir "MicGuard_${Version}_x64-setup.exe") -Force }
        $portable = Join-Path $OutDir "$script:PackId-win-Portable.zip"
        if (Test-Path $portable) { Rename-Item $portable (Join-Path $OutDir "MicGuard_${Version}_x64-Portable.zip") -Force }
    } finally { Pop-Location }
    Join-Path $OutDir "MicGuard_${Version}_x64-setup.exe"
}

function Stop-MicGuard {
    Stop-Process -Name MicGuard -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 40 -and (Get-Process MicGuard -ErrorAction SilentlyContinue); $i++) {
        Start-Sleep -Milliseconds 250
    }
    # Relaunching while MicGuard-owned msedgewebview2 children still hold the
    # user-data folder makes WebView2 init fail with 0x8007139F (2026-07-18).
    for ($i = 0; $i -lt 25; $i++) {
        $held = Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'webview-exe-name=MicGuard\.exe' }
        if (-not $held) { break }
        Start-Sleep -Milliseconds 200
    }
}

function Install-MicGuardLocal {
    # Runs the Velopack Setup silently over the installed copy (same version
    # reinstalls too), then starts it through the Run-key stub, the same path
    # Windows autostart uses.
    param([Parameter(Mandatory)][string]$SetupPath)
    if (-not (Test-Path $SetupPath)) { throw "Setup not found: $SetupPath" }
    Stop-MicGuard
    $p = Start-Process $SetupPath -ArgumentList '--silent' -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "Setup exited $($p.ExitCode)" }
    Start-Sleep -Seconds 2
    if (-not (Get-Process MicGuard -ErrorAction SilentlyContinue)) { Start-Process $script:StubExe }
    $ver = Select-String -Path (Join-Path $script:VelopackDir 'current\sq.version') -Pattern '<version>(.+)</version>'
    Write-Host "  Installed $($ver.Matches[0].Groups[1].Value) at $script:VelopackDir" -ForegroundColor Green
}

function Publish-MicGuardRelease {
    # Draft -> upload each asset (retried, --clobber) -> verify sizes -> publish.
    # A single `gh release create <assets>` once died on "ReleaseAsset.name
    # already exists" (gh retried a 30 MB upload that had landed) and gh then
    # deleted the whole draft (v2.0.5, 2026-10-27).
    param([Parameter(Mandatory)][string]$Version,
          [Parameter(Mandatory)][string]$OutDir,
          [Parameter(Mandatory)][string]$Notes)
    $tag = "v$Version"
    $assets = Get-ChildItem $OutDir -File | Where-Object {
        $_.Name -in @("releases.win.json", "RELEASES", "assets.win.json") -or
        $_.Name -like "*.nupkg" -or $_.Name -like "MicGuard_*" }
    gh release create $tag --repo $script:GithubRepo --title "MicGuard $tag" --notes $Notes --draft --verify-tag
    if ($LASTEXITCODE -ne 0) { throw "gh release create (draft) failed" }
    foreach ($a in $assets) {
        $ok = $false
        foreach ($try in 1..3) {
            gh release upload $tag $a.FullName --repo $script:GithubRepo --clobber
            if ($LASTEXITCODE -eq 0) { $ok = $true; break }
            Write-Host "  upload retry $try for $($a.Name)" -ForegroundColor DarkGray
            Start-Sleep -Seconds 3
        }
        if (-not $ok) { throw "upload failed: $($a.Name) (draft $tag left in place - re-run Publish-MicGuardRelease or delete it)" }
    }
    $remote = (gh release view $tag --repo $script:GithubRepo --json assets | ConvertFrom-Json).assets
    foreach ($a in $assets) {
        $r = $remote | Where-Object name -eq $a.Name
        if (-not $r -or $r.size -ne $a.Length) { throw "asset mismatch: $($a.Name) local $($a.Length) remote $($r.size)" }
    }
    gh release edit $tag --repo $script:GithubRepo --draft=false --latest
    if ($LASTEXITCODE -ne 0) { throw "publishing draft $tag failed" }
}
