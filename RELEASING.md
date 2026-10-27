# Releasing a new MicGuard version

**As of v2.0 (2026-07-23), releases are packaged with [Velopack](https://velopack.io)
(the `vpk` CLI), not a bare PyInstaller onefile exe.** The version is still
decided **at build time** by `release.ps1` — it looks at the latest released
`vX.Y.Z` git tag, suggests the next version, and stamps your choice into both
`micguard.py` (`VERSION = "x.y.z"` — what running apps compare against) and
`pyproject.toml`. You never edit a version number by hand.

Testing a build locally WITHOUT releasing is a different script:
`.\install-test.ps1` (onedir build → `vpk pack` at the current VERSION into
`Releases\local-test\` → silent Velopack Setup over
`%LOCALAPPDATA%\com.cbuzi.micguard`, the copy the Run key autostarts →
relaunch → smoke). No version bump, no tag, no GitHub.

There is also a howler-style interactive driver, `.\build-release-FULL_Build.ps1`:
version-stamp → build + `vpk pack` into `Releases\vX.Y.Z\` → optional local
Setup install/relaunch → then a publish menu: **[1]** release now (it just calls
`release.ps1` — still the single release path), **[2]** (default) emit a
ready-to-paste **Claude prompt** (saved next to the archived build + copied to
the clipboard) that has Claude draft the release notes from the git log since
the last tag and run `release.ps1 -Notes` itself, or **[3]** build only.
Flags: `-Version x.y.z`, `-Install`/`-NoInstall`, `-PromptOnly`.

All three scripts dot-source `build-common.ps1` for the shared steps (onedir
build, `vpk pack`, local Setup install, publish). Publishing creates a
**draft** release, uploads each asset with retries and `--clobber`, checks
every remote size against the local file, and only then publishes it as
Latest. A one-shot `gh release create <assets>` died mid-upload on v2.0.5
("ReleaseAsset.name already exists") and gh deleted the whole release; if an
upload still fails, the draft stays in place for a re-run.

## Prerequisites

- `uv` (deps synced: `uv sync`)
- `gh` CLI logged in to the Bristopher account (`gh auth status`)
- **`dotnet` SDK** — required for the `vpk` (Velopack) CLI. `release.ps1`
  checks for it and throws with a clear message if it's missing; install the
  .NET SDK first if you've never packaged a release on this machine.
- **`vpk`** — the Velopack packaging CLI. `release.ps1` auto-installs it via
  `dotnet tool install -g vpk` the first time it's missing and adds
  `%USERPROFILE%\.dotnet\tools` to `$env:Path` for that session. If it still
  can't find `vpk` after installing (a fresh shell hasn't picked up the PATH
  change yet), open a new terminal and re-run.

## The one command

```powershell
.\release.ps1                              # interactive: shows the latest tag,
                                           #   suggests the next version, Enter
                                           #   accepts / type your own (e.g. 2.1.0)
.\release.ps1 -Version 2.1.0               # non-interactive, exact version
.\release.ps1 -Bump minor                  # non-interactive: bump from latest tag
.\release.ps1 -Version 2.1.0 -Notes "..."  # with user-facing release notes
```

That's it. The script:

1. Refuses to run if the working tree has uncommitted changes, or if the
   chosen tag already exists.
2. Suggests the next version from the latest released tag (if `micguard.py`
   was already set ahead of the tags for a local test build, it suggests
   releasing exactly that).
3. Stamps the chosen version into `micguard.py` + `pyproject.toml`.
4. Ensures `dotnet`/`vpk` are available (installs `vpk` on first use).
5. **Builds the onedir PyInstaller bundle**: `pyinstaller --onedir --noconsole
   --name MicGuard --icon assets\icon.ico --collect-all webview micguard.py`
   → `dist\MicGuard\` (a folder of files, NOT a single exe — Velopack needs
   the unpacked bundle to compute deltas).
6. **Packages with `vpk pack`**: if a prior GitHub release exists, first
   downloads it (`vpk download github --repoUrl ...`) into the output dir so
   `vpk pack` can compute a small DELTA update alongside the full package
   (skipped gracefully — full package only — when there's no prior release,
   e.g. the very first v2.x build). Then `vpk pack --packId
   com.cbuzi.micguard --packTitle MicGuard --packAuthors Bristopher
   --packVersion <ver> --packDir dist\MicGuard --mainExe MicGuard.exe --icon
   assets\icon.ico --outputDir Releases\v<ver>\` produces: a Setup installer,
   a Portable exe, and the delta-update feed files
   (`releases.win.json`/`RELEASES`/`assets.win.json`/`*.nupkg`) — the feed
   files keep their exact vpk-generated names because the in-app updater
   fetches them by fixed name from `releases/latest/download/`.
7. Renames the two human-facing exes (feed files are left untouched):
   `com.cbuzi.micguard-win-Setup.exe` → `MicGuard_<ver>_x64-setup.exe`,
   `com.cbuzi.micguard-win-Portable.exe` → `MicGuard_<ver>_x64-Portable.exe`.
8. Commits `Release vX.Y.Z`, creates an annotated tag, pushes branch + tag.
9. Publishes a GitHub release with the Setup exe, Portable exe, and all feed
   files attached (`gh release create vX.Y.Z <assets...>`).

**⚠️ `gh release create` in the current script omits `--repo`** (relies on
`gh`'s remote inference from the working directory) and there's **no
overwrite-confirmation before `Remove-Item $outDir -Recurse -Force`** deletes
an existing `Releases\v<ver>\` folder if you re-run for the same version —
both are minor, eyeball them on the first real v2.x release.

## How users get it

**Installed copies (Velopack-managed, `is_velopack_installed()` true):** on
next launch (if "Check for updates on launch" is on) or tray → *Check for
updates*, MicGuard's `velopack.UpdateManager` checks the feed and **asks**
before doing anything — nothing updates silently. If accepted, it downloads
the delta (or full package if no delta is available) and
`apply_updates_and_restart()`s itself onto the new version. If the check or
apply fails for any reason, it opens
https://github.com/Bristopher/MicGuard/releases/latest so they can download
the Setup exe manually.

**Source runs and the raw Portable exe** (`is_velopack_installed()` false)
have no install dir to update in place — "Check for updates" fails open
straight to the releases page instead of attempting an in-place update.

**Existing pre-v2.0 (v1.11.x and earlier) installs are a one-time special
case:** the old exe's urllib-based updater has no idea Velopack exists and
can't migrate itself automatically. Those users must download and run
`MicGuard_<ver>_x64-setup.exe` once by hand (linked in the release notes,
which `release.ps1` auto-generates this reminder into when `-Notes` isn't
passed). Velopack installs cleanly over the old install location and their
existing `%APPDATA%\MicGuard\config.json` carries over untouched (config
always lived outside whatever the install mechanism was). After that one
manual install, in-app updates resume working normally for good.

## If something goes wrong mid-release

The script is safe to re-run after you fix the cause, but clean up whatever
half-happened first:

```powershell
git tag -d vX.Y.Z                      # if the tag was created
git push origin :refs/tags/vX.Y.Z      # if it was pushed
gh release delete vX.Y.Z               # if the release was published
git reset --hard HEAD~1                # if the release commit was made
```
