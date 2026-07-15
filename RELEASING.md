# Releasing a new MicGuard version

The version is decided **at build time** by the release script. It looks at
the latest released `vX.Y.Z` git tag, suggests the next version, and stamps
your choice into both `micguard.py` (`VERSION = "x.y.z"` — what running apps
compare against) and `pyproject.toml`. You never edit a version number by hand.

## The one command

```powershell
.\release.ps1                              # interactive: shows the latest tag,
                                           #   suggests the next version, Enter
                                           #   accepts / type your own (e.g. 1.5.0)
.\release.ps1 -Version 1.4.0               # non-interactive, exact version
.\release.ps1 -Bump minor                  # non-interactive: bump from latest tag
.\release.ps1 -Version 1.4.0 -Notes "..."  # with user-facing release notes
```

That's it. The script:

1. Refuses to run if the working tree has uncommitted changes, or if the
   chosen tag already exists.
2. Suggests the next version from the latest released tag (if `micguard.py`
   was already set ahead of the tags for a local test build, it suggests
   releasing exactly that).
3. Stamps the chosen version into `micguard.py` + `pyproject.toml`.
4. Rebuilds `dist\MicGuard.exe` (PyInstaller onefile, no console, shield icon,
   `--collect-all webview` for the WebView2 UI) and archives a versioned copy
   at `Releases\vX.Y.Z\MicGuard-X.Y.Z.exe` (git-ignored) so every released
   build stays on disk after `dist\` gets overwritten.
5. Commits `Release vX.Y.Z`, creates an annotated tag, pushes branch + tag.
6. Publishes a GitHub release with the exe attached
   (`gh release create vX.Y.Z dist\MicGuard.exe`).

## How users get it

On next launch (or tray → *Check for updates*) MicGuard sees the newer release
tag and **asks** the user; nothing updates silently. If they accept, it swaps
its own exe and restarts. If the in-place update fails for any reason, it
opens https://github.com/Bristopher/MicGuard/releases/latest so they can
download `MicGuard.exe` manually.

## Requirements

- `uv` (deps synced: `uv sync`)
- `gh` CLI logged in to the Bristopher account (`gh auth status`)

## If something goes wrong mid-release

The script is safe to re-run after you fix the cause, but clean up whatever
half-happened first:

```powershell
git tag -d vX.Y.Z                      # if the tag was created
git push origin :refs/tags/vX.Y.Z      # if it was pushed
gh release delete vX.Y.Z               # if the release was published
git reset --hard HEAD~1                # if the release commit was made
```
