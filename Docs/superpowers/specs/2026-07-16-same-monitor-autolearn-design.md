# MicGuard v1.8.1 — same-monitor popup priority with auto-learn

**Date:** 2026-07-16
**Status:** ✅ Approved by Bristopher ("Approved — build it"; context: "THE SECOND
MONITOR IS A SHITTY FALLBACK I WANT A BETTER SAME MONITOR PRIORITY INTEGRATION …
explore what you can do thats ban proof")
**Supersedes:** the v1.7 unconditional other-monitor relocation (which itself
superseded v1.6.1's unconditional suppression). The research doc
`Docs/Future/Same-Monitor-Overlay-Exclusive-Fullscreen.md` stays as the
options record; the Game Bar widget remains parked pending this feature's
real-world results.

## The insight

`SHQueryUserNotificationState`'s D3D-exclusive flag reports what the game
REQUESTED, not what Windows granted. On Windows 11, DWM routinely
reverse-composes topmost windows over "exclusive" games (that's how Game Bar
widgets and the volume flyout appear over them), and FSO games only think
they're exclusive. Many titles — likely including R6 Siege — tolerate a
no-activate popup on the game monitor with zero side effects. The only
failure mode is the one we already met in v1.6.1: a genuinely exclusive game
minimizes. So: TRY the same monitor, watch for exactly that failure, recover
automatically, and remember per-exe. Ban-proof by construction: MicGuard only
shows/hides/moves ITS OWN windows and reads public window state (foreground
hwnd, IsIconic) — it never touches the game's process, memory, or rendering.

## Config

- `fullscreen_popups`: `"auto"` (default) | `"other"` | `"off"` — top-level
  key in `DEFAULT_CONFIG` (dict-merge gives it to old configs).
  - `auto`: try the game's monitor first; auto-learn failures (below).
  - `other`: v1.7 behavior — always the game-free monitor.
  - `off`: v1.6.1 behavior — suppress while exclusive fullscreen.
- `fse_incompatible`: `[]` — learned lowercase exe names that minimize when
  overlaid. Written via `save_config` when a probe learns a game. Users can
  clear it by deleting entries (documented in the settings hint).

Settings UI: one dropdown row "Popups over fullscreen games" under the mixer
rows — "Try same monitor (auto-learn)" / "Other monitor" / "Hide" — S-synced
like every control (the C1 rule).

## Monitor pick (pure core)

```python
def pick_popup_monitor(exclusive: bool, mode: str, blacklisted: bool,
                       cursor_mon: int, game_mon: int,
                       monitors: list[tuple[int, rect]]) -> tuple[rect | None, bool]
# returns (rect, tried_same). tried_same=True only when exclusive and the
# rect IS the game's monitor (auto mode, not blacklisted) — the caller must
# arm the probe in exactly that case.
```

- not exclusive → cursor monitor rect (today's behavior), tried_same=False.
- exclusive + mode "off" → (None, False).
- exclusive + (mode "other" OR blacklisted) → first monitor ≠ game_mon
  (cursor monitor preferred when it differs from the game's), else None —
  exactly v1.7's relocation.
- exclusive + mode "auto" + not blacklisted → (game monitor rect, True).

`popup_monitor_rect()` keeps its name/callers but now reads
`cfg["fullscreen_popups"]` + the blacklist + foreground exe and returns
`(rect | None, tried_same)`; the three popup call sites adapt.

## The probe (the auto-learn)

Armed only when `tried_same` was True, per popup show:

1. BEFORE showing, capture the foreground (game) hwnd + exe.
2. Show the popup as normal (no-activate).
3. A daemon thread polls `IsIconic(game_hwnd)` every 100 ms for 1.5 s.
4. If the game minimized: hide the popup, `ShowWindow(game_hwnd, SW_RESTORE)`
   (returns the game to fullscreen; it kept input focus the whole time),
   append the exe to `cfg["fse_incompatible"]`, `save_config`, log
   `"<exe> minimizes under same-monitor popups — learned, using the other
   monitor from now on"`, and re-show the popup via the caller-supplied
   reshow callback (mixer re-runs `_show_mixer` — now blacklisted → other
   monitor; OSD/alert pass None and simply stay hidden until their next
   trigger).
5. If 1.5 s passes clean: nothing to do — this game tolerates overlays and
   will never probe differently again (no state written; tolerance is
   re-verified for free on every open, which also self-heals if a game
   patch changes behavior… in the good direction; the bad direction
   re-learns on the next press).
6. One probe at a time (a flag guards re-arming while one is live); the
   probe thread touches only user32 calls, the cfg dict, and save_config —
   no COM, log-and-degrade throughout.

Worst case per incompatible game: ONE minimize-and-instant-restore, ever,
then permanent correct placement. That single event is the unavoidable price
of empiricism — the flag alone cannot distinguish tolerant from intolerant
games (v1.6.1's assumption proved that in both directions).

## Out of scope

- Xbox Game Bar widget (separate UWP/MSIX app) — parked in the Future doc
  with SDK links; revisit ONLY if auto-learn leaves a game Bristopher cares
  about on the other monitor.
- Any form of process/renderer interaction (injection) — permanently out.

## Testing

- pytest: `pick_popup_monitor` full matrix (non-exclusive, off, other,
  blacklisted, auto-tries-same, no-other-monitor cases).
- Live: desktop smoke (popup on cursor monitor, no probe); the real Siege
  exclusive-fullscreen press is backlog §13 — expected outcome A (popup on
  the game monitor, game keeps running) or outcome B (one blink + popup on
  monitor 2 + `fse_incompatible` gains the exe), both acceptable, both
  logged.
