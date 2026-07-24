# MicGuard → Velopack installer + auto-update migration

**Date:** 2026-07-23
**Status:** 🟡 Awaiting Bristopher's review
**Target version:** v2.0.0 (distribution overhaul — breaking change to how MicGuard is installed/updated)
**Supersedes:** the rejected in-app self-installer (archived at branch `archive/in-app-self-install`)

Migrates MicGuard from a single portable `--onefile` exe with a urllib rename-swap
updater to a **Velopack**-packaged app — the same installer/update framework used by
the sibling app Gkey Mover v2 / ClipShelf. Velopack produces a real `Setup.exe`
(Start Menu, Windows Search, Add/Remove Programs) plus delta self-updates, and a
self-updating portable bundle. This is the model Bristopher asked for ("use velopack
like my other apps").

## Why

- The current app is a loose `--onefile` exe. It self-registers Add/Remove and an
  optional Run key but creates **no Start Menu shortcut**, so it never shows in the
  Start Menu / Windows Search. Bristopher's original ask was Start-Menu/Windows-Search
  integration.
- Velopack is the proven answer already in use in Gkey Mover v2, and now has an
  official Python SDK (`pip install velopack`, docs.velopack.io/getting-started/python).
- It replaces the hand-rolled updater (rename-swap + `--updated` relaunch + WebView2
  teardown race handling) with a maintained framework doing delta updates.

## The one hard constraint (and its consequence)

**Velopack requires PyInstaller `--onedir`, not `--onefile`** (its docs: a directory
structure is required; `--onefile` is incompatible). Consequences, both accepted by
Bristopher:
- MicGuard is no longer a single downloadable exe. Users download
  `MicGuard_x.y.z_x64-setup.exe` (or the portable bundle).
- The dev-guide's **Rule 1 (stdlib-first for onefile size)** no longer applies — the
  app ships as a folder inside an installer, so a `velopack` dependency and onedir
  bloat are fine. Rule 1 is rewritten as part of this change.

## Identity (immutable once shipped — like Gkey's `com.cbuzi.*`)

| Thing | Value | Notes |
|---|---|---|
| packId | `com.cbuzi.micguard` | Velopack bundle id; installs to `%LOCALAPPDATA%\com.cbuzi.micguard\`. NEVER change — installed apps update/locate by it. |
| packTitle | `MicGuard` | shortcut + Add/Remove display name |
| packAuthors | `Bristopher` | |
| mainExe | `MicGuard.exe` | |
| Install dir | `%LOCALAPPDATA%\com.cbuzi.micguard\` | Velopack layout: `current\` (app files) + `Update.exe` + `MicGuard.exe` (stable stub) |
| Config/logs | `%APPDATA%\MicGuard\` (UNCHANGED) | deliberately OUTSIDE the install dir so updates (which wipe `current\`) never touch settings; existing configs survive |
| Feed URL | `https://github.com/Bristopher/MicGuard/releases/latest/download/` | GitHub rewrites `latest/download/<asset>` to the newest release's asset (same trick Gkey uses) |
| GITHUB_REPO | `Bristopher/MicGuard` (UNCHANGED) | |

## App code changes (`micguard.py`)

### Added
- **`import velopack`** and, as the FIRST statement in `main()` (before logging,
  mutex, anything): `velopack.App().run()`. Velopack may quit/restart the process to
  perform install/update/uninstall hooks, so it must run first.
- **`update_feed_url()`** → the constant feed URL above.
- **`installed_exe_stub()`** → `%LOCALAPPDATA%\com.cbuzi.micguard\MicGuard.exe` (the
  stable stub, computed from packId + `LOCALAPPDATA`). Used for the autostart Run-key
  target (the stub always points at the latest `current\`, unlike `sys.executable`
  which is `…\current\MicGuard.exe` and changes each update).
- **Velopack update flow** replacing the old checker. A thin wrapper around
  `velopack.UpdateManager(update_feed_url())`:
  - `check`: `mgr.check_for_updates()` → `None` if none.
  - consent: the EXISTING `_dialog("askyesno", …)` before applying (product rule:
    never update without consent; fail-open to the releases page on error).
  - apply: `mgr.download_updates(info)` then `mgr.apply_updates_and_restart(info)`.
  - Startup check stays gated by the `check_updates` config (default True), on a
    daemon thread; manual check stays on the tray + Settings.
  - Guard: only attempt when running as an installed Velopack app (not from source /
    not portable-dev) — `UpdateManager` raising/returning None is handled as "no update".

### Removed (Velopack owns these now)
- Updater: `apply_update`, `fetch_latest_release`, `cleanup_old_exe`,
  `_process_snapshot`, `_cmdline_of`, `webview_teardown_pending`,
  `wait_webview_teardown`, and all `--updated` handling in `main()`.
  (`parse_version` may stay if still referenced; otherwise remove.)
- Add/Remove self-registration: `uninstall_entry_values`, `register_uninstall_entry`,
  `unregister_uninstall_entry`, and the `UNINSTALL_KEY`/`PUBLISHER` uses tied to it.
- Self-delete uninstall: `uninstall_self`, `uninstall_from_arp`, and the `--uninstall`
  arg branch in `main()`. Tray "Uninstall" now shells to Velopack:
  `subprocess.Popen([<installdir>\Update.exe, "--uninstall"])` after best-effort
  clearing `%APPDATA%\MicGuard` (so "zero leftovers" is preserved), then quits.

### Changed
- **Autostart Run key**: `set_run_at_startup(True)` writes the stub path
  (`installed_exe_stub()`), not `launch_command()`/`sys.executable`. From source it
  keeps the existing pythonw command (dev convenience). The Settings toggle still owns
  it at runtime.
- **`main()`**: `velopack.App().run()` first; drop `--updated`/`--uninstall` branches;
  keep the single-instance mutex; if a WebView2-relaunch settle is still needed after
  a Velopack restart, add a short `EBWebView` lock wait in Velopack's `on_restarted`
  hook (mirroring Gkey's `wait_for_webview_unlock`) rather than the old teardown poll.
- **`IS_FROZEN`** stays; a new `IS_INSTALLED` (running from the Velopack `current\`
  dir under the packId) gates update/uninstall/autostart-stub behavior.

### Unchanged
Enforcer + Core Audio + `IPolicyConfig`, all pywebview windows (settings/menu/alert/
OSD/mixer), the mixer feature, hotkeys, config load/save/merge, history. The migration
is distribution + update plumbing only; the app's behavior is untouched.

## Dependencies

- `uv add velopack` (Python SDK; wraps the Velopack `Update.exe` runtime, which
  `vpk pack` injects into the bundle — do not vendor it manually).
- `vpk` CLI on the build machine: `dotnet tool install -g vpk` (needs the .NET SDK).
  `release.ps1` checks for `vpk` and installs/instructs if missing.

## Build & release (`release.ps1` rewritten Gkey-style)

Mirrors Gkey Mover v2's `build-release.ps1` flow:
1. Guard clean git tree + `gh` auth + `vpk` present (install if not).
2. Pick/stamp version (VERSION in `micguard.py` stays the single source of truth,
   mirrored to `pyproject.toml` — the standing rule is unchanged; `release.ps1` is
   still the only thing that bumps it).
3. `uv run pyinstaller --onedir --noconsole --name MicGuard --icon assets\icon.ico
   --collect-all webview micguard.py` → `dist\MicGuard\` (folder).
4. `vpk download github --repoUrl https://github.com/Bristopher/MicGuard` (fetch prior
   release so Velopack can build a delta) — tolerate "no prior Velopack release" on the
   first v2 build.
5. `vpk pack --packId com.cbuzi.micguard --packTitle "MicGuard" --packAuthors
   "Bristopher" --packVersion <v> --packDir dist\MicGuard --mainExe MicGuard.exe
   --icon assets\icon.ico --outputDir Releases\v<v>` → produces in `Releases\v<v>`:
   the Setup.exe, the portable bundle, and the delta feed (`*.nupkg`,
   `releases.win.json`, `RELEASES`, `assets.win.json`).
6. Rename user-facing exes (Gkey convention): Setup → `MicGuard_<v>_x64-setup.exe`,
   portable → `MicGuard_<v>_x64-Portable.exe`. **Feed files keep their exact names**
   (`releases.win.json`, `RELEASES`, `*.nupkg`) — `UpdateManager` fetches them by fixed
   filename from the feed URL.
7. Commit `Release v<v>`, tag, push, `gh release create v<v>` uploading: the feed files
   (fixed names) + the setup + the portable exes.

Shortcuts: Velopack's installer creates Start Menu + Desktop shortcuts by default
(`--shortcuts` can narrow to `StartMenuRoot`/`Desktop`/`Startup`/`None`). Default
(Start Menu + Desktop) is fine and satisfies the Windows-Search ask; revisit if a
Desktop shortcut is unwanted.

## Migration for existing v1.11.x users

Existing installs run the OLD rename-swap updater, which looks for a raw `MicGuard.exe`
release asset. The v2.0.0 release does **not** ship that asset (only the Velopack
setup/portable/feed), so old clients see "no compatible asset" and fall back to opening
the releases page — where the user downloads `MicGuard_2.0.0_x64-setup.exe` **once**.
The release notes state this explicitly. After that one manual step they're on Velopack
and auto-update from then on. Config at `%APPDATA%\MicGuard` carries over untouched.

## Version

**v2.0.0** — the install/update/distribution model changes; a major bump is honest and
signals the one-time manual migration. `release.ps1` performs the bump at release time.

## Testing

- **pytest (pure):** `update_feed_url`, `installed_exe_stub` (path math from packId +
  LOCALAPPDATA), and any `IS_INSTALLED` detection factored purely. The existing 141
  tests must stay green (the migration removes updater internals some tests may cover —
  those tests are removed/retargeted, noted per-task).
- **Manual (build/COM/install):** build the onedir + `vpk pack`; run `Setup.exe` →
  app installs to `%LOCALAPPDATA%\com.cbuzi.micguard`, Start Menu + Windows Search entry
  appears, launches, tray works, enforcement + mixer + settings all function; config at
  `%APPDATA%\MicGuard` intact. Ship a v2.0.1 test build → in-app "Check for updates"
  downloads the delta and relaunches on the new version (consent dialog first).
  Uninstall via Add/Remove AND via tray → both remove shortcuts + install dir + Run key,
  and clear `%APPDATA%\MicGuard`. Autostart survives a reboot (Run key → stub path).
  These go in the Verification Backlog with the ship commit range.

## Ideology fit / departures

- **No admin** — Velopack installs per-user to `%LOCALAPPDATA%` (no elevation). ✅
- **Consent-based updates** — kept (dialog before apply; fail-open to releases page). ✅
- **Event-driven, tray never dies** — unchanged; update/uninstall shell out and degrade.
- **DEPARTURE — onefile/stdlib-first (Rule 1)** — intentionally dropped; onedir +
  Velopack + the `velopack` dependency replace it. Dev guide updated in the same change.
- **DEPARTURE — self-managed Add/Remove + self-delete uninstall** — replaced by
  Velopack's installer/uninstaller.

## Cross-cutting registrations (same change as the feature)

- `Docs/System-Conventions.md`: replace/append the install/update/startup system with
  the Velopack model (packId source of truth; `%LOCALAPPDATA%\<packId>` layout; config
  outside install dir; `UpdateManager` update flow; `Update.exe --uninstall`; stub-path
  autostart).
- `Docs/AI-Development-Guide.md`: rewrite Rule 1 (onefile → onedir + Velopack) and the
  updater/uninstall rules; add the immutable-identity note (packId / feed / config dir).
- `Docs/Architecture.md`: update the distribution + update sections.
- `RELEASING.md`: rewrite for the `vpk pack` flow.
- A feature doc from `Docs/Feature-Template.md` + index row.
- Verification Backlog §20 with the manual items above.

## Open items flagged for review

1. **v2.0.0** version bump — confirm.
2. **One-time manual migration** for existing users — confirm the fail-open-to-releases
   behavior is acceptable (vs. shipping a one-shot raw-exe "migrator" asset — more work,
   probably unnecessary for a small user base).
3. **Desktop shortcut** on by default (Velopack default) — keep, or Start-Menu-only?
4. **packId `com.cbuzi.micguard`** — confirm the identifier convention.
