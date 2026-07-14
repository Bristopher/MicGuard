# MicGuard v1.6 — Shift+F2 mixer popup, active-window volume, boost-by-ducking

**Date:** 2026-07-14
**Status:** Approved design (brainstormed with Bristopher)
**Baseline:** v1.5 on `main` (02cb1fb): profiles, priority lists, HotkeyManager
(RegisterHotKey, system + `app:<exe>` targets), OSD, no-activate window prime
pattern (`_prime_window` / `_show_noactivate`).

## Problem / intent

While gaming, Bristopher wants to (a) push Discord "past 100%", (b) nudge the
volume of whatever window is focused, and (c) see and control all of it from
a popup like his Gkey Mover v2 overlay (Shift+F1 — which is why that combo
was unregistrable; MicGuard uses **Shift+F2**). Also: the volume OSD renders
with a dead strip at the bottom (frameless real-rect quirk) — fix it.

## Decisions (user-confirmed)

1. **">100%" = headroom trick, duck the game.** Windows session volume caps
   at 100%; no external API reaches Discord's internal 200% gain. Chosen:
   per-app **boost** (0..+50, step-driven). Boosting an app already at 100%
   lowers the *detected game's* session by the same amount, visualized in
   amber ("ducked −N%"). No game detected → lower every other audio session
   except the boosted app (per-session; absolute system volume untouched).
   Nudging down unwinds boost (restores ducked sessions) before lowering the
   app itself. Boost is transient: reset when the boosted app's session
   vanishes or MicGuard exits. Never persisted to config.
2. **New hotkey target `active`** — the foreground window's process. Label
   everywhere as `Active window (<exe>)`. No session → no-op with a dim OSD
   note ("no audio").
3. **Mixer popup = gkey-style digit menu** (studied from Gkey Mover v2
   source — `OverlayApp.tsx`, `overlay.rs`, `hotkeys.rs`):
   - `shift+f2` TOGGLES it (new default binding, target `"mixer"`, ships in
     `DEFAULT_CONFIG["hotkeys"]["bindings"]`; master switch still gates all
     hotkeys; rebindable in settings like any row).
   - Bottom-center of the **cursor's monitor**, ~80 px above the bottom edge,
     recomputed at every open. No-activate + primed (existing pattern).
   - Rows with **number badges**: 1 System · 2..N one per DISTINCT
     `app:<exe>` in the user's bindings (Discord by default) · last row
     Active window (exe). Each row: badge, name, live bar, %, dim chip with
     the row's bound combo (if any).
   - While visible, EPHEMERAL global hotkeys: digits 1–9 select a row,
     ↑/↓ nudge the selected row ±2 (plain mods → hold repeats), Esc closes.
     Registered on show, unregistered on hide — never held while closed.
     Digit registration failures (game holds a key) are logged and skipped,
     never surfaced (gkey convention).
   - Reopen resets selection to row 1. Auto-hide after 6 s idle; any
     interaction resets the timer. Boost zone: bar runs past the 100 mark in
     green; ducked rows show amber "ducked −N%".
   - Honest limit (same as gkey/Discord overlays): invisible over EXCLUSIVE
     fullscreen; volume changes still apply. Borderless/windowed is the
     target environment.
4. **OSD height fix**: size the OSD window to its real content (measure the
   real rect like `open_menu` does, or shrink OSD_H to match the rendered
   content) — no dead strip.

## Architecture

Everything stays in `micguard.py`; all new UI is one persistent no-activate
webview singleton (`MIXER_HTML`) following the window-styling conventions.

### Volume engine additions (module level)

- `get_foreground_exe() -> str | None`: `GetForegroundWindow` →
  `GetWindowThreadProcessId` → exe name via psutil (pycaw dep already ships
  it). None for desktop/own windows.
