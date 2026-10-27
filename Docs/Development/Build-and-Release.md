# Build & Release — how to build MicGuard and ship an update

**Status:** ✅ Current
**Last Updated:** 2026-10-27 (Velopack-era build, install-test and publish flow)

The quick-reference version of this lives at the repo root
([RELEASING.md](../../RELEASING.md), next to the script). This doc is the full
walkthrough: building, shipping, writing the release notes, and recovering
from a botched release.

---

## Building locally

```powershell
uv sync                        # one-time: creates .venv from uv.lock
uv run pythonw micguard.py     # run from source (tray icon appears)

# build the onedir bundle (what Velopack packs; build-common.ps1 does this):
uv run pyinstaller --noconfirm --onedir --noconsole --name MicGuard `
    --icon assets\icon.ico --collect-all webview micguard.py
# -> dist\MicGuard\MicGuard.exe + dist\MicGuard\_internal\
```

Flags that matter:
- `--onedir --noconsole` — a folder Velopack can delta-update, no console
  window (logs go to `%APPDATA%\MicGuard\micguard.log`, rotated at 2 MB x 3).
- `--collect-all webview` — pywebview ships JS bridge files and
  `WebView2Loader.dll` that PyInstaller doesn't detect; without this the
  frozen exe has no UI. (pythonnet/clr_loader are handled automatically by
  pyinstaller-hooks-contrib.)
- `--icon assets\icon.ico` — the shield icon on the exe file itself.

Test a build by running `dist\MicGuard\MicGuard.exe` and checking the log
prints `starting (frozen=True)` and the settings window opens from the tray.
Note that in-app update/uninstall only work in a Velopack-installed copy, so
use `install-test.ps1` (below) to test those.

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
3. Rebuilds the onedir bundle and packs it with `vpk` into `Releases\vX.Y.Z\`
   (git-ignored): `MicGuard_X.Y.Z_x64-setup.exe`, the Portable zip, the full
   and delta `.nupkg` and the feed files (`releases.win.json`, `RELEASES`,
   `assets.win.json`, never renamed: the in-app updater fetches them by name).
4. Commits `Release vX.Y.Z`, creates an **annotated** tag, pushes commit + tag
   (annotated matters: `--follow-tags` ignores lightweight tags — learned the
   hard way on v1.1.0).
5. Publishes the GitHub release as a **draft first**, uploads each asset with
   retries, checks every remote size against the local file, then publishes
   it as Latest (`Publish-MicGuardRelease` in `build-common.ps1`).

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

If only the publish step failed (tag already pushed), a draft release with
whatever uploaded is left on GitHub. Finish it from the repo root without
rebuilding: `. .\build-common.ps1; gh release delete vX.Y.Z --yes;
Publish-MicGuardRelease -Version X.Y.Z -OutDir Releases\vX.Y.Z -Notes "..."`.

```powershell
gh release delete vX.Y.Z               # if the release was published
git push origin :refs/tags/vX.Y.Z      # if the tag was pushed
git tag -d vX.Y.Z                      # if the tag was created
git reset --hard HEAD~1                # if the release commit was made
```

## Updating a dev machine's installed copy without a release

One command — `install-test.ps1` (repo root) is the TEST-BUILD path:

```powershell
.\install-test.ps1            # build from the working tree → vpk pack at the
                              #   current VERSION into Releases\local-test\ →
                              #   silent Setup over %LOCALAPPDATA%\com.cbuzi.micguard
                              #   (the autostart copy) → relaunch via the Run-key
                              #   stub → log tail + sabotage smoke
.\install-test.ps1 -SkipBuild # reinstall the last Releases\local-test Setup
```

Velopack's Setup reinstalls over the same version, so a test build replaces
the installed files without a version bump.

No version bump, no tag, no GitHub — releasing stays `release.ps1`'s job.
This is the loop for "pre-stamp the version, build, test locally, THEN
release": stamp `VERSION` + `pyproject.toml`, run `install-test.ps1`, verify,
and `release.ps1` will offer to release exactly the pre-stamped version.
