# build-release-FULL_Build.ps1 — howler-style full build driver for MicGuard.
#
# What this does:
#   1. Picks the version AT BUILD TIME (suggests next from the latest git tag;
#      honors a pre-stamped VERSION in micguard.py) and stamps micguard.py +
#      pyproject.toml.
#   2. Builds the onefile MicGuard.exe via PyInstaller and archives a versioned
#      copy under Releases\vX.Y.Z\ (git-ignored).
#   3. Optionally installs it over %LOCALAPPDATA%\Programs\MicGuard and
#      relaunches, for hands-on testing.
#   4. Optionally publishes to GitHub — EITHER directly through release.ps1
#      (still THE single release path: commit, tag, gh release), OR by spitting
#      out a ready-to-paste Claude prompt (also copied to the clipboard and
#      saved next to the archived exe) that has Claude write the release notes
#      from the git log and run release.ps1 itself.
#
# Flags (all optional — no flags = fully interactive):
#   -Version x.y.z   Skip the version prompt.
#   -Install         Install + relaunch locally without asking.
#   -NoInstall       Skip the install question entirely.
#   -PromptOnly      Don't publish; just build and emit the Claude prompt.
#
# Typical usage:
#   .\build-release-FULL_Build.ps1                 # interactive everything
#   .\build-release-FULL_Build.ps1 -Version 1.9.0 -Install -PromptOnly

