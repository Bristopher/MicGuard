# MicGuard — Architecture

**Status:** ✅ Current — describes the shipped v1.8.0 app
**Last Updated:** 2026-07-17

## Overview

MicGuard is a single-process Windows tray app that pins default audio
devices and their volumes. Windows and games (Black Ops 3 was the original
offender) silently change both; MicGuard subscribes to Core Audio change
events and re-asserts the configured state within ~50 ms (measured). It is
deliberately tiny: **one source file (`micguard.py`, ~3,600 lines as of
v1.8), stdlib + 5 runtime deps, compiled to a single unsigned exe** that
friends run with zero setup. There is no server, no database, no installer,
no Task Scheduler — a JSON config, a log file, and one `HKCU\...\Run`
registry value.

As of v1.5 (design: [superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md](superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md);
plan: [superpowers/plans/2026-07-13-device-priority-profiles-hotkeys.md](superpowers/plans/2026-07-13-device-priority-profiles-hotkeys.md))
the app enforces TWO flows — capture (mics) and render (outputs) — each an
ordered priority/fallback list with per-device volume, grouped into named
profiles, plus optional global volume hotkeys with a game-safe on-screen
display. Full feature detail: [Features/Device-Priority-Profiles-Hotkeys.md](Features/Device-Priority-Profiles-Hotkeys.md).

