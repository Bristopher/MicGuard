# MicGuard mixer — mouse control, configurable timeout, reset key

**Date:** 2026-07-22
**Status:** ✅ Approved by Bristopher ("yes continue! except no double click to
reset mouse bonus, i want r key only")
**Target version:** v1.11 (mixer input extension)

Extends the Shift+F3 volume mixer (shipped v1.6, nav modes/rolodex/meters v1.7)
with mouse interaction, a configurable/disablable auto-hide timeout, and an R
key that resets the selected row to 100%. Builds on the existing pure-core +
ephemeral-key + no-activate-window model — no new architectural pattern, one new
capability (a JS→Python bridge on the mixer window it never had).

## Goals

1. **Configurable auto-hide** — the mixer's hardcoded 6 s timeout becomes a
   setting; `0` disables auto-hide entirely (popup stays until Esc / click-away
   / toggle).
2. **Mouse control** (cursor-available surfaces — desktop, borderless, any time
   a cursor exists; the keyboard path is unchanged and remains the in-game path):
   - **Hover-select** — the row under the cursor becomes the selected row.
   - **Drag-to-set** — click/drag a row's bar to set that row's volume.
   - **Scroll-to-adjust** — wheel over a row nudges its volume ±2%.
3. **Reset key** — `R` sets the selected row to 100%.

## Non-goals / YAGNI

- No mouse-set of the boost zone (>100%). Drag maps 0–100% only; boost stays
  keyboard/scroll-driven through the existing `boosted_nudge` path.
- **No double-click-to-reset** (explicitly cut by Bristopher). R key only.
- No per-row scroll-step config; fixed ±2% (matches the default hotkey step).
- No mouse hook unless the spike (below) proves the native wheel path can't
  deliver — and only then, scoped to popup-visible, with Bristopher's OK.

## Config (4 new keys)

Added to `DEFAULT_CONFIG`; old configs pick them up through the standing
`DEFAULT_CONFIG | json.load(f)` merge (Dynamic-Settings.md) — no migration code.

```python
"mixer_timeout":      6,      # auto-hide seconds; 0 (or less) = never auto-hide
"mixer_hover_select": True,   # cursor over a row highlights/selects it
"mixer_drag":         True,   # click/drag a bar to set that row's volume
"mixer_scroll":       False,  # wheel over a row adjusts its volume (default OFF)
```

Defaults: the two deliberate interactions (hover, drag) ship on; scroll — the
"accidental change" risk — ships off, per the product ask. `mixer_timeout`
default 6 preserves today's behavior exactly.

## Component 1 — configurable timeout

`App._arm_mixer_timer` currently hardcodes `threading.Timer(6.0, self._hide_mixer)`.
Change: read `self.cfg.get("mixer_timeout", 6)`; if `<= 0`, cancel any existing
timer and arm **nothing** (never auto-hides). Every keypress and every mouse
bridge call re-arms it (so a drag/scroll/hover never lets the popup vanish
mid-interaction).

Pure helper (pytest): `mixer_hide_delay(cfg) -> float | None` — returns the
seconds to arm, or `None` for "no timer". Keeps the 0-means-never branch
testable without a live window.

## Component 2 — the JS→Python bridge (the one architectural change)

The mixer window is created in `_make_mixer_window` with **no `js_api`** — it is
today a one-way surface (Python pushes `setMixer`/`setLevels` via `evaluate_js`).
Add `js_api=Api()` exposing three methods (reset has no mouse counterpart —
double-click was cut; the R key handles reset entirely in `_mixer_key`). Each:
- runs on a webview worker thread → calls `_co_initialize()` first (Rule 2),
- takes a **row index** (looked up into `self._mixer_rows`, exactly how the
  keyboard uses `self._mixer_sel`) — never a raw exe/key, so JS and Python can't
  disagree on which row,
- acquires `self._mixer_lock` (Component 5) around the state read/mutate/refresh,
- re-arms the hide timer.

| Method | Behavior |
|---|---|
| `hover(index)` | Gated by `mixer_hover_select`. Sets `self._mixer_sel` to the row (clamped). JS has already moved the `.sel` highlight locally for snappiness, so this does **not** force a `setMixer` repaint — it only keeps Python's selection authoritative so keyboard nav and the next repaint agree. |
| `set_volume(index, pct)` | Gated by `mixer_drag`. Absolute set of the row to `pct` (0–100). Repaints. |
| `scroll(index, up)` | Gated by `mixer_scroll`. Nudge the row ±2% (`up` → +2, else −2). Repaints. |

**Reuse, don't reinvent, the per-row setters.** `set_volume` / `scroll` map the
row to its action identically to `_mixer_key`'s `nudge` branch:
- `row["key"] == "system"` → `_default_render_volume().SetMasterVolumeLevelScalar(pct/100)`
  for absolute; `adjust_system_volume(±2)` for scroll.
- else `exe = row.get("exe")` → `set_app_session(exe, pct)` for absolute;
  `boosted_nudge(...)` + `set_app_session` for scroll (same boost/duck semantics
  as the keyboard).
- Muted row + any adjust → unmute first (Windows-mixer feel), mirroring the
  keyboard `nudge` branch.
- **Absolute drag on a currently-boosted app row** un-boosts it first via the
  existing `_restore_boost(self.hotkeys)` path (restores ducked victims), then
  `set_app_session(exe, pct)` — so the display can't show a stale boost badge
  over a dragged-down session.

To avoid duplicating that ~20-line branch, extract a shared
`App._mixer_apply(row, *, absolute=None, step=None)` that both `_mixer_key`'s
nudge and the bridge's `set_volume`/`scroll` call.

**Drag math** — pure `bar_x_to_pct(x_frac: float) -> int`: the bar renders 100%
at ¾ width (the last ¼ is the boost overlay zone), so
`round(min(1.0, x_frac / 0.75) * 100)` clamped 0–100. Dragging anywhere in the
right quarter clamps to 100; boost is unreachable by mouse. pytest-covered.

JS side (`MIXER_HTML`): add `mousemove` (→ `hover`, only firing when the hovered
row index changes — change-detection in JS so we don't spam the bridge),
`mousedown`+`mousemove`-while-down / `click` (→ `set_volume` with the bar-local
x fraction), and `wheel` (→ `scroll`). Handlers no-op client-side based on flags
pushed in the model (`model.drag`, `model.scroll`, `model.hoverSelect`) so a
disabled interaction costs nothing.

## Component 3 — thread-safety

Mouse bridge methods (webview worker threads) and `_mixer_key` (hotkey thread)
both mutate `_mixer_sel`/`_mixer_off`/`_mixer_rows` and call `_refresh_mixer`
(COM + `evaluate_js`). Add `self._mixer_lock = threading.Lock()`; guard the
state mutate+refresh in `_mixer_key`, all four bridge methods, and
`_refresh_mixer`'s body. (`evaluate_js` itself is already marshalled to the UI
thread by pywebview; the lock protects the Python-side row model.)

## Component 4 — the R (reset) key

New **ephemeral** key, held only while the popup is visible (like Esc/M/digits —
never grabbed globally):
- `MIXER_KEYS` gains `(119, 0, 0x52)` (R). `_MIXER_KEYNAMES` gains `119: "r"`.
- `mixer_key_action` returns `("reset", 0)` for `key == "r"` in **every** nav
  mode (added alongside the `esc`/`m` early returns at the top — R is never a
  movement key, safe in wasd mode too). pytest-covered for all three modes.
- `_mixer_key` handles `kind == "reset"`: set the selected row to 100% via the
  shared `_mixer_apply(row, absolute=100)` (system → 100%, app → session 100%,
  clearing boost as in Component 2). Then refresh + re-arm.
- Footer hints (`_refresh_mixer`'s per-nav footer strings) gain `· R 100%`.

Note: resetting **system** to 100% can be loud — acceptable, it's an explicit
per-press user action (matches the existing "nudge system up" freedom).

## Component 5 — the wheel-delivery spike (plan task 1, de-risks everything else)

Mouse move/click/drag reach a `WS_EX_NOACTIVATE` window normally (they don't
activate it — which is exactly why the game keeps focus). **Wheel is the
uncertainty:** `WM_MOUSEWHEEL` is delivered to the *focused* window; a
no-activate popup never has focus, so wheel-over-hover relies on Windows'
"Scroll inactive windows when I hover over them" (default on since Win10)
forwarding it, and on WebView2's host chain surfacing it as a JS `wheel` event.

Task 1 builds the minimal `wheel`→`console.log`/bridge path on the real
no-activate mixer window and verifies (desktop) that scrolling over it fires.
- **Fires** → implement scroll as designed, done.
- **Doesn't fire** → fallback is a `WH_MOUSE_LL` hook installed **only while the
  popup is visible** and removed on hide (the same "open-UI scoped exception"
  that already licenses the meter pump's polling). The app otherwise forbids
  hooks (Hotkey-manager rule) — so this path is taken only if the native one
  fails, and flagged for Bristopher's explicit OK before shipping. Drag/hover
  are unaffected either way.

## Component 6 — Settings UI

Four rows in the existing mixer section of `SETTINGS_HTML`, mirroring the
`mixerMeters` row exactly (state key in `get_state`, persisted in `save`):
- "Auto-hide after (seconds, 0 = never)" — number input → `mixerTimeout`
- "Highlight row on hover" — toggle → `mixerHoverSelect`
- "Drag bars to set volume" — toggle → `mixerDrag`
- "Scroll wheel over a row to adjust" — toggle → `mixerScroll`

`get_state` (~line 3209) adds the four keys; `save` (~line 3363) validates and
persists them (`mixer_timeout` coerced to a clamped int ≥ 0; the three flags via
`bool(...)`). No change to the merge/persist mechanism.

## Files touched

- `micguard.py` — `DEFAULT_CONFIG` (4 keys), `mixer_hide_delay` +
  `bar_x_to_pct` pure helpers, `mixer_key_action` (`r`), `MIXER_KEYS` /
  `_MIXER_KEYNAMES` (R), `_make_mixer_window` (`js_api`), the mixer `Api`
  (3 methods: hover/set_volume/scroll), `_mixer_apply` (extracted shared
  setter), `_mixer_key`
  (reset + lock), `_arm_mixer_timer` (config-driven), `_refresh_mixer`
  (lock + footer + model flags), `MIXER_HTML` (mouse handlers), `SETTINGS_HTML`
  (4 rows), settings `get_state`/`save`.
- `tests/test_micguard.py` — `bar_x_to_pct`, `mixer_hide_delay`,
  `mixer_key_action` r-key across nav modes, scroll-step math.
- Docs: `Features/` (extend the mixer feature doc / add a section),
  `System-Conventions.md` ("Hotkey manager" ephemeral-keys row gains R; note the
  mixer now has a js_api bridge under "Window styling system"),
  `Dynamic-Settings.md` (4 new settings), the doc index, and the verification
  backlog (mouse + timeout + reset manual items) — all in the shipping change.

## Testing

- **pytest** (`uv run pytest -q`, must stay green): `bar_x_to_pct` boundaries
  (0, ¾→100, right-quarter clamp), `mixer_hide_delay` (6→6.0, 0→None, negative→
  None), `mixer_key_action` returns `("reset",0)` for `r` in digits/arrows/wasd,
  scroll ±2 step application.
- **Spike** (task 1): wheel fires over the no-activate popup on the desktop.
- **Live/manual** (verification backlog): hover moves selection; drag sets
  volume and tracks the cursor; scroll (when enabled) adjusts the hovered row;
  R resets selected to 100%; `mixer_timeout=0` keeps it open, `=3` hides in 3 s;
  a held hotkey during a drag doesn't corrupt state (lock); mouse never steals
  focus from a borderless game.

## Ideology fit

- **Event-driven, no new polling** — bridge calls are user-input events, not a
  loop; the only sanctioned wheel-hook fallback is scoped to an open UI.
- **Pure core, thin shell** — new decisions (`bar_x_to_pct`, `mixer_hide_delay`,
  the `r` action) are pure and pytest-covered; COM/Win32 stays in the shell.
- **Config = one dict, one merge** — four keys in `DEFAULT_CONFIG`, surfaced in
  Settings, no new config surface.
- **Reuse the setters** — mouse changes route through the same per-row
  volume/mute/boost paths as the keyboard, so behavior stays identical across
  input methods.

## References

- Mixer feature doc: `Docs/Features/Device-Priority-Profiles-Hotkeys.md`
  (mixer sections), `Docs/Features/Fullscreen-Safe-Popups.md` (no-activate model)
- Registry: `Docs/System-Conventions.md` — "Window styling system",
  "Hotkey manager"
- Prior mixer specs: `superpowers/specs/2026-07-14-mixer-popup-active-window-boost-design.md`,
  `superpowers/specs/2026-07-15-mixer-nav-rolodex-meters-design.md`
