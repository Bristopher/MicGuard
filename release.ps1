# Stamps the version, rebuilds the onedir MicGuard bundle, packages it with
# Velopack (vpk), commits, tags, and publishes a GitHub release. See RELEASING.md.
#
# The version is decided AT BUILD TIME (howler-style): the script looks at the
# latest released git tag, suggests the next version, and lets you accept it or
# type your own. VERSION in micguard.py + pyproject.toml are stamped from that.
#
# Packaging is Velopack (vpk), Gkey Mover v2 style: PyInstaller builds an
# --onedir bundle (dist\MicGuard\), `vpk pack` turns it into a Setup installer
# + Portable exe + delta update feed (releases.win.json / RELEASES /
# assets.win.json / *.nupkg). The feed files keep their exact vpk names - the
# in-app updater fetches them by fixed name from releases/latest/download/.
# The Setup + Portable exes are renamed to MicGuard_<version>_x64-{setup,Portable}.exe
# for human downloaders.
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
. "$PSScriptRoot\build-common.ps1"   # $GithubRepo, build/pack/publish helpers

if (git status --porcelain) {
    throw 'Working tree is not clean - commit or stash your changes first.'
}
gh auth status *> $null
if ($LASTEXITCODE -ne 0) { throw "gh CLI is not logged in - run: gh auth login" }

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
    # suggest: if micguard.py is already ahead of the last tag (pre-bumped for
    # a test build), release exactly that; otherwise bump the last tag's patch
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

# ── Steps 1-3: onedir build, Velopack pack (with delta), rename exes ─────────
# (shared with install-test.ps1 / build-release-FULL_Build.ps1: build-common.ps1)
Write-Host ""
Write-Host "Step 1 - Building with PyInstaller (onedir)..." -ForegroundColor Yellow
Build-MicGuardOnedir
Write-Host ""
Write-Host "Step 2 - Packaging with Velopack..." -ForegroundColor Yellow
$outDir = Join-Path $PSScriptRoot "Releases\v$new"
$null = New-MicGuardPackage -Version $new -OutDir $outDir -WithDelta
Write-Host "  Packaged -> $outDir"

# ── Step 4: Commit, tag, push ─────────────────────────────────────────────────
Write-Host ""
Write-Host "Step 4 - Commit, tag, push..." -ForegroundColor Yellow
git add micguard.py pyproject.toml
# the stamp may be a no-op when the version was pre-bumped for a test build
if (git status --porcelain) { git commit -m "Release v$new" }
# annotated tag + explicit tag push: --follow-tags skips lightweight tags,
# and gh release create refuses a tag that isn't on the remote
git tag -a "v$new" -m "v$new"
git push origin main "v$new"

# ── Step 5: Publish the GitHub release ────────────────────────────────────────
Write-Host ""
Write-Host "Step 5 - Publishing GitHub release..." -ForegroundColor Yellow
if (-not $Notes) {
    $Notes = "MicGuard v$new`n`n" +
        "Existing v1.11.x (and earlier) users: download and run the setup " +
        "installer (MicGuard_${new}_x64-setup.exe) once - the old " +
        "single-exe updater can't migrate itself to the new Velopack-based " +
        "updater automatically. After that one-time install, in-app updates " +
        "resume working normally."
}

Publish-MicGuardRelease -Version $new -OutDir $outDir -Notes $Notes

Write-Host ""
Write-Host "Done: v$new published. Running apps will offer the update on next launch." -ForegroundColor Green
Get-ChildItem $outDir | ForEach-Object {
    $sizeMB = [math]::Round($_.Length / 1MB, 1)
    Write-Host "  $($_.Name) (${sizeMB} MB)"
}
