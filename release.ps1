# Stamps the version, rebuilds MicGuard.exe, commits, tags, and publishes a
# GitHub release. See RELEASING.md.
#
# The version is decided AT BUILD TIME (howler-style): the script looks at the
# latest released git tag, suggests the next version, and lets you accept it or
# type your own. VERSION in micguard.py + pyproject.toml are stamped from that.
#
# Usage:
#   .\release.ps1                          # interactive: suggests next version,
#                                          #   Enter accepts / type a custom one
#   .\release.ps1 -Version 1.4.0           # non-interactive, exact version
#   .\release.ps1 -Bump minor              # non-interactive, bump from latest tag
#   .\release.ps1 -Version 1.4.0 -Notes "..."   # with release notes
param(
    [string]$Version = '',
    [ValidateSet('patch', 'minor', 'major')]
    [string]$Bump = '',
    [string]$Notes = ''
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (git status --porcelain) {
    throw 'Working tree is not clean - commit or stash your changes first.'
}

# ── Detect the latest RELEASED version from git tags ─────────────────────────
$latestTag = git tag --list 'v*' |
    Where-Object { $_ -match '^v(\d+)\.(\d+)\.(\d+)$' } |
    ForEach-Object {
        $null = $_ -match '^v(\d+)\.(\d+)\.(\d+)$'
        [PSCustomObject]@{ Major = [int]$Matches[1]; Minor = [int]$Matches[2]; Patch = [int]$Matches[3] }
    } |
    Sort-Object Major, Minor, Patch | Select-Object -Last 1

# current VERSION in micguard.py (may already be pre-bumped for local testing)
$src = Get-Content micguard.py -Raw
if ($src -notmatch 'VERSION = "(\d+)\.(\d+)\.(\d+)"') {
    throw 'VERSION = "x.y.z" not found in micguard.py'
}
$curMajor, $curMinor, $curPatch = [int]$Matches[1], [int]$Matches[2], [int]$Matches[3]
$current = "$curMajor.$curMinor.$curPatch"

if ($latestTag) {
    $released = "$($latestTag.Major).$($latestTag.Minor).$($latestTag.Patch)"
    Write-Host "Latest released tag: v$released"
    # suggest: if micguard.py is already ahead of the last tag (pre-bumped for a
    # test build), release exactly that; otherwise bump the last tag's patch
    $aheadOfTag = ($curMajor -gt $latestTag.Major) -or
        ($curMajor -eq $latestTag.Major -and $curMinor -gt $latestTag.Minor) -or
        ($curMajor -eq $latestTag.Major -and $curMinor -eq $latestTag.Minor -and $curPatch -gt $latestTag.Patch)
    $suggested = if ($aheadOfTag) { $current } else {
        "$($latestTag.Major).$($latestTag.Minor).$($latestTag.Patch + 1)"
    }
    $bumpBase = $latestTag
} else {
    $suggested = $current
    $bumpBase = [PSCustomObject]@{ Major = $curMajor; Minor = $curMinor; Patch = $curPatch }
}
Write-Host "Version in micguard.py:  v$current"

# ── Decide the version: -Version > -Bump > interactive prompt ────────────────
if ($Version) {
    $new = $Version.Trim().TrimStart('v')
} elseif ($Bump) {
    $major, $minor, $patch = $bumpBase.Major, $bumpBase.Minor, $bumpBase.Patch
    switch ($Bump) {
        'major' { $major++; $minor = 0; $patch = 0 }
        'minor' { $minor++; $patch = 0 }
        'patch' { $patch++ }
    }
    $new = "$major.$minor.$patch"
} else {
    Write-Host "Suggested next version:  v$suggested"
    $userInput = Read-Host 'Press Enter to accept, or type a custom version (e.g. 1.5.0)'
    $new = if ($userInput.Trim() -ne '') { $userInput.Trim().TrimStart('v') } else { $suggested }
}
if ($new -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid version '$new' - expected x.y.z" }
if (git tag --list "v$new") { throw "Tag v$new already exists - pick a different version." }
Write-Host "Releasing v$new" -ForegroundColor Green

# ── Stamp the version into micguard.py + pyproject.toml ─────────────────────
($src -replace 'VERSION = "\d+\.\d+\.\d+"', "VERSION = `"$new`"") |
    Set-Content micguard.py -NoNewline
# (?m) so ^ matches the version line inside the raw file - without it the
# mirror silently never happened (pyproject sat at 1.0.0 until 2026-07-12)
(Get-Content pyproject.toml -Raw) -replace '(?m)^version = "\d+\.\d+\.\d+"', "version = `"$new`"" |
    Set-Content pyproject.toml -NoNewline
Write-Host "  Stamped micguard.py + pyproject.toml -> $new"

# ── Build ─────────────────────────────────────────────────────────────────────
uv run pyinstaller --onefile --noconsole --name MicGuard `
    --icon assets\icon.ico --collect-all webview micguard.py
if (-not (Test-Path dist\MicGuard.exe)) { throw 'Build failed - no dist\MicGuard.exe' }

# ── Archive a versioned copy (howler-style Releases\vX.Y.Z\) ─────────────────
# dist\MicGuard.exe gets overwritten by every build; keep each release's exe
# under Releases\ (git-ignored) so old builds stay reproducible on disk. The
# GitHub asset stays exactly `MicGuard.exe` — the in-app updater requires it.
$archiveDir = Join-Path $PSScriptRoot "Releases\v$new"
New-Item -ItemType Directory -Force $archiveDir | Out-Null
Copy-Item dist\MicGuard.exe (Join-Path $archiveDir "MicGuard-$new.exe") -Force
Write-Host "  Archived -> Releases\v$new\MicGuard-$new.exe"

# ── Commit, tag, publish ─────────────────────────────────────────────────────
git add micguard.py pyproject.toml
# the stamp may be a no-op when the version was pre-bumped for a test build
if (git status --porcelain) { git commit -m "Release v$new" }
# annotated tag + explicit tag push: --follow-tags skips lightweight tags,
# and gh release create refuses a tag that isn't on the remote
git tag -a "v$new" -m "v$new"
git push origin main "v$new"

if (-not $Notes) { $Notes = "MicGuard v$new" }
gh release create "v$new" dist\MicGuard.exe --title "MicGuard v$new" --notes $Notes

Write-Host "Done: v$new published. Running apps will offer the update on next launch." -ForegroundColor Green