- `list_app_sessions() -> dict[str, float]`: exe (lower) → current session
  volume 0..1 (max across that exe's sessions).
- `BoostState` (plain class, owned by HotkeyManager's thread but read by the
  mixer render path): `{exe: boost_pct}` plus `ducked: {exe: original_pct}`
  bookkeeping. Methods `nudge(exe, step) -> (shown_pct, ducked_info)` and
  `restore_all()`. `shown_pct = session_pct + boost` (0..150 display scale).
- `adjust_app_volume` grows boost awareness: at 100% and step>0 → increase
  boost + duck; step<0 with boost>0 → unwind first. Duck target = detected
  game exe (foreground fullscreen/borderless with a session, else every
  other session except the boosted exe).
- Target `"active"` in `HotkeyManager._fire`: resolve `get_foreground_exe()`
  per press, then behave like `app:<exe>` (incl. boost). OSD label
  `f"Active — {exe}"`.

### Mixer window + controller

- `MIXER_HTML` template: gkey aesthetics adapted to zinc tokens — near-black
  card, 1 px hairline, rounded rows with digit badges, dim mono hint chips,
  footer hint line ("Esc closes · 1–9 pick · ↑↓ adjust"). ~380 px wide,
  height fits row count (real-rect measured).
- `App._make_mixer_window(hidden=True)` singleton + prime via the existing
  `_prime_windows` hook; only `_quit` destroys.
- `App.toggle_mixer()` (called from `_fire` on the `"mixer"` target):
  visible → hide; hidden → build row model (sessions snapshot + boost state
  + foreground exe), `evaluate_js("setMixer(model)")`, position on cursor's
  monitor (`MonitorFromPoint` or fall back to primary bottom-center), show
  no-activate, register ephemeral keys, start the 6 s idle timer.
- Ephemeral keys implementation: a SECOND short-lived RegisterHotKey message
  loop is NOT needed — the running HotkeyManager registers/unregisters the
  extra ids (digits/arrows/Esc) on its own thread via
  `PostThreadMessageW(WM_APP)` requests, keeping the same-thread
  register/unregister rule. Arrow nudges route through the same
  boost-aware adjust path; every event refreshes the mixer rows
  (`evaluate_js`) and resets the idle timer.
- Hide path: unregister ephemeral ids, cancel timer, hide window. Also
  called from `_quit` and when hotkeys get disabled/restarted.

### Config

- `DEFAULT_CONFIG["hotkeys"]["bindings"]` gains
  `{"keys": "shift+f2", "target": "mixer", "step": 0}`. Dict-merge does the
  migration for existing installs ONLY for fresh configs — existing users'
  `bindings` array is user content and is NOT auto-appended; instead the
  settings Hotkeys section's target dropdown gains "Mixer popup" and
  "Active window" options, and the Feature doc/release notes tell users the
  new defaults. (First-run installs get it automatically.)
- Boost is never persisted.

### Settings integration

Target dropdown options become: `System volume`, `Active window`,
`Mixer popup (toggle)`, plus running sessions as today. Step field disabled
when target is `mixer`.

## Error handling

- All new Win32/COM lookups: try/except, log + degrade (no-op nudge, "no
  audio" OSD note). Foreground exe resolution failures → target skipped.
- Ephemeral key collisions: log + skip that key (menu still mouse-free
  usable via remaining keys; digits are conveniences, arrows are the core).
- Boost bookkeeping restores ducked sessions on: boost unwind, boosted
  session vanishing (checked on each nudge/open), `_quit`, hotkey restart.
- Mixer render failures never block the volume change (same rule as OSD).

## Testing

- pytest additions (pure logic): boost math (nudge up at 100 → boost+duck;
  unwind order; clamp 0..50), row-model builder from a fake session dict,
  `parse_hotkey` unchanged.
- Live harness: toggle mixer via simulated WM_HOTKEY → visible, rows
  rendered, foreground unchanged; digit select + arrow nudge moves a real
  session; boost past 100 ducks the (simulated) game session and restores
  on unwind; idle auto-hide; ephemeral keys released after hide (probe by
  registering them ourselves post-hide).
- OSD height: real-rect equals content height (no dead strip) — screenshot.
- Human (backlog §8): feel in a real borderless game + Discord call.

## Out of scope

True >hardware-max DSP boost (Equalizer APO territory — rejected as
invasive); mixer mouse dragging (digit/arrow only, v1); per-monitor DPI
tuning beyond cursor-monitor placement; typing/free-text in the popup (gkey's
WH_KEYBOARD_LL hook NOT ported — MicGuard has no text entry need).
