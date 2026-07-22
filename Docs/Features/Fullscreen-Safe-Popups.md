# Fullscreen-Safe Popups — no-activate windows + same-monitor auto-learn

**Status:** ✅ Production (v1.6.1 suppression → v1.7 relocation → v1.8.1 same-monitor auto-learn)
**Author:** Bristopher + AI
**Date:** 2026-07-22
**Version:** shipped across v1.6.1–v1.8.1; current as of v1.10.2

---

## Overview

How the Shift+F3 mixer popup, the volume OSD, and the fallback alert appear
*over games* — including titles that claim exclusive fullscreen — without the
game ever losing focus, input, or (worst case) getting minimized. This is a
layered system, not a single trick: no-activate windows for the common case,
exclusive-fullscreen detection for the dangerous case, and a per-game
auto-learn probe that tries the game's own monitor first and remembers which
games can't take it.

Product context: the v1.7 "always use the other monitor" behavior was rejected
("the second monitor is a shitty fallback — I want a better same-monitor
priority integration … explore what you can do that's ban proof"). The answer
is ban-proof by construction: MicGuard only shows/hides/moves ITS OWN windows
and reads public window state (`GetForegroundWindow`, `IsIconic`) — it never
touches the game's process, memory, or rendering.

## Architecture

Four layers, outermost first:

### 1. No-activate windows (`_show_noactivate`)

Every popup is a frameless WebView2 window shown via raw
`SetWindowPos`/`ShowWindow` with `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` +
`SW_SHOWNOACTIVATE` — the game keeps focus and input the entire time. This
alone covers borderless/windowed games, which on Windows 11 is what most
"fullscreen" games effectively are: Fullscreen Optimizations mean DWM
composes topmost windows over them (that's how Game Bar widgets and the
volume flyout work), and `SHQueryUserNotificationState`'s exclusive flag
reports what the game *requested*, not what Windows *granted*.

### 2. The WebView2 priming cycle (`_prime_window` / `_prime_windows`)

A no-activate WebView2 window that has never been shown normally composites
**solid black forever** — WebView2 never produces a frame for a window whose
first show was `SW_SHOWNOACTIVATE`. Fix: one real activating show→hide cycle
per window, run once at GUI-loop start (`webview.start(func=self._prime_windows)`),
and only AFTER the window's page has loaded (`win.events.loaded.wait(...)`) —
priming an unloaded page doesn't count and never self-corrects. Without this
layer, layer 1 renders nothing. (AI-Development-Guide Common Mistake #14.)

### 3. Exclusive-fullscreen detection (`exclusive_fullscreen_active`)

`SHQueryUserNotificationState` == `QUNS_RUNNING_D3D_FULL_SCREEN` (3) means the
foreground app *requested* D3D exclusive fullscreen. That's the one dangerous
case: over a *genuinely* exclusive game, showing ANY window — even no-activate
— breaks exclusivity and Windows minimizes the game (hit live 2026-07-15).
Any failure in the query defaults to False (show), because the common case is
harmless.

### 4. Monitor pick + auto-learn probe (`popup_monitor_rect` → `pick_popup_monitor` → `_arm_fse_probe`)

`popup_monitor_rect(cfg)` gathers the live state (exclusive?, monitor rects,
cursor monitor, game monitor, foreground exe, blacklist) and delegates the
decision to the PURE `pick_popup_monitor(...)` → `(rect | None, tried_same)`:

| Situation | Result |
|---|---|
| Not exclusive | Cursor's monitor (bottom-center), no probe |
| Exclusive, mode `"off"` | `None` — popup suppressed, hotkey nudges still work |
| Exclusive, mode `"auto"`, exe NOT learned | **The game's own monitor**, `tried_same=True` → caller MUST arm the probe |
| Exclusive, mode `"other"` OR exe learned | A game-free monitor (cursor's if different, else first ≠ game's), else `None` |

**The probe** (`_arm_fse_probe`, armed only when `tried_same` was True):
captures the game hwnd+exe *before* showing, then a daemon thread polls
`IsIconic(game_hwnd)` every 100 ms for 1.5 s:

- **Game minimized** → the popup caused it: hide the popup, `SW_RESTORE` the
  game, append the lowercase exe to `cfg["fse_incompatible"]`, `save_config`,
  log the learn. For the mixer, a reshow callback re-runs `_show_mixer` — but
  only after waiting (≤5 s) for exclusive mode to re-engage post-restore,
  because reopening earlier re-picks "not exclusive" → same monitor → a second
  minimize with no probe armed. If exclusive never returns, stay hidden until
  the next hotkey.
- **Focus moved to another app while the game is still up** → the *user* is
  alt-tabbing, not the popup's fault: end the probe, learn nothing. Order of
  events is the only reliable discriminator — a popup-caused minimize goes
  iconic FIRST and hands focus over after; a user switch moves focus while the
  game is still restored.
- **1.5 s clean** → this game tolerates same-monitor overlays; nothing is
  written (tolerance is the default, only failures are recorded).

Cost of the learn: exactly one minimize+auto-restore the first time an
incompatible game meets a popup. Your config has already learned
`rainbowsix.exe` this way.

## Features

### Implemented
- ✅ No-activate show path for mixer, OSD, and fallback alert (v1.5/v1.6)
- ✅ One-time priming cycle at GUI start; defensive re-prime on window recreate
- ✅ Exclusive-fullscreen detection with fail-open default
- ✅ `fullscreen_popups` modes: `auto` (same-monitor + learn) / `other` / `off`
- ✅ Per-exe auto-learn blacklist `fse_incompatible` with alt-tab false-positive guard
- ✅ Mixer reshow-after-learn with exclusive-re-engage wait
- ✅ Settings dropdown "Popups over fullscreen games" (S-synced)

### Planned
- 🔜 Nothing active. Parked alternative if same-monitor ever proves
  insufficient: the Game Bar widget route —
  [Future/Same-Monitor-Overlay-Exclusive-Fullscreen.md](../Future/Same-Monitor-Overlay-Exclusive-Fullscreen.md)
  (injection and z-band approaches are recorded there as rejected: anti-cheat
  risk / privileged API).

## Design Philosophy / Ideology

- **Try, observe, remember** — the exclusive flag lies (requested ≠ granted),
  so treat it as a hint, attempt the best UX, watch for the one concrete
  failure signal (`IsIconic`), and persist only failures per-exe.
- **Ban-proof by construction** — own-window operations and public reads only;
  no hooks, no injection, no game-process access. This was an explicit product
  requirement.
- **Degrade, never punish** — every rung down (same monitor → other monitor →
  hidden) keeps the hotkeys functional; the popup is feedback, not the feature.
- **Pure core, thin shell** — `pick_popup_monitor` is a pure function
  (pytest-covered); all COM/Win32 state-gathering stays in
  `popup_monitor_rect` and the probe.

## API / Interface Reference

```python
exclusive_fullscreen_active() -> bool          # QUNS_RUNNING_D3D_FULL_SCREEN
_enum_monitor_work_rects() -> list[(hmon, (x, y, w, h))]
pick_popup_monitor(exclusive, mode, blacklisted,
                   cursor_mon, game_mon, monitors) -> (rect | None, tried_same)
popup_monitor_rect(cfg) -> (rect | None, tried_same)   # the shell over the pure pick
App._show_noactivate(win, title, x, y)         # WS_EX_NOACTIVATE show path
App._prime_window(win, flag_attr)              # one activating show/hide cycle
App._fse_probe_target() -> (game_hwnd, game_exe) | None
App._arm_fse_probe(game_hwnd, game_exe, hide, reshow=None)
```

Call sites: `_show_mixer` (probe + reshow), `show_osd` and the fallback alert
(probe, no reshow — they stay hidden until their next trigger).

Gotcha that bites here: `MonitorFromPoint`/`MonitorFromWindow` need
`restype = HMONITOR` set explicitly — the default `c_int` restype truncates
handles on x64 (see Architecture Gotchas).

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `fullscreen_popups` | `"auto"` | `auto` = try game's monitor + learn; `other` = always a game-free monitor; `off` = suppress over exclusive fullscreen |
| `fse_incompatible` | `[]` | Learned lowercase exe names that minimize under same-monitor popups; written by the probe via `save_config`; user-clearable |

Both live in `DEFAULT_CONFIG` (dict-merge migration, per
[Dynamic-Settings.md](../Dynamic-Settings.md)); the mode is surfaced as the
"Popups over fullscreen games" dropdown in Settings.

## Testing

- `uv run pytest -q` — `pick_popup_monitor` decision table is covered in
  `tests/test_micguard.py` (all mode × exclusive × blacklist combinations).
- Live: open the Shift+F3 mixer on the desktop (cursor-monitor placement),
  then inside a borderless game (same-monitor, no probe effect), then inside a
  genuinely exclusive title not yet in `fse_incompatible` — expect one
  minimize+restore and a `"<exe> minimizes under same-monitor popups —
  learned"` log line, then other-monitor placement on every later open.
- The probe and priming have NO automated coverage (real games + WebView2
  compositor required) — they stay on the verification backlog (§13).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Popup is a solid black rectangle | The priming cycle didn't run or ran before the page loaded — see `_prime_windows` and Common Mistake #14 |
| Game minimizes when the popup appears, every time | The learn didn't persist (check `fse_incompatible` in config.json) or the game exe changes name per-launch |
| Popup never appears in-game | `fullscreen_popups: "off"`, or the game is exclusive on the ONLY monitor with mode `other` — both are by-design suppression; nudge hotkeys still work |
| Game got blacklisted from an alt-tab coincidence | Remove its exe from `fse_incompatible` in config.json (the alt-tab guard checks focus order, but a minimize within the 1.5 s window that wasn't popup-caused can still false-positive) |
| Popup on the "wrong" monitor on the desktop | Placement follows the CURSOR's monitor by design (gkey-style), not the game/focused window |

## References

- Design spec: [superpowers/specs/2026-07-16-same-monitor-autolearn-design.md](../superpowers/specs/2026-07-16-same-monitor-autolearn-design.md)
- Rejected-options research: [Future/Same-Monitor-Overlay-Exclusive-Fullscreen.md](../Future/Same-Monitor-Overlay-Exclusive-Fullscreen.md)
- Registry rows: [System-Conventions.md](../System-Conventions.md) — "Window styling system" (no-activate + priming) and "Fullscreen-safe popup placement"
- Verification: backlog §13 (same-monitor auto-learn items)
- Windows 11 FSO finding (why "exclusive" usually isn't): memory obs 11932, 2026-07-16
