# MicGuard v1.5 — Device priority lists, profiles, fallback alerts, volume hotkeys

**Date:** 2026-07-13
**Status:** Approved design (brainstormed with Bristopher; all decisions below were his calls)
**Baseline:** v1.4.0 (`micguard.py` single-mic guard + WebView2 UI)

## Problem

A mic disconnecting (USB flake, replug) makes Windows silently pick some other
default mic, and nothing puts things back when the good mic returns. Bristopher
wants: ordered fallbacks for BOTH capture and render devices with per-device
volumes, named profiles bundling those lists, a can't-miss-but-can't-interrupt
alert when fallback happens, and global volume hotkeys with a game-safe OSD.

## Decisions (user-confirmed)

1. **Strict priority order, auto-switch back.** The highest-priority CONNECTED
   device in each list is always enforced as default (all three roles). When a
   higher-priority device reconnects it immediately wins again.
2. **Output devices are a full mirror of input** — ordered list + per-device
   volume — EXCEPT volume hold is **optional per output device**
   (`hold_volume` flag): off = set volume once when the device becomes
   default, then leave it alone (volume keys stay usable); on = mic-style
   snap-back. Mic volumes are always held (that is the product).
3. **Profiles switch via the tray menu** (click to activate, active one
   marked). No hotkey-cycling, no auto-by-app (would need process polling —
   banned by System-Conventions §1).
4. **Fallback alert = themed frameless popup** near the tray, auto-dismisses
   ~8 s, click dismisses sooner, never steals focus. Fires on fallback AND on
   recovery ("back on AT2020 @ 85%"). Config flag `notify_fallback` (default
   true).
5. **Hotkeys ship OFF by default** (master switch). Defaults pre-filled:
   Ctrl+↑/↓ → system output volume ±2%, Ctrl+Shift+↑/↓ → Discord ±2%.
   RegisterHotKey swallows combos system-wide — that's why off-by-default.
6. **OSD popup for hotkey volume changes**: themed, bottom-center, updates in
   place, fades ~1.2 s, created no-activate so fullscreen games keep focus.
   Known limit: exclusive-fullscreen games may draw over it (borderless is
   fine); the volume still changes.

## Config schema (v2)

```jsonc
{
  // v2 — structured profiles. v1 keys (device_id/device_name/volume) are
  // converted by a PERMANENT shape adapter in load_config(): if "profiles"
  // is missing, synthesize [{name: "Default", mics: [old mic @ old volume],
  // outputs: []}]. This is the one sanctioned exception to "dict-merge is
  // the migration"; documented in Dynamic-Settings.md.
  "profiles": [
    {
      "name": "Default",
      "mics":    [ {"id": "...", "name": "Microphone (2- AT2020USB+)", "volume": 85} ],
      "outputs": [ {"id": "...", "name": "Headphones (...)", "volume": 30, "hold_volume": false} ]
    }
  ],
  "active_profile": "Default",
  "enforce": true,              // global switch, covers both flows
  "notify_fallback": true,
  "hotkeys": {
    "enabled": false,
    "bindings": [
      {"keys": "ctrl+up",         "target": "system",          "step":  2},
      {"keys": "ctrl+down",       "target": "system",          "step": -2},
      {"keys": "ctrl+shift+up",   "target": "app:Discord.exe", "step":  2},
      {"keys": "ctrl+shift+down", "target": "app:Discord.exe", "step": -2}
    ]
  },
  "run_at_startup": true,
  "check_updates": true
}
```

`RECOMMENDED_VOLUME` (85) stays the mic default for "Use recommended".

## Components

### 1. Enforcer (generalized)

- `_pick(list, flow) -> entry | None`: first entry whose `id` is in the
  ACTIVE endpoints of that flow (`EnumAudioEndpoints`), else None. Pure
  function on (list, active-ids) — unit-testable without hardware.
- `_enforce()` per flow (capture, render):
  1. `want = _pick(...)`; if None → leave Windows alone, alert once.
  2. default != want.id → `set_default_endpoint(want.id)` (works for render
     too; same IPolicyConfig call) + fallback/recovery alert via
     `app.notify_fallback(...)` when the enforced device CHANGED because of
     availability.
  3. Volume: mics always `SetMasterVolumeLevelScalar(volume)`+unmute;
     outputs only when `hold_volume` (else set once at switch time).
- Volume listeners re-attach to the currently enforced device of each flow
  (two `_VolumeCallback`s instead of one).
- Existing wake-queue/watchdog machinery unchanged; `_DeviceCallback` already
  wakes on device state/default changes for all flows (drop the
  capture-only filter).
- `hold_volume` interplay: `Enforcer.hold_volume` (hear-yourself preview
  suspend) stays and now only suspends the CAPTURE volume assert.

### 2. Profiles

- `App.active_lists()` -> (mics, outputs) of the active profile; single read
  path used by Enforcer and UI.
- Tray menu: "Profiles" section listing each profile name, active one gets
  the green dot treatment; click → `active_profile = name`, save, reattach,
  poke, menu refreshes. Menu height becomes dynamic: after `refreshMenu()`,
  JS reports `document.body.scrollHeight`, Python resizes the window before
  positioning (real-rect anchoring from v1.4.0 already handles placement).
