# Velopack Install / Update / Uninstall

**Status:** 🏗️ In Development (code shipped, manual-verify pending — see Verification Backlog §20)
**Author:** Bristopher (design/plan) + Claude (implementation)
**Date:** 2026-07-23
**Version:** 2.0.0

---

## Overview

Through v1.11, MicGuard shipped as a single PyInstaller `--onefile` exe with a
hand-rolled updater (urllib download + rename-the-running-exe-aside swap) and
a self-registered `HKCU\...\Uninstall` Add/Remove Programs entry. That worked,
but it meant: no Start Menu entry, no Windows Search discoverability, every
update was a full-size download, and the uninstall/update code was all
MicGuard-maintained surface area for something Windows has a real mechanism
for.

**v2.0 replaces all of that with [Velopack](https://velopack.io)** — the app
now ships as a PyInstaller `--onedir` bundle packaged by the `vpk` CLI into a
real Setup installer + a Portable exe + a delta-update feed. Velopack owns
install, Start Menu/Search registration, in-place delta updates, and Add/
Remove Programs uninstall. MicGuard's own code shrinks to: identity constants,
a handful of pure path/detection helpers, running `velopack.App().run()`
first in `main()`, wiring the Settings/tray update flow to
`velopack.UpdateManager`, and one tray "Uninstall…" handler that shells out to
Velopack's own uninstaller.

This is a **packaging/distribution change, not a feature the end user
interacts with differently** day-to-day — the tray icon, enforcement, mixer,
settings, hotkeys, Mic EQ, etc. are all unchanged. The visible differences are:
a real installer + Start Menu shortcut, smaller/faster updates, and a native
Add/Remove Programs uninstall entry.

## Architecture

```
main()
 ├─ velopack.App().on_restarted(_velopack_on_restarted).run()   ← MUST be first
 │     Lets Velopack intercept install/uninstall/update hooks and
 │     quit/restart the process as needed. Wrapped in try/except so a
 │     source/dev run (no Velopack context present) never crashes here.
 ├─ already_running(0.0)                       ← single-instance mutex, as before
 └─ App() / app.run()                          ← unchanged tray/enforcement/UI

Update check (tray "Check for updates" / Settings row / launch check)
 └─ App._update_check(quiet)
     ├─ not is_velopack_installed()? → fail open: info dialog + open RELEASES_URL
     ├─ mgr = velopack.UpdateManager(update_feed_url())
     ├─ info = mgr.check_for_updates()          ← None means up to date
     ├─ no info? → "Up to date (vX.Y.Z)"
     └─ info? → consent dialog → App._velopack_apply(mgr, info)
                  ├─ mgr.download_updates(info)
                  ├─ mgr.apply_updates_and_restart(info)   ← does not return on success
                  └─ any Exception → info dialog + open RELEASES_URL (fail open)

Uninstall (tray "Uninstall…")
 └─ App._uninstall
     ├─ confirm dialog
     ├─ set_run_at_startup(False)               ← clear the Run key
     ├─ history.add("uninstall", ...) + flush
     ├─ is_velopack_installed() and Update.exe exists?
     │     shutil.rmtree(CONFIG_DIR)             ← our data, outside install dir
     │     subprocess.Popen([Update.exe, "uninstall", "--silent"], DETACHED)
     │  else (source/portable-dev):
     │     shutil.rmtree(CONFIG_DIR)             ← nothing installed to remove
     └─ App._quit()

Add/Remove Programs uninstall
 └─ Velopack self-registers the ARP entry at install time and points its
    Uninstall button at the SAME Update.exe uninstall entry point — no
    MicGuard code runs for that path (unlike the old ARP flow, which called
    into micguard.py's own uninstall_from_arp).

Autostart
 └─ set_run_at_startup(enabled)
     enabled=True: target = installed_exe_stub() if is_velopack_installed()
                    else launch_command()  (dev: pythonw + script path)
     Never sys.executable — that resolves inside current\, which Velopack
     replaces whole on every update.
```

## Identity table (IMMUTABLE — see AI-Development-Guide.md)

| Value | Current | Constant / where |
|---|---|---|
| packId | `com.cbuzi.micguard` | `PACK_ID` |
| Pack title | `MicGuard` | `PACK_TITLE` (= `APP_NAME`) |
| Pack authors | `Bristopher` | `PACK_AUTHORS` |
| Update feed URL | `https://github.com/Bristopher/MicGuard/releases/latest/download/` | `UPDATE_FEED_URL` (derived from `GITHUB_REPO`) — GitHub rewrites `releases/latest/download/<asset>` to the newest release's asset, so the feed URL itself never has to change per-release |
| Install root | `%LOCALAPPDATA%\com.cbuzi.micguard\` | `VELOPACK_DIR` |
| mainExe | `MicGuard.exe` | `vpk pack --mainExe MicGuard.exe`; `installed_exe_stub()` builds `VELOPACK_DIR\MicGuard.exe` |
| Config dir (unchanged, deliberately outside install root) | `%APPDATA%\MicGuard\` | `CONFIG_DIR` |

## Features

### Implemented
- ✅ Velopack identity constants (`PACK_ID`, `PACK_TITLE`, `PACK_AUTHORS`,
  `UPDATE_FEED_URL`, `VELOPACK_DIR`) in `micguard.py`
- ✅ Pure helpers: `update_feed_url()`, `installed_exe_stub()`,
  `is_velopack_installed()` — all pytest-covered (`VelopackHelpersTests`)
- ✅ `main()` runs `velopack.App().on_restarted(_velopack_on_restarted).run()`
  first, before the single-instance mutex
- ✅ Old `--updated` relaunch machinery removed (`_process_snapshot`,
  `_cmdline_of`, `webview_teardown_pending`, `wait_webview_teardown`,
  `cleanup_old_exe`)
- ✅ `App._update_check` / Settings update row rewired to
  `velopack.UpdateManager` → consent dialog → `App._velopack_apply`
  (`download_updates` + `apply_updates_and_restart`), gated by
  `is_velopack_installed()`, fail-open to `RELEASES_URL` on any error
- ✅ Old updater removed: `apply_update`, `fetch_latest_release`,
  `parse_version`
- ✅ `App._uninstall` DEFINED (was a latent `AttributeError` on the v1.11.0
  base — the tray menu referenced it but no implementation existed): consent
  → clear Run key → flush history → `rmtree(CONFIG_DIR)` → `Update.exe
  uninstall --silent` → `_quit`
- ✅ Old self-registered Add/Remove Programs code removed:
  `register_uninstall_entry`, `uninstall_entry_values`,
  `unregister_uninstall_entry`, `uninstall_self`, `uninstall_from_arp`,
  `UNINSTALL_KEY`, `PUBLISHER`, the `--uninstall` CLI branch in `main()`
- ✅ `set_run_at_startup(True)` writes `installed_exe_stub()` when
  Velopack-installed, else `launch_command()` for dev runs
- ✅ `release.ps1` rewritten: `pyinstaller --onedir` → `vpk pack` → rename
  Setup/Portable exes → commit/tag/push → `gh release create` with the
  Setup/Portable exes + feed files

### Planned
- 🔜 None currently — this closes out the v2.0.0 Velopack migration. Any
  future packaging change (code signing, MSIX, etc.) gets its own design doc.

## Design Philosophy / Ideology

- **Own less, delegate more.** Windows already has a real installer/updater
  ecosystem (Velopack, itself built on Squirrel-style delta packages); the
  v1.x hand-rolled rename-swap trampoline existed only because there was no
  installer in the first place. Once there's a real install dir, delta
  updates and Add/Remove uninstall are Velopack's job, not MicGuard's.
- **Consent-based updates are non-negotiable and didn't change.** Velopack
  changes HOW an update is fetched/applied, never WHETHER the user is asked
  first — `App._velopack_apply` is called only after the same yes/no dialog
  pattern used since v1.1.0 (see System-Conventions "User-consent
  convention").
- **Config survives everything.** `%APPDATA%\MicGuard` was already outside
  the old install location; it stays outside the new Velopack install root
  too, so an update, an uninstall+reinstall, or even switching from the old
  onefile exe to the new Velopack install never touches a user's settings,
  log, or history.
- **One uninstall implementation, not two.** The old ARP path
  (`uninstall_from_arp`) and the tray path (`_uninstall`) were two separate
  code paths doing almost the same thing. v2.0 collapses this to one:
  Velopack's own `Update.exe uninstall` is the actual removal mechanism for
  BOTH triggers (ARP calls it directly; the tray handler shells out to it
  after doing MicGuard's own consent + config cleanup).

## API / Interface Reference

```python
PACK_ID: str                       # "com.cbuzi.micguard" — IMMUTABLE
PACK_TITLE: str                    # "MicGuard"
PACK_AUTHORS: str                  # "Bristopher"
UPDATE_FEED_URL: str               # f"https://github.com/{GITHUB_REPO}/releases/latest/download/"
VELOPACK_DIR: str                  # %LOCALAPPDATA%\com.cbuzi.micguard

def update_feed_url() -> str: ...
def installed_exe_stub() -> str: ...      # VELOPACK_DIR\MicGuard.exe
def is_velopack_installed() -> bool: ...  # sys.executable inside VELOPACK_DIR?

class App:
    def _velopack_apply(self, mgr: velopack.UpdateManager,
                         info: velopack.UpdateInfo) -> bool: ...
    def _update_check(self, quiet: bool) -> str: ...
    def _uninstall(self, icon=None, item=None) -> None: ...

def _velopack_on_restarted(*_a) -> None: ...   # velopack.App().on_restarted callback

def main() -> None: ...   # runs velopack.App().run() before already_running()
```

## Configuration

No new `DEFAULT_CONFIG` keys — this feature is packaging/distribution, not a
user-facing setting. The existing `check_updates` and `run_at_startup` config
flags are unchanged in meaning; only the mechanism behind "check" and
"startup target" changed.

## Testing

**Automated (pytest, `uv run pytest -q` — 133 tests, down from 141
pre-migration since the old rename-swap-updater tests were removed along with
the code they covered):** `VelopackHelpersTests` covers the pure helpers
(`update_feed_url`, `installed_exe_stub`, `is_velopack_installed` across
frozen/not-frozen and inside/outside-install-dir cases). `uv run python -c
"import micguard"` stays clean throughout.

**Not automatable — needs the real Velopack toolchain (a built installer, a
published feed) and can only be exercised by a live pass:** see Verification
Backlog [§20](../Verify/2026_07-12_Verification-Backlog.md#20-velopack-installupdateuninstall-migration-v20-2026-07-23)
for the full manual matrix — build (`vpk pack` succeeds), install (Setup.exe
→ Start Menu/Search entry, launches, tray/enforcement/mixer/settings all
work), config preservation, in-app update (consent → delta download →
relaunch → first-paint), uninstall via both Add/Remove and the tray, autostart
across a reboot, the portable bundle, and the pre-v2.0-install migration path.

## Troubleshooting

- **`vpk` not found when running `release.ps1`** — needs the .NET SDK
  (`dotnet`) installed; the script auto-installs `vpk` via `dotnet tool
  install -g vpk` on first use and may need a fresh shell to pick up the PATH
  change.
- **"Check for updates" always fails open to the releases page** — check
  `is_velopack_installed()`; it's `False` (by design) for source runs and the
  raw Portable exe, both of which have no install dir to update in place.
- **An existing v1.11.x (or earlier) install never sees an update offered** —
  expected; the old urllib-based updater predates Velopack and can't discover
  or apply a Velopack-packaged release. That install needs a one-time manual
  `Setup.exe` download (see [RELEASING.md](../../RELEASING.md) "How users get
  it"); after that, in-app updates work normally.
- **First window is black/grey right after an update-triggered restart** —
  should be covered by `_velopack_on_restarted`'s ~1 s settle sleep (WebView2
  helper-process teardown from the old process instance); if it recurs,
  that's the thing to widen or replace with a poll (see the pre-v2.0
  WebView2-teardown-poll precedent this pattern is modeled on).
- **`velopack.__version__` reports `0.0.0`** even though `uv`/pip report the
  installed package as `1.2.0` — cosmetic only, the actual API surface used
  (`App`, `UpdateManager`) works correctly; don't mistake it for a broken
  install.
- **Uninstall via tray doesn't remove everything** — `Update.exe uninstall
  --silent`'s exact behavior (silent flag semantics) hasn't been confirmed by
  a live run yet; see Verification Backlog §20's uninstall item.

## References

- Design spec: [superpowers/specs/2026-07-23-velopack-installer-migration-design.md](../superpowers/specs/2026-07-23-velopack-installer-migration-design.md)
- Implementation plan: [superpowers/plans/2026-07-23-velopack-installer-migration.md](../superpowers/plans/2026-07-23-velopack-installer-migration.md) (6 tasks)
- Implementation commits: `b5c8fca` (identity + helpers) → `11a2674` (App
  bootstrap + relaunch removal) → `17b9419` (UpdateManager wiring) →
  `c725722` (install/uninstall/Add-Remove + autostart) → `a65b042`
  (`release.ps1` rewrite)
- [Architecture.md](../Architecture.md) — stack table, distribution/update/
  uninstall flow prose
- [System-Conventions.md](../System-Conventions.md) — "Velopack
  install/update/uninstall/autostart" registered system
- [AI-Development-Guide.md](../AI-Development-Guide.md) — Rule 1, IMMUTABLE
  IDENTITY table
- [RELEASING.md](../../RELEASING.md) — the `vpk pack` release flow, one-time
  pre-v2.0 migration note
- [Verify/2026_07-12_Verification-Backlog.md](../Verify/2026_07-12_Verification-Backlog.md) §20 — manual verify matrix