Since then: **v1.6** added the gkey-style mixer popup with boost-past-100%
ducking and the active-window hotkey target; **v1.7** added mixer nav modes,
the all-sessions rolodex, live level pulse, and M mute (same feature doc);
**v1.8** added the optional [Mic EQ extension](Features/Mic-EQ-Extension.md)
(Equalizer APO), same-monitor fullscreen popup placement with per-exe
auto-learn (spec: [superpowers/specs/2026-07-16-same-monitor-autolearn-design.md](superpowers/specs/2026-07-16-same-monitor-autolearn-design.md)),
WASD mixer nav, and the stale-device-ID self-heal described below.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.13 (`requires-python >=3.11`) | What the repo grew up in; `dict \| dict` merge and `str \| None` syntax are used, hence the 3.11 floor |
| Audio API | **pycaw** + **comtypes** | The only maintained Python bindings to Windows Core Audio (`IMMDeviceEnumerator`, `IAudioEndpointVolume`, event callbacks). Replaced the old `nircmd.exe` shell-outs — direct COM calls are ~instant and give us *callbacks* instead of polling |
| Set-default-device | `IPolicyConfig` COM interface, hand-declared in `micguard.py` | Windows has **no public API** to set the default audio device. This undocumented-but-stable-since-Win7 interface is what SoundSwitch/EarTrumpet use. Only `SetDefaultEndpoint` is called; earlier vtable slots are placeholder `COMMETHOD`s (slot *count* must stay exact — see Gotchas) |
| Tray icon | **pystray** (+ **Pillow** to draw the shield-mic glyph) | De-facto standard, ctypes-based on Windows (no pywin32 dependency). Runs detached. Native Win32 tray menus can't be themed, so `_patch_tray_clicks` swaps pystray's `WM_NOTIFY` handler: left-click → Settings, right-click → the themed `MENU_HTML` webview menu at the cursor (auto-hides on blur). The native pystray menu definition stays as the fallback if the patch ever breaks on a pystray update |
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
├── tests/test_micguard.py  # pytest suite for the pure logic (uv run pytest -q)
├── pyproject.toml       # uv project; version is MIRRORED here by release.ps1
├── release.ps1          # one-command release: bump → build → tag → gh release
├── install-test.ps1     # TEST-BUILD path: build → install over %LOCALAPPDATA% → smoke
├── RELEASING.md         # how to ship a version (root, next to the script)
├── README.md            # user-facing install/what-it-does
├── assets/              # icon + the README settings screenshot
├── Docs/                # this docs tree (index: Auto-set-default-Microphone-vol-Main-Doc-Index.md)
├── dist/MicGuard.exe    # build output (gitignored; uploaded to GitHub Releases)
├── Releases/vX.Y.Z/     # versioned archive of every shipped exe (gitignored; written by release.ps1)
└── .venv/               # uv-managed (gitignored)
```

Runtime footprint on a user's machine:

| Path | What |
|---|---|
| `%APPDATA%\MicGuard\config.json` | settings (see [Dynamic-Settings.md](Dynamic-Settings.md)) |
| `%APPDATA%\MicGuard\micguard.log` | INFO-level log; the only debugging surface on a friend's PC |
| `%LOCALAPPDATA%\Programs\MicGuard\MicGuard.exe` | suggested install location (any path works) |
| `HKCU\...\Run\MicGuard` | startup entry, only when the setting is on |

## Threads table (v1.8)

| Thread | Started by | Lifetime | Owns COM? | Purpose |
|---|---|---|---|---|
| main | `main()` | app lifetime | no (until webview.start) | mutex check, `App()` construction, `app.run()` |
| `Enforcer` (`enforcer`) | `App.run()` | app lifetime, daemon | yes — the only long-lived COM owner | registers `_DeviceCallback` + per-flow `_VolumeCallback`s, drains `wake` queue, re-asserts capture + render priority lists |
| `HotkeyManager` (`hotkeys`) | `App.run()`/`_restart_hotkeys` iff `cfg["hotkeys"]["enabled"]` | while enabled; replaced (not reused) on rebind/toggle | yes — CoInitialize at thread start, short-lived pointers released before CoUninitialize | `RegisterHotKey` + blocking `GetMessageW` loop; `WM_HOTKEY` → adjust system or per-app session volume → `App.show_osd` |
| webview main thread | `app.run()` | app lifetime | no | `webview.start(func=self._prime_windows)` owns the GUI message loop; hidden master window keeps it alive |
| webview worker threads | pywebview, per window | per `js_api` call | defensive CoInitialize | settings/menu/dialog `js_api` handlers |
| meter pump | `App._start_meter` | while settings window open | defensive CoInitialize | 20 Hz `IAudioMeterInformation.GetPeakValue()` → level bar |
| `MicMonitor` ("hear yourself") | `App._set_monitor` | while the switch is on | CoInitialize; releases before CoUninitialize | WASAPI passthrough selected mic → speakers; sets `Enforcer.hold_volume` |
| Alert / OSD hide timers | `notify_fallback` / `show_osd` | transient `threading.Timer` | none (UI only) | auto-hide the fallback popup (8 s) / hotkey OSD (1.2 s) |
| Mixer idle timer | `App._arm_mixer_timer` (v1.6) | armed while the mixer popup is visible; re-armed on every ephemeral key press | none (UI only) | `threading.Timer(6.0, self._hide_mixer)` auto-closes the popup and releases the ephemeral keys after 6 s of no digit/arrow/Esc activity |
| Mixer meter pump | `App._start_mixer_meters` (v1.7) | while the mixer popup is visible AND `mixer_meters` is on | defensive CoInitialize; nulls COM locals before CoUninitialize | 20 Hz session/endpoint `IAudioMeterInformation` peaks → live level pulse on the bars |
| FSE probe | `App._arm_fse_probe` (v1.8) | ~1.5 s after a popup shows over an exclusive-fullscreen game | none (Win32 only) | watches `IsIconic`/foreground order-of-events; if the popup minimized the game it hides, restores the game, learns the exe into `fse_incompatible`, and relocates future popups |
| Mic EQ startup timer | `App.run` (v1.8) | one-shot `threading.Timer(3.0)` | none (file I/O only) | re-asserts the Equalizer APO include file after the first enforce pass settles (change-only write) |

`micguard.py` sections top-to-bottom: Core Audio plumbing → `HotkeyManager` →
event callbacks → config/registry/update/uninstall helpers (incl.
`migrate_config`) → `Enforcer` → `App` (tray + settings + alert/OSD windows)
→ `main()`.

```
main()                                   [main thread]
 ├─ already_running()?  → exit (named mutex "Local\MicGuardSingleton")
 ├─ App() — first run: autodetect_device() picks the mic that is BOTH
 │          default + default-comms (fallback: multimedia default → first
 │          active capture device), volume prefilled from current level,
 │          written into the "Default" profile's mics list (config v2)
 └─ app.run() → pystray runs DETACHED; HotkeyManager starts if enabled;
     webview.start(func=self._prime_windows) owns the main thread:
     ├─ _prime_windows runs ONCE the GUI loop is live: primes the alert
     │   and OSD singleton windows (see "WebView2 no-activate prime" below)
     ├─ hidden master window keeps webview's loop alive for the app's lifetime
     ├─ settings window pre-created HIDDEN (background_color=#09090b) —
     │   open = evaluate_js("refresh()") + show() → ~30 ms, no white flash;
     │   Cancel/Save/✕ hide() it, never destroy
     ├─ alert + OSD windows: persistent hidden singletons, shown via
     │   `_show_noactivate` (WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
     │   SW_SHOWNOACTIVATE) so games/fullscreen apps keep input focus
     ├─ dialogs = short-lived frameless windows, created from any thread
     └─ quit = destroy every window → start() returns → icon+enforcer+hotkeys stopped

Enforcer (threading.Thread, daemon)      [owns all LONG-LIVED COM objects]
 ├─ comtypes.CoInitialize()
 ├─ registers _DeviceCallback  (IMMNotificationClient: default-device /
 │                              device-state changes, both flows)
 ├─ registers TWO _VolumeCallback instances (IAudioEndpointVolumeCallback),
 │  one per flow, re-attached whenever the enforced device of that flow changes
 └─ loop: wake.get(timeout=15)   # 15 s watchdog is the only "polling"
     └─ _enforce():  for each flow (capture, render):
         0. heal_stale_ids(entries, devices) — USB re-enumeration can hand a
            saved device a NEW endpoint ID; when exactly one connected device
            matches a stale entry's exact name and its ID is unclaimed, the
            entry re-adopts the new ID (config saved, logged) — v1.8
         1. pick_device(entries, active_ids) — highest-priority CONNECTED
            device in that flow's priority list, else None
         2. device changed since last pass (availability-driven)?
            → on_fallback(flow, lost_name, new_entry_or_None) → App.notify_fallback
         3. default endpoint != want.id?
            → IPolicyConfig.SetDefaultEndpoint(device_id) for all 3 roles
         4. volume: mics always held (snap-back); outputs held only if that
            device's `hold_volume` flag is set, else set ONCE at switch time
            so the system volume keys/mixer stay usable
         5. muted (capture only)? → unmute

Callbacks (arrive on arbitrary COM threads)
 └─ do NOTHING but wake.put("volume"|"default"|"state")   ← hard rule

UI (webview)
 ├─ js_api calls (get_state/save) arrive on webview WORKER threads →
 │   they CoInitialize defensively before touching Core Audio
 └─ update check / uninstall run in their own threads and block on
     App._dialog (threading.Event answered by the dialog's js_api)

HotkeyManager (threading.Thread, daemon) [own Win32 message loop, no polling]
 ├─ comtypes.CoInitialize(); RegisterHotKey(None, id, mods, vk) per enabled
 │  binding (plain mods, no MOD_NOREPEAT, so holding the combo auto-repeats)
 ├─ blocking GetMessageW loop — zero idle cost; WM_HOTKEY → _fire(binding)
 │   ├─ target "system": adjusts the default render endpoint's volume
 │   ├─ target "active": adjusts the foreground window's process session
 │   ├─ target "app:<exe>": pycaw AudioUtilities.GetAllSessions(), matches
 │   │   process name case-insensitively, adjusts SimpleAudioVolume.MasterVolume
 │   │   ("active"/"app:<exe>" both route through boosted_nudge — see below)
 │   └─ target "mixer": App.toggle_mixer() — shows/hides the mixer popup;
 │       carries no step (step is always 0 for mixer bindings)
 ├─ every fire calls App.show_osd(label, percent) — never raises even if the
 │   OSD window itself fails (hotkeys must keep working)
 ├─ mixer ephemeral keys (v1.6): WHILE the popup is visible, App posts
 │   WM_APP_MIXER_ON/OFF (0x8001/0x8002) into THIS thread's queue — the ONLY
 │   way another thread can add/remove RegisterHotKey registrations, since
 │   RegisterHotKey is per-thread. `_register_mixer_keys`/`_unregister_mixer_keys`
 │   run exclusively on the manager thread's message loop; a bare-key id
 │   >= 100 (digits 1-9 = ids 100-108, up/down = 109/110, Esc = 111) routes to
 │   `App._mixer_key` instead of `_fire`. `_register_mixer_keys` is a no-op if
 │   `_mixer_ids` is already populated — guards a double WM_APP_MIXER_ON from
 │   orphaning a second set of registrations that never gets unregistered.
 │   `set_mixer_keys(on)` is the thread-safe entry point every other thread
 │   calls; it just PostThreadMessageW's the request, never touches
 │   RegisterHotKey directly.
 └─ shutdown(): waits for `_ready` (thread may not have registered yet) before
    PostThreadMessageW(WM_QUIT) — posting into an unregistered thread would
    hang forever; App._restart_hotkeys replaces the whole instance on
    enable/rebind rather than mutating a running one (a live mixer popup is
    hidden first, releasing its ephemeral keys, before the old manager dies)

Settings-scoped live audio (exist ONLY while the settings window is visible)
 ├─ meter pump thread: polls IAudioMeterInformation.GetPeakValue() at 20 Hz
 │   and evaluate_js's the level bar; started by open_settings, stopped by
 │   App._settings_closing (the Cancel/Save/✕ path)
 └─ MicMonitor thread ("hear yourself"): WASAPI shared-mode passthrough,
     selected mic → default speakers (IAudioClient + hand-declared
     IAudioCaptureClient/IAudioRenderClient, AUTOCONVERTPCM flags so one
     format fits both ends). While it runs, Enforcer.hold_volume=True lets
     the user drag the volume live without enforcement snapping it back;
     stopping the monitor clears the hold and pokes the enforcer. In-app by
     design: Windows' own "Listen to this device" is never touched, so
     closing settings can't clobber a listen the user enabled elsewhere.
```

**The enforcement loop in words:** anything that touches either flow fires a
COM callback → the callback drops a token on `Enforcer.wake` (a
`queue.Queue`) → the enforcer drains the burst and, per flow, re-asserts
device + volume + mute, and goes back to sleep. Our own corrective
`SetMasterVolumeLevelScalar` fires the callback again, but the next
`_enforce` pass is a no-op because the value now matches — that's the
recursion guard. A 15-second `queue.get` timeout doubles as a watchdog pass
for any missed event; that one COM read (times two flows) is the app's
entire idle cost.

**Two volume listeners, not one:** `Enforcer._volume_coms`/`_volume_cbs` are
now keyed by flow (`"capture"`/`"render"`). `_attach_volume_listener(key, id)`
unregisters the old callback and registers a fresh one whenever the enforced
device for that flow changes (profile switch, fallback, recovery) — the
render listener only fires drift-restore when that output's `hold_volume` is
on; otherwise its volume is set once at switch time and left alone so the
user's volume keys/mixer work normally.

**Config v2 + the permanent adapter:** `DEFAULT_CONFIG` now nests `profiles`
(each `{name, mics: [{id,name,volume}], outputs: [{id,name,volume,hold_volume}]}`),
`active_profile`, `hotkeys` (master switch + binding list), plus the existing
`enforce`/`notify_fallback`/`run_at_startup`/`check_updates` flags. Old
v1 configs (`device_id`/`device_name`/`volume` at the root) are converted by
`migrate_config(raw)` inside `load_config()`, BEFORE the `DEFAULT_CONFIG |
raw` merge — this is a permanent, deliberate exception to "dict-merge is the
whole migration system" (see [Dynamic-Settings.md](Dynamic-Settings.md)).
The migration is idempotent and only touches the file on disk when
`save_config()` next runs (e.g. any settings Save) — an unmodified v1.4
install stays v1 shape on disk until then, which is correct and expected.

**Update flow (user-consented, never silent):** on launch (if
`check_updates`) and via tray → *Check for updates*: fetch
`releases/latest` → newer tag? → **yes/no dialog**. Accept → download the
`.exe` asset, then **rename-swap**: rename the running exe to `.old`
(Windows allows renaming a running image, just not overwriting it), move the
new exe into place, spawn it with `--updated` (which makes `already_running`
wait up to 15 s for the old process's mutex), quit; the new process deletes
the `.old`. NEVER go back to a copy-over-the-exe trampoline bat — that
raced the PyInstaller onefile bootstrap and produced "Failed to load Python
DLL ..._MEI..." on the first real user update (2026-07-12). Any failure →
info dialog with the releases URL + `webbrowser.open` (a failed swap rolls
the rename back). Version comparison is `parse_version` on
`VERSION = "x.y.z"` — the single source of truth that `release.ps1` bumps.

**Uninstall flow:** tray → *Uninstall…* → confirm dialog → delete Run key +
`%APPDATA%\MicGuard` → trampoline bat deletes the exe after exit.

**Popups vs exclusive-fullscreen games (v1.7 → v1.8):** every no-activate
popup (mixer, OSD, fallback alert) places itself via
`popup_monitor_rect(cfg)` / `pick_popup_monitor(...)`. Default mode `auto`
tries the game's own monitor first (Win11 Fullscreen Optimizations tolerate
overlays even in "exclusive" mode); a ~1.5 s FSE probe then watches whether
the game got minimized by the popup — if so it restores the game, learns the
exe into `cfg["fse_incompatible"]`, and that game's popups relocate to a
game-free monitor from then on. Modes `other`/`off` skip the attempt.
Injection/z-band overlays were researched and rejected (anti-cheat ban risk)
— see [Future/Same-Monitor-Overlay-Exclusive-Fullscreen.md](Future/Same-Monitor-Overlay-Exclusive-Fullscreen.md).

**Mic EQ extension (v1.8, optional):** an always-visible 3-state settings
card integrates Equalizer APO — per-profile mic gain (past the driver's max)
and bass boost, written to a `MicGuard-Mic.txt` include file whose `Device:`
line follows the enforced mic (including fallbacks). Setup is guided and
consent-first; nothing is installed silently. Full detail:
[Features/Mic-EQ-Extension.md](Features/Mic-EQ-Extension.md).

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
- **Release COM pointers on their own thread BEFORE CoUninitialize.** Short-
  lived audio threads (MicMonitor, the meter pump) null out every COM local
  and `gc.collect()` before CoUninitialize — letting the GC Release them
  later, from another thread, is an access-violation crash (found 2026-07-12
  while building hear-yourself).
- **Never name a Thread attribute `_stop`** — it shadows
  `threading.Thread._stop()` and breaks `join()`/`is_alive()` with
  `'Event' object is not callable`. Stop events are `_stop_evt`.
- **The tray menu's blur-to-close has a 0.5 s grace window** — after a tray
  click the taskbar reclaims foreground for a beat; treating that first blur
  as "clicked away" made the menu flash and vanish (user-reported v1.3.2).
  `_blur_menu` re-asserts SetForegroundWindow inside the grace, hides after.
  The menu is also positioned from its REAL GetWindowRect size — pywebview
  frameless windows come out smaller than the requested width/height.
- **`background_color="#09090b"` on every create_window** — without it
  WebView2 flashes white before first paint (user-reported).
- **`DEFAULT_CONFIG | json.load(f)`** in `load_config` is the config migration
  mechanism: new keys added to `DEFAULT_CONFIG` just work for old installs.
- The exe is **unsigned** → SmartScreen warning on friends' PCs (documented in
  README). Signing is a known open gap.
- **WebView2 no-activate windows composite solid black without a "prime."** A
  window shown ONLY via the `SW_SHOWNOACTIVATE` path in `_show_noactivate`
  (the alert popup, the hotkey OSD) never gets its first real paint from
  WebView2's swapchain — it stays black even after the HTML has loaded. Fix:
  `App._prime_window` runs one normal (activating) show → 150 ms → hide cycle
  BEFORE the window is ever shown no-activate, and it must wait on
  `win.events.loaded` first — priming before the page has loaded doesn't
  count; the swapchain never presents and the window stays black permanently
  even once the page does load (found in the Task 7 fallback-alert harness,
  2026-07-13). Priming happens once, up front, via `webview.start(func=
  self._prime_windows)`, off the hot path of the first real popup; a
  `_alert_primed`/`_osd_primed` flag (reset whenever the window is recreated
  or closed) makes a missed/stale prime self-correct defensively at the next
  `notify_fallback`/`show_osd` call.
- **`RegisterHotKey` swallows the combo system-wide** — while MicGuard holds
  e.g. Ctrl+Up, no other app on the PC can bind or receive that combo, and if
  another app already owns it registration just fails (logged, not fatal).
  This is exactly why hotkeys ship with `hotkeys.enabled = False` by default:
  the feature is opt-in, not a silent global keyboard grab.
- **Ephemeral RegisterHotKey while a popup is open — register/unregister ONLY
  on the manager thread via WM_APP posts; guard double-ON.** The mixer's
  bare-key bindings (digits/arrows/Esc) can only be registered on the
  `HotkeyManager` thread that owns them (RegisterHotKey is per-thread). Any
  other thread (App, on show/hide) must go through
  `HotkeyManager.set_mixer_keys(on)`, which posts `WM_APP_MIXER_ON`/`_OFF`
  into that thread's message loop rather than calling `RegisterHotKey`
  directly. `_register_mixer_keys` also short-circuits if `_mixer_ids` is
  already non-empty — without that guard, two ON posts in a row (e.g. a fast
  reopen) would register the same ids twice and only the first set would ever
  get unregistered, permanently swallowing those keys system-wide.
- **`MonitorFromPoint`'s return type must be declared `HMONITOR`, not left at
  ctypes' default `c_int`.** `HMONITOR` is a pointer-sized handle; on x64 the
  default 32-bit-int restype silently truncates it, so `GetMonitorInfoW`
  either fails or returns the wrong monitor's geometry — the mixer popup
  placed itself on the wrong screen in multi-monitor testing until
  `u.MonitorFromPoint.restype = ctypes.wintypes.HMONITOR` was set explicitly
  (found 2026-07-13 building the mixer's cursor-monitor placement).
- **`HotkeyManager.shutdown()` must wait for `_ready` before posting
  `WM_QUIT`.** A freshly-started manager may not have called `RegisterHotKey`
  /entered `GetMessageW` yet; posting `WM_QUIT` to a thread ID that hasn't
  reached the message loop is silently dropped, and the old thread then
  blocks in `GetMessageW` forever — it never exits and the rebind path leaks
  a thread holding the old hotkey registrations. `self._ready.wait(timeout=1)`
  before `PostThreadMessageW` fixes it (bit the settings rebind path in
  testing, 2026-07-13).

## Honest gaps

- **No ruff config yet** — the standing Astral-toolchain preference applies,
  but `[tool.ruff]` hasn't been added to `pyproject.toml`.
- **Test coverage is pure-function only.** `tests/test_micguard.py`
  (pytest, `uv run pytest -q`, 88 tests as of v1.8) covers the COM-free and
  hardware-free logic: `migrate_config`, `active_profile_lists`,
  `pick_device`, `heal_stale_ids`, `parse_hotkey`, the mixer nav/viewport/
  boost math (`mixer_key_action` incl. WASD), the EQ renderer/writer cores,
  and `pick_popup_monitor`. Core Audio behavior (enforcement, fallback,
  hotkey firing, the OSD/alert windows) still has no automated coverage;
  it's verified live via the manual smoke commands in
  [AI-Development-Guide.md](AI-Development-Guide.md) §6 plus the human
  backlog in
  [Verify/2026_07-12_Verification-Backlog.md](Verify/2026_07-12_Verification-Backlog.md).
  A fake-`AudioUtilities` seam is the natural next step if COM-level tests are
  ever added.
- The GitHub repo starts at the v1.0.0 rewrite (`4bda0ee`); anything older is
  untracked local-only history.
