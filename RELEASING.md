# Releasing a new MicGuard version

The version number lives in exactly one place you ever think about:
`VERSION = "x.y.z"` in `micguard.py`. Everything else (pyproject, git tag,
GitHub release, what running apps compare against) is derived from it by the
release script.

## The one command

```powershell
.\release.ps1                              # patch: 1.0.0 -> 1.0.1
.\release.ps1 -Bump minor                  # 1.0.1 -> 1.1.0
.\release.ps1 -Bump major -Notes "Rewrite" # 1.1.0 -> 2.0.0
```

That's it. The script:

1. Refuses to run if the working tree has uncommitted changes.
2. Reads `VERSION` from `micguard.py`, bumps the requested part, and writes
   the new number back to both `micguard.py` and `pyproject.toml` — you never
   edit a version number by hand.
3. Rebuilds `dist\MicGuard.exe` (PyInstaller onefile, no console).
4. Commits `Release vX.Y.Z`, tags `vX.Y.Z`, pushes with `--follow-tags`.
5. Publishes a GitHub release with the exe attached
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
