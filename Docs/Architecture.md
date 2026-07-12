# MicGuard — Architecture

**Status:** ✅ Current — describes the shipped v1.x app
**Last Updated:** 2026-07-12

## Overview

MicGuard is a single-process Windows tray app that pins the default capture
device and its recording volume. Windows and games (Black Ops 3 was the
original offender) silently change both; MicGuard subscribes to Core Audio
change events and re-asserts the configured state within ~50 ms (measured).
It is deliberately tiny: **one source file (`micguard.py`, ~600 lines), stdlib
+ 4 runtime deps, compiled to a single unsigned exe** that friends run with
zero setup. There is no server, no database, no installer, no Task Scheduler —
a JSON config, a log file, and one `HKCU\...\Run` registry value.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.13 (`requires-python >=3.11`) | What the repo grew up in; `dict \| dict` merge and `str \| None` syntax are used, hence the 3.11 floor |
| Audio API | **pycaw** + **comtypes** | The only maintained Python bindings to Windows Core Audio (`IMMDeviceEnumerator`, `IAudioEndpointVolume`, event callbacks). Replaced the old `nircmd.exe` shell-outs — direct COM calls are ~instant and give us *callbacks* instead of polling |
| Set-default-device | `IPolicyConfig` COM interface, hand-declared in `micguard.py` | Windows has **no public API** to set the default audio device. This undocumented-but-stable-since-Win7 interface is what SoundSwitch/EarTrumpet use. Only `SetDefaultEndpoint` is called; earlier vtable slots are placeholder `COMMETHOD`s (slot *count* must stay exact — see Gotchas) |
| Tray icon | **pystray** (+ **Pillow** to draw the shield-mic glyph) | De-facto standard, ctypes-based on Windows (no pywin32 dependency). Runs detached; left-click opens Settings (`default=True` menu item) |
| Settings/dialog UI | **pywebview** (WebView2) — real HTML/CSS | The user rejected two native-toolkit designs (ttk, then CustomTkinter) — tkinter-family UIs were ruled out outright. pywebview renders frameless windows with actual shadcn/zinc CSS in Windows' built-in WebView2 for a few MB (vs ~200 MB for Electron), no Node/React build step. Templates live IN `micguard.py` (`SETTINGS_HTML`/`DIALOG_HTML`); JS↔Python via `js_api`. Frozen builds REQUIRE `--collect-all webview`; friends' PCs need the WebView2 runtime (ships with Win11/Edge) |
| Config | JSON at `%APPDATA%\MicGuard\config.json` (stdlib `json`) | Human-readable, trivially merged with defaults on load |
| Startup | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` via stdlib `winreg` | Per-user, no admin, no Task Scheduler — an explicit product requirement |
| Updates | GitHub Releases API via stdlib `urllib` | No `requests` dependency for one GET; latest-release tag compared to `VERSION` |
| Packaging | **PyInstaller** `--onefile --noconsole`, driven by **uv** | Single `MicGuard.exe` (~19.6 MB) on the Releases page; `uv` manages the venv/lockfile per the standing tooling rule |

**Deliberate non-picks** (vs [Preferred-Stack.md](Preferred-Stack.md)): no
Pydantic/whenever/Tenacity/icecream. Every dependency inflates the onefile exe
and this app has no timezone math, no schema boundary, and one retry-able
network call. Stdlib-first is the rule here (see AI-Development-Guide Rule 1).
`uv` + the handpicked-libraries rule apply in full.

## Where things live

```
.
├── micguard.py          # THE app — all source lives here on purpose
├── pyproject.toml       # uv project; version is MIRRORED here by release.ps1
├── release.ps1          # one-command release: bump → build → tag → gh release
├── RELEASING.md         # how to ship a version (root, next to the script)
├── README.md            # user-facing install/what-it-does
├── Docs/                # this docs tree (index: Auto-set-default-Microphone-vol-Main-Doc-Index.md)
├── dist/MicGuard.exe    # build output (gitignored; uploaded to GitHub Releases)
├── .myArchive/          # DEAD CODE — the pre-rewrite nircmd/polling scripts
│                        #   (BlackOps3_*.py, Auto-set-default-*.py, nircmd.exe).
│                        #   Gitignored, kept for reference only. Never import from it.
└── .venv/               # uv-managed (gitignored)
```

Runtime footprint on a user's machine:

| Path | What |
|---|---|
| `%APPDATA%\MicGuard\config.json` | settings (see [Dynamic-Settings.md](Dynamic-Settings.md)) |
| `%APPDATA%\MicGuard\micguard.log` | INFO-level log; the only debugging surface on a friend's PC |
| `%LOCALAPPDATA%\Programs\MicGuard\MicGuard.exe` | suggested install location (any path works) |
| `HKCU\...\Run\MicGuard` | startup entry, only when the setting is on |

## Threads & event flow (the whole design)

`micguard.py` sections top-to-bottom: Core Audio plumbing → event callbacks →
config/registry/update/uninstall helpers → `Enforcer` → `App` (tray + settings)
→ `main()`.

```
main()                                   [main thread]
 ├─ already_running()?  → exit (named mutex "Local\MicGuardSingleton")
 ├─ App() — first run: autodetect_device() picks the mic that is BOTH
 │          default + default-comms (fallback: multimedia default → first
 │          active capture device), volume prefilled from current level
 └─ app.run() → pystray runs DETACHED; webview.start() owns the main thread:
     ├─ hidden master window keeps webview's loop alive for the app's lifetime
     ├─ settings window pre-created HIDDEN (background_color=#09090b) —
     │   open = evaluate_js("refresh()") + show() → ~30 ms, no white flash;
     │   Cancel/Save/✕ hide() it, never destroy
     ├─ dialogs = short-lived frameless windows, created from any thread
     └─ quit = destroy every window → start() returns → icon+enforcer stopped

Enforcer (threading.Thread, daemon)      [owns all LONG-LIVED COM objects]
 ├─ comtypes.CoInitialize()
 ├─ registers _DeviceCallback  (IMMNotificationClient: default-device /
 │                              device-state changes)
 ├─ registers _VolumeCallback  (IAudioEndpointVolumeCallback on the target mic)
 └─ loop: wake.get(timeout=15)   # 15 s watchdog is the only "polling"
     └─ _enforce():
         1. any of the 3 roles (console/multimedia/comms) drifted?
            → IPolicyConfig.SetDefaultEndpoint(device_id, each role)
         2. |current − target| > 0.005? → SetMasterVolumeLevelScalar
         3. muted? → unmute

Callbacks (arrive on arbitrary COM threads)
 └─ do NOTHING but wake.put("volume"|"default"|"state")   ← hard rule

UI (webview)
 ├─ js_api calls (get_state/save) arrive on webview WORKER threads →
 │   they CoInitialize defensively before touching Core Audio
 └─ update check / uninstall run in their own threads and block on
     App._dialog (threading.Event answered by the dialog's js_api)
```

**The enforcement loop in words:** anything that touches the mic fires a COM
callback → the callback drops a token on `Enforcer.wake` (a `queue.Queue`) →
the enforcer drains the burst, re-asserts device + volume + mute, and goes
back to sleep. Our own corrective `SetMasterVolumeLevelScalar` fires the
callback again, but the next `_enforce` pass is a no-op because the value now
matches — that's the recursion guard. A 15-second `queue.get` timeout doubles
as a watchdog pass for any missed event; that one COM read is the app's entire
idle cost.

**Update flow (user-consented, never silent):** on launch (if
`check_updates`) and via tray → *Check for updates*: fetch
`releases/latest` → newer tag? → **yes/no dialog**. Accept → download the
`.exe` asset to `%APPDATA%\MicGuard\MicGuard.new.exe`, spawn `update.bat`
(wait-loop → `copy /y` over `sys.executable` → restart → self-delete), quit.
Any failure → info dialog with the releases URL + `webbrowser.open` so the
user can download manually. Version comparison is `parse_version` on
`VERSION = "x.y.z"` — the single source of truth that `release.ps1` bumps.

**Uninstall flow:** tray → *Uninstall…* → confirm dialog → delete Run key +
`%APPDATA%\MicGuard` → trampoline bat deletes the exe after exit.

## Gotchas (each one cost real debugging time)

- **Every thread that touches COM calls `comtypes.CoInitialize()` first.**
  The settings window enumerates devices on its own thread — forgetting this
  produced `WinError -2147221008 CoInitialize has not been called` (fixed
  2026-07-12). The Enforcer owns all long-lived COM objects; nothing else
  holds one across threads.
- **`IPolicyConfig._methods_` slot count is load-bearing.** The placeholder
  `COMMETHOD`s exist only to pad the vtable so `SetDefaultEndpoint` lands at
  slot 11 (after `ResetDeviceFormat` — the Win7+ layout, not the Vista one).
  Add/remove a line and you corrupt the call. Never call the placeholders.
- **Callbacks must not do COM work or block** — they arrive on COM's threads;
  re-entering the audio API there can deadlock. Queue-poke only.
- **PyInstaller onefile shows two `MicGuard.exe` processes** (bootstrap +
  child). Not a bug; `Stop-Process -Name MicGuard` kills both.
- **tkinter-family UIs are banned here by history, not taste alone**: ttk and
  CustomTkinter designs were both user-rejected, and CTk had a real
  thread-race bug (unmapping a window from a background thread before its
  mainloop → permanently invisible window, shipped in v1.2.0's first-run).
  pywebview replaced all of it — don't reintroduce tkinter windows.
- **webview.start() returning IS the quit path** — the app exits when every
  webview window (including the hidden master) is destroyed. Never destroy
  the master or the persistent settings window casually: settings windows
  `hide()`, only `_quit` destroys.
- **js_api handlers run on webview worker threads** — CoInitialize
  defensively (try/except) before any Core Audio call there.
- **`background_color="#09090b"` on every create_window** — without it
  WebView2 flashes white before first paint (user-reported).
- **`DEFAULT_CONFIG | json.load(f)`** in `load_config` is the config migration
  mechanism: new keys added to `DEFAULT_CONFIG` just work for old installs.
- The exe is **unsigned** → SmartScreen warning on friends' PCs (documented in
  README). Signing is a known open gap.

## Honest gaps

- **No test suite.** Verification is the manual smoke commands in
  [AI-Development-Guide.md](AI-Development-Guide.md) §6 plus the human backlog
  in [Verify/2026_07-12_Verification-Backlog.md](Verify/2026_07-12_Verification-Backlog.md).
  Core Audio behavior is hard to unit-test; a fake-`AudioUtilities` seam would
  be the starting point if tests are ever added.
- **No ruff config yet** — the standing Astral-toolchain preference applies,
  but `[tool.ruff]` hasn't been added to `pyproject.toml`.
- `.myArchive/` and `.history/` are untracked local-only history; the GitHub
  repo starts at the v1.0.0 rewrite (`4bda0ee`).
