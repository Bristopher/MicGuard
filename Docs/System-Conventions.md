# System Conventions

Cross-cutting systems registry — the things multiple features must integrate
with. Starts empty on purpose; it fills itself as the project grows.

## Standing rules
1. **Walk this file BEFORE building** any page/feature and wire into every
   system that applies.
2. **Register AFTER building**: if a feature introduced something other features
   must integrate with (a shared control, hook, app-wide event, data fallback,
   a naming/sign convention), add it here in the SAME change. This is part of
   finishing the feature, not optional.

## Registered systems

| System | The rule | Where |
|---|---|---|
| **Enforcer wake-queue** | ANY "react to X happening" behavior is implemented as a lightweight callback/watcher that calls `wake.put(...)` on the `Enforcer` queue — never a new polling loop, never a new thread doing COM work, never COM calls inside a callback. The Enforcer thread is the single place that owns COM objects and re-asserts state; its 15 s `queue.get` timeout is the app's only sanctioned "poll". | `micguard.py` — `Enforcer.run`/`_enforce`, `_VolumeCallback`, `_DeviceCallback` |
| **Config = DEFAULT_CONFIG merge** | Every setting is a key in `DEFAULT_CONFIG`, persisted only via `save_config()`, surfaced in the settings window, applied live via `enforcer.reattach()`/`poke()`. No env vars, no second file, no registry-stored settings. The `DEFAULT_CONFIG \| file` merge is the migration mechanism. | `micguard.py` — `DEFAULT_CONFIG`, `load_config`/`save_config`; [Dynamic-Settings.md](Dynamic-Settings.md) |
| **Single-source version via release.ps1** | `VERSION` in `micguard.py` is the only authoritative version; `release.ps1` bumps it, mirrors `pyproject.toml`, builds, tags `vX.Y.Z`, and publishes the exe to GitHub Releases. Nothing else may change a version number or create a tag — installed copies compare release tags against `VERSION` to offer updates. | `micguard.py` (`VERSION`), `release.ps1`, [../RELEASING.md](../RELEASING.md) |
| **User-consent convention** | Actions that change the user's machine beyond enforcing their chosen mic state (updating the exe, uninstalling, anything destructive) ask via a topmost dialog (`App._dialog`) and fail open to a manual path (e.g. opening the releases page). Enforcement itself is the product and never asks. | `micguard.py` — `_update_check`, `_uninstall`, `_dialog` |
| **Window styling system** | EVERY window/dialog is a frameless pywebview (WebView2) window built from the HTML templates in `micguard.py`: shared `BASE_CSS` (shadcn/zinc tokens: `#09090b` bg, `#27272a` hairlines, white primary button, `#22c55e` green ONLY for on/active states) + `SETTINGS_HTML`/`DIALOG_HTML`/`MENU_HTML`. New UI = extend those templates; never a tkinter window, never a second UI library, never inline hardcoded colors outside the templates' shared tokens. Rules that ride along: `background_color="#09090b"` on every `create_window` (white-flash guard); yes/no + info prompts go through `App._dialog` (screen-centered); the settings window AND the tray menu are persistent hide/show singletons — only `_quit` destroys windows, because webview.start() returning IS app exit; js_api handlers CoInitialize before Core Audio calls. | `micguard.py` — `BASE_CSS`, `SETTINGS_HTML`, `DIALOG_HTML`, `MENU_HTML`, `_make_settings_window`, `_make_menu_window`, `open_settings`, `open_menu`, `_dialog` |
| **Settings-scoped live audio** | Live feedback in the settings window (the level meter's 20 Hz poll, the MicMonitor "hear yourself" passthrough) may run ONLY while the window is visible and MUST be stopped by `App._settings_closing()` — this is the sanctioned exception to "no polling", scoped to an open UI. Anything that lets the user fiddle with volume live sets `enforcer.hold_volume = True` and clears it (+ `poke()`) when done, so enforcement never fights a preview. The monitor is in-app WASAPI passthrough; never toggle Windows' "Listen to this device" instead. | `micguard.py` — `MicMonitor`, `App._start_meter`/`_stop_meter`/`_set_monitor`/`_settings_closing`, `Enforcer.hold_volume` |
| **Tray interaction contract** | Left-click tray = Settings; right-click tray = the themed `MENU_HTML` menu anchored at the cursor (bottom-left corner at pointer, flips near edges, auto-hides on blur). This is done by `_patch_tray_clicks` replacing pystray's `WM_NOTIFY` handler — new tray actions get a row in `MENU_HTML` + an `Api` method in `_make_menu_window` AND a matching item in the native pystray fallback menu (used only if the patch fails on a future pystray version). | `micguard.py` — `_patch_tray_clicks`, `open_menu`, `MENU_HTML`, `App.run` (fallback menu) |