- Settings: profile row at top — dropdown + New / Rename / Delete buttons
  (Delete disabled on the last profile). New copies the current profile.

### 3. Fallback alert popup

- New `ALERT_HTML` template (BASE_CSS tokens), persistent hidden singleton
  window like settings/menu (registered in the window-styling convention).
- Positioned bottom-right above the taskbar; `WS_EX_NOACTIVATE` +
  `SW_SHOWNOACTIVATE` so it never takes focus; auto-hide timer 8 s
  (threading.Timer, reset on re-show); click anywhere dismisses.
- `App.notify_fallback(kind, lost, now)` renders e.g.
  "AT2020 disconnected — now guarding: C920 @ 60%" / "AT2020 reconnected —
  back to it @ 85%". Also logs. Gated on `notify_fallback` config.
- The existing `_notify` tray-toast stays for non-fallback messages.

### 4. Settings UI (reorganized)

Order top→bottom: header · profile row · **Microphones (priority order)**
list · live meter + Hear yourself (target = currently enforced mic) ·
**Headphones / Speakers (priority order)** list · **Hotkeys** section ·
existing switches (Enforce, Start with Windows, Check updates,
Fallback alerts) · footer.

List rows: `▲▼` order buttons · device name · editable volume % (same
digits-only input as v1.3.1) · (outputs) Hold-volume mini toggle · `✕`
remove. **+ Add fallback** appends a row via a dropdown of connected devices
of that flow not already listed. Volume adoption from v1.4.0 applies: a
newly added device's volume prefills with its CURRENT level;
"Use recommended (85%)" link stays on the mic list.

Hotkeys section: master switch + rows (combo capture field — focus it, press
keys, JS records the combo · target dropdown: System / apps with active
audio sessions / free-text exe · step) + **+ Add binding**.

Window: `SET_W` stays 442; content scrolls (`overflow-y: auto` on a content
div) past ~760 px so profiles with many devices never clip.

### 5. Hotkeys + OSD

- `HotkeyManager(threading.Thread)`: own Win32 message loop.
  `RegisterHotKey(None, id, mods, vk)` per enabled binding, `GetMessageW`
  blocks (event-driven, zero idle cost), `WM_HOTKEY` → dispatch:
  - `system`: default render endpoint volume ± step (CoInitialize on this
    thread; short-lived pointers released before CoUninitialize — see
    AI-guide mistake #11).
  - `app:<exe>`: pycaw `AudioUtilities.GetAllSessions()`, match process name
    case-insensitively, adjust every matching session's
    `SimpleAudioVolume.MasterVolume` by step (that IS the sndvol slider).
  - then `App.show_osd(label, percent)`.
  - Rebind/toggle = stop thread (PostThreadMessage WM_QUIT) + start new one.
  - Registration failures (combo taken by another app) log + surface in
    settings row ("in use elsewhere"), never crash.
- OSD: `OSD_HTML` singleton, ~260×64, bottom-center; `WS_EX_NOACTIVATE |
  WS_EX_TOOLWINDOW`, shown with `SW_SHOWNOACTIVATE`; shows target + bar + %;
  hide timer 1.2 s reset on every update; `evaluate_js` updates in place.

## Threading recap (new threads, all following the COM rules)

| Thread | Lifetime | COM |
|---|---|---|
| HotkeyManager | while hotkeys enabled | CoInitialize; short-lived pointers, released before CoUninitialize |
| Alert/OSD hide timers | transient `threading.Timer` | none (UI only) |
| Enforcer / meter / MicMonitor | unchanged | unchanged |

## Error handling

- A device list referencing an id that no longer exists is fine — `_pick`
  skips it; the row shows "(not connected)" in settings.
- Empty mic list in the active profile = MicGuard enforces nothing for that
  flow (and says so in the settings hint + one alert), never crashes.
- Hotkey COM failures degrade to a logged warning; OSD failures never block
  the volume change.
- All new webview windows follow the persistent hide/show rule; only `_quit`
  destroys.

## Testing

- Unit-style (harness scripts, per AI-guide §6): `_pick` priority selection
  incl. missing devices; v1→v2 config adapter (old config in → Default
  profile out, idempotent); hotkey combo→(mods,vk) parsing; per-app session
  match on a synthetic session list.
- Live: fallback simulation by temporarily building a profile whose #1 mic
  id is fake → enforcer must pick #2 and alert; hotkey press → real system/
  Discord volume moves + OSD screenshot; sabotage test still sub-second on
  the enforced mic; OSD focus test (foreground window unchanged after OSD).
- Human (backlog §7): real USB unplug/replug mid-call, exclusive-fullscreen
  OSD visibility, profile switching feel, hotkey capture UX.

## Out of scope (deferred, Docs/Future/ if wanted later)

- Hotkey profile cycling, auto-profiles by running app, mute-toggle
  hotkeys, per-app hotkey OSD mixer panel, communications-role split
  (different comms vs default device).
