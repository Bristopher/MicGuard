# Build & Release — how to build MicGuard and ship an update

**Status:** ✅ Current
**Last Updated:** 2026-07-17

The quick-reference version of this lives at the repo root
([RELEASING.md](../../RELEASING.md), next to the script). This doc is the full
walkthrough: building, shipping, writing the release notes, and recovering
from a botched release.

---

## Building locally

```powershell
uv sync                        # one-time: creates .venv from uv.lock
uv run pythonw micguard.py     # run from source (tray icon appears)

# build the distributable exe:
uv run pyinstaller --onefile --noconsole --name MicGuard `
    --icon assets\icon.ico --collect-all webview micguard.py
# -> dist\MicGuard.exe (~21 MB)
```

Flags that matter:
- `--onefile --noconsole` — single exe, no console window (logs go to
  `%APPDATA%\MicGuard\micguard.log`).
- `--collect-all webview` — pywebview ships JS bridge files and
  `WebView2Loader.dll` that PyInstaller doesn't detect; without this the
  frozen exe has no UI. (pythonnet/clr_loader are handled automatically by
  pyinstaller-hooks-contrib.)
- `--icon assets\icon.ico` — the shield icon on the exe file itself.

Test a build by running `dist\MicGuard.exe` and checking the log prints
`starting (frozen=True)` and the settings window opens from the tray.

## Shipping an update — one command

```powershell
.\release.ps1                              # interactive: suggests next version
                                           #   from the latest tag, Enter accepts
.\release.ps1 -Version 1.4.0               # non-interactive, exact version
.\release.ps1 -Bump minor                  # non-interactive, bump from latest tag
.\release.ps1 -Version 1.4.0 -Notes "..."  # with release notes
```

The script (root of the repo):
1. Refuses to run on a dirty working tree or a tag that already exists.
2. Decides the version **at build time** (latest released tag → suggestion →
   your choice) and stamps it into `micguard.py` (`VERSION = "x.y.z"`, what
   update checks compare against) and `pyproject.toml` — **never edit a
   version number by hand**. If `micguard.py` was pre-set ahead of the tags
   for a local test build, the suggestion is to release exactly that version.
3. Rebuilds `dist\MicGuard.exe` with the flags above.
4. Commits `Release vX.Y.Z`, creates an **annotated** tag, pushes commit + tag
   (annotated matters: `--follow-tags` ignores lightweight tags — learned the
   hard way on v1.1.0).
5. Publishes the GitHub release with the exe attached (asset name is always
   exactly `MicGuard.exe` — the updater downloads it by name).
6. Archives a versioned copy at `Releases\vX.Y.Z\MicGuard-X.Y.Z.exe`
   (git-ignored) so every shipped build stays retrievable locally.

Installed copies see the new tag on next launch and **ask** the user to
update (never silent); a failed in-place update opens the releases page.

Pass the release notes via `-Notes "..."` — write them first using
[Release-Notes.md](Release-Notes.md) (template + the AI prompt that drafts
them for you).

## Requirements

- `uv` on PATH; `gh` CLI logged in as **Bristopher** (`gh auth status`).
- A clean `git status`.

## If a release goes wrong mid-way

Fix the cause, then clean up whatever half-happened and re-run:

```powershell
gh release delete vX.Y.Z               # if the release was published
git push origin :refs/tags/vX.Y.Z      # if the tag was pushed
git tag -d vX.Y.Z                      # if the tag was created
git reset --hard HEAD~1                # if the release commit was made
```

## Updating a dev machine's installed copy without a release

One command — `install-test.ps1` (repo root) is the TEST-BUILD path:

```powershell
.\install-test.ps1            # build from the working tree → stop the running
                              #   app → install over %LOCALAPPDATA%\Programs\MicGuard
                              #   → relaunch → log tail + sabotage smoke
.\install-test.ps1 -SkipBuild # reinstall the existing dist\MicGuard.exe
```

No version bump, no tag, no GitHub — releasing stays `release.ps1`'s job.
This is the loop for "pre-stamp the version, build, test locally, THEN
release": stamp `VERSION` + `pyproject.toml`, run `install-test.ps1`, verify,
and `release.ps1` will offer to release exactly the pre-stamped version.
