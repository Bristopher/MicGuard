# Bumps the version, rebuilds MicGuard.exe, commits, tags, and publishes a
# GitHub release. See RELEASING.md. Usage:
#   .\release.ps1                      # patch bump (1.0.0 -> 1.0.1)
#   .\release.ps1 -Bump minor          # 1.0.1 -> 1.1.0
#   .\release.ps1 -Bump major -Notes "Big rewrite"
param(
    [ValidateSet('patch', 'minor', 'major')]
    [string]$Bump = 'patch',
    [string]$Notes = ''
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (git status --porcelain) {
    throw 'Working tree is not clean - commit or stash your changes first.'
}

# micguard.py is the single source of truth for the version
$src = Get-Content micguard.py -Raw
if ($src -notmatch 'VERSION = "(\d+)\.(\d+)\.(\d+)"') {
    throw 'VERSION = "x.y.z" not found in micguard.py'
}
$major, $minor, $patch = [int]$Matches[1], [int]$Matches[2], [int]$Matches[3]
switch ($Bump) {
    'major' { $major++; $minor = 0; $patch = 0 }
    'minor' { $minor++; $patch = 0 }
    'patch' { $patch++ }
}
$new = "$major.$minor.$patch"
Write-Host "Releasing v$new" -ForegroundColor Green

($src -replace 'VERSION = "\d+\.\d+\.\d+"', "VERSION = `"$new`"") |
    Set-Content micguard.py -NoNewline
(Get-Content pyproject.toml -Raw) -replace '^version = "\d+\.\d+\.\d+"', "version = `"$new`"" |
    Set-Content pyproject.toml -NoNewline

uv run pyinstaller --onefile --noconsole --name MicGuard `
    --icon assets\icon.ico --collect-all webview micguard.py
if (-not (Test-Path dist\MicGuard.exe)) { throw 'Build failed - no dist\MicGuard.exe' }

git add micguard.py pyproject.toml
git commit -m "Release v$new"
# annotated tag + explicit tag push: --follow-tags skips lightweight tags,
# and gh release create refuses a tag that isn't on the remote
git tag -a "v$new" -m "v$new"
git push origin main "v$new"

if (-not $Notes) { $Notes = "MicGuard v$new" }
gh release create "v$new" dist\MicGuard.exe --title "MicGuard v$new" --notes $Notes

Write-Host "Done: v$new published. Running apps will offer the update on next launch." -ForegroundColor Green