param(
    [string]$Version = '',
    [switch]$Install,
    [switch]$NoInstall,
    [switch]$PromptOnly
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# ── Detect the latest RELEASED version from git tags ─────────────────────────
$latestTag = git tag --list 'v*' |
    Where-Object { $_ -match '^v(\d+)\.(\d+)\.(\d+)$' } |
    ForEach-Object {
        $null = $_ -match '^v(\d+)\.(\d+)\.(\d+)$'
        [PSCustomObject]@{ Major = [int]$Matches[1]; Minor = [int]$Matches[2]; Patch = [int]$Matches[3] }
    } |
    Sort-Object Major, Minor, Patch | Select-Object -Last 1

$src = Get-Content micguard.py -Raw
if ($src -notmatch 'VERSION = "(\d+)\.(\d+)\.(\d+)"') {
    throw 'VERSION = "x.y.z" not found in micguard.py'
}
$curMajor, $curMinor, $curPatch = [int]$Matches[1], [int]$Matches[2], [int]$Matches[3]
$current = "$curMajor.$curMinor.$curPatch"

if ($latestTag) {
    $released = "$($latestTag.Major).$($latestTag.Minor).$($latestTag.Patch)"
    Write-Host "Latest released tag: v$released"
    $aheadOfTag = ($curMajor -gt $latestTag.Major) -or
        ($curMajor -eq $latestTag.Major -and $curMinor -gt $latestTag.Minor) -or
        ($curMajor -eq $latestTag.Major -and $curMinor -eq $latestTag.Minor -and $curPatch -gt $latestTag.Patch)
    $suggested = if ($aheadOfTag) { $current } else {
        "$($latestTag.Major).$($latestTag.Minor).$($latestTag.Patch + 1)"
    }
} else {
    $released = $null
    $suggested = $current
}
Write-Host "Version in micguard.py:  v$current"

# ── Decide the version ───────────────────────────────────────────────────────
if ($Version.Trim() -ne '') {
    $new = $Version.Trim().TrimStart('v')
    Write-Host "Using supplied version: v$new"
} else {
    Write-Host "Suggested next version:  v$suggested"
    $userInput = Read-Host 'Press Enter to accept, or type a custom version (e.g. 1.9.0)'
    $new = if ($userInput.Trim() -ne '') { $userInput.Trim().TrimStart('v') } else { $suggested }
}
if ($new -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid version '$new' - expected x.y.z" }
if (git tag --list "v$new") { throw "Tag v$new already exists - pick a different version." }

Write-Host ''
Write-Host "Building MicGuard v$new..." -ForegroundColor Cyan

# ── Stamp micguard.py + pyproject.toml ───────────────────────────────────────
($src -replace 'VERSION = "\d+\.\d+\.\d+"', "VERSION = `"$new`"") |
    Set-Content micguard.py -NoNewline
(Get-Content pyproject.toml -Raw) -replace '(?m)^version = "\d+\.\d+\.\d+"', "version = `"$new`"" |
    Set-Content pyproject.toml -NoNewline
Write-Host "  Stamped micguard.py + pyproject.toml -> $new"

# ── Build ────────────────────────────────────────────────────────────────────
uv run pyinstaller --onefile --noconsole --name MicGuard `
    --icon assets\icon.ico --collect-all webview micguard.py
if ($LASTEXITCODE -ne 0 -or -not (Test-Path dist\MicGuard.exe)) {
    throw 'Build failed - no dist\MicGuard.exe'
}
$sizeMB = [math]::Round((Get-Item dist\MicGuard.exe).Length / 1MB, 1)
Write-Host "  Built dist\MicGuard.exe ($sizeMB MB)" -ForegroundColor Green

# ── Archive a versioned copy (Releases\vX.Y.Z\, git-ignored) ─────────────────
$archiveDir = Join-Path $PSScriptRoot "Releases\v$new"
New-Item -ItemType Directory -Force $archiveDir | Out-Null
Copy-Item dist\MicGuard.exe (Join-Path $archiveDir "MicGuard-$new.exe") -Force
Write-Host "  Archived -> Releases\v$new\MicGuard-$new.exe"

# ── Optional: install locally + relaunch ─────────────────────────────────────
$doInstall = $Install
if (-not $Install -and -not $NoInstall) {
    $ans = Read-Host 'Install over %LOCALAPPDATA%\Programs\MicGuard and relaunch for testing? [y/N]'
    $doInstall = $ans.Trim().ToLower() -eq 'y'
}
if ($doInstall) {
    $installDir = Join-Path $env:LOCALAPPDATA 'Programs\MicGuard'
    New-Item -ItemType Directory -Force $installDir | Out-Null
    Get-Process MicGuard -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep 1
    Copy-Item dist\MicGuard.exe (Join-Path $installDir 'MicGuard.exe') -Force
    Start-Process (Join-Path $installDir 'MicGuard.exe')
    Write-Host "  Installed + relaunched from $installDir" -ForegroundColor Green
}

# ── Publish choice ───────────────────────────────────────────────────────────
$logRange = if ($released) { "v$released..HEAD" } else { '' }
$commitLog = if ($logRange) { git log $logRange --oneline --no-merges } else { git log --oneline --no-merges }
$commitLog = ($commitLog | Out-String).Trim()

$choice = '2'
if (-not $PromptOnly) {
    Write-Host ''
    Write-Host 'Publish to GitHub?' -ForegroundColor Yellow
    Write-Host '  [1] Yes, now — release.ps1 runs with basic notes (commit, tag, gh release)'
    Write-Host '  [2] Claude prompt — write a ready-to-paste prompt so Claude drafts the'
    Write-Host '      release notes from the git log and runs release.ps1 for you (default)'
    Write-Host '  [3] No — build only, publish later'
    $choice = (Read-Host 'Choose [1/2/3] (default: 2)').Trim()
    if ($choice -eq '') { $choice = '2' }
}

switch ($choice) {
    '1' {
        # release.ps1 stays THE release path — it re-verifies the clean tree,
        # rebuilds, commits the stamp, tags, and publishes the exe asset
        # (named exactly MicGuard.exe — the in-app updater requires it).
        .\release.ps1 -Version $new
    }
    '3' {
        Write-Host ''
        Write-Host "Build only — publish later with .\release.ps1 -Version $new" -ForegroundColor DarkGray
    }
    default {
        $releasedLine = if ($released) { "v$released" } else { '(none — this is the first release)' }
        $prompt = @"
Release MicGuard v$new to GitHub.

The exe is already built and version-stamped (micguard.py + pyproject.toml at
$new). Commits since the last release ($releasedLine):

$commitLog

Do this:
1. Write user-facing release notes for v$new from those commits — follow the
   template and tone in Docs/Development/Release-Notes.md (lead with what the
   user gets, group by feature, no commit hashes, no AI voice).
2. Commit any outstanding changes first if the tree is dirty (release.ps1
   refuses a dirty tree).
3. Publish with:  .\release.ps1 -Version $new -Notes "<the notes>"
   (it rebuilds, commits the stamp if needed, tags v$new, and uploads the
   asset named exactly MicGuard.exe — never rename it, the in-app updater
   depends on that name).
4. Confirm the release page looks right and report the URL.
"@
        $promptPath = Join-Path $archiveDir "claude-release-prompt-v$new.txt"
        Set-Content $promptPath $prompt -Encoding UTF8
        try { Set-Clipboard $prompt; $clip = ' (copied to clipboard)' } catch { $clip = '' }
        Write-Host ''
        Write-Host "Claude release prompt saved -> $promptPath$clip" -ForegroundColor Green
        Write-Host ''
        Write-Host $prompt -ForegroundColor White
    }
}

Write-Host ''
Write-Host "Done. MicGuard v$new built." -ForegroundColor Green
