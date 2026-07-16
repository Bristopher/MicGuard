# MicGuard v1.7 — mixer nav modes, rolodex sessions, level pulse, mute key

**Date:** 2026-07-15
**Status:** ✅ Approved by Bristopher (this doc is the durable record of the
brainstorm; the implementation plan references it)
**Origin:** Bristopher's requests after v1.6.1 shipped: arrow-key navigation
as a settings toggle, "3 dots / rolodex" scrolling through ALL apps playing
audio, a live "waveform" on the mixer bars (toggleable, on by default), and —
added during design review — an `M` mute toggle for the selected row.

## Scope

Four mixer-popup upgrades + two new settings. No changes to enforcement,
profiles, or the update flow. Everything below only runs while the mixer
popup is visible.

## 1. New settings (Dynamic-Settings mechanism, no exceptions)

| Key | Default | Settings UI |
|---|---|---|
| `mixer_nav` | `"digits"` | Dropdown "Mixer navigation" in the Hotkeys card: "Digits select, ↑/↓ change volume" / "Arrows: ↑/↓ select, ←/→ change volume" |
| `mixer_meters` | `true` | Switch "Live level pulse on mixer bars" |

Both added to `DEFAULT_CONFIG`; old configs gain them via the dict merge.
`save()` restarts nothing — the mixer reads `cfg` on every open/keypress, and
the meter pump checks the flag at start.

## 2. Navigation modes + M mute (pure-function core)

A new pure function maps a mixer key to an action so both modes are
pytest-coverable:

```python
def mixer_key_action(nav: str, key: str) -> tuple[str, int] | None
# key ∈ "1".."9", "up", "down", "left", "right", "esc", "m"
# returns ("select", n) | ("move", ±1) | ("nudge", ±2) | ("mute", 0)
#       | ("close", 0) | None (key inert in this mode)
```

- **digits mode (default, unchanged behavior + M):** digits select visible
  row n; ↑/↓ nudge ±2; ←/→ inert; M toggles mute; Esc closes.
- **arrows mode:** ↑/↓ move the selection (scrolling the viewport at the
  edges); ←/→ nudge ±2; digits STILL jump to visible row n (approved:
  "digits still jump"); M toggles mute; Esc closes.

The ephemeral key set (`MIXER_KEYS`) gains VK_LEFT (0x25), VK_RIGHT (0x27),
and M (0x4D), registered/unregistered exactly like the existing keys (only
while the popup is open, only on the HotkeyManager thread via the WM_APP
posts). Keys that are inert in the current mode are still registered —
swallowing ←/→ while a popup is open is the lesser evil vs. re-registering
sets on a settings change mid-open.

**Mute semantics:** app rows toggle the session's `SimpleAudioVolume`
mute; the System row toggles the default render endpoint's mute. Muted rows
render dimmed with a "muted" chip; nudging a muted row unmutes it first
(matches Windows' own mixer feel). MicGuard's capture-side auto-unmute is
untouched (it guards the mic, not outputs).

## 3. Rolodex (all audio sessions, pinned + rest)

`build_mixer_rows` output becomes two tiers (approved: "pinned + rest"):

1. **Pinned** — exactly today's rows: System, each bound `app:` target,
   the active window.
2. **Rest** — every other audio session from `list_app_sessions()`, deduped
   against pinned, sorted alphabetically (stable — rows must not jump around
   between refreshes).

The pure row-model gains a viewport: `MIXER_VISIBLE = 7` rows max on screen,
`(rows, selected, offset) -> (visible_rows, dots_below, dots_above)`. The
dots row (`• • •`) renders below the last row while more exist below (and a
dimmer one above when scrolled down). Selection moving past the visible edge
(arrow mode) shifts `offset`. Digits always address what's visible (1 = top
visible row). The popup's height is measured once per open from the visible
row count and does NOT resize while scrolling (no jitter); selection resets
to 0 and offset to 0 on each open, as today.

## 4. Live level pulse (meter pump)

A `MixerMeterPump` thread exists only while the mixer is visible: started at
the end of `_show_mixer` (if `cfg["mixer_meters"]`), stopped in
`_hide_mixer`. It:

- CoInitializes; QIs `IAudioMeterInformation` from each visible row's session
  control (System row: the default render endpoint's meter);
- polls peak values at 20 Hz and pushes `setLevels({rowKey: peak, ...})` to
  the page — the JS paints a brighter fill inside each bar, width =
  `peak × 100%` of the track (independent overlay; the volume fill stays);
- re-resolves sessions whenever `_refresh_mixer` rebuilds the row model
  (refresh stashes the row list; the pump reads the stash each tick — no
  per-tick enumeration);
- on stop: nulls every COM local, `gc.collect()`, THEN CoUninitialize
  (AI-guide mistake #11); stop event is `_stop_evt` (#12); every exception
  logs and kills only the pump, never the tray (#Rule 5).

Cost: ~a dozen COM reads × 20 Hz for the ~6 s the popup lives — negligible;
the toggle exists for user preference, not real performance need.

## 5. Out of scope (parked)

- **Auto-profile-switch when an app launches** — captured as
  [../../Future/Auto-Profile-Switch-On-App-Launch.md](../../Future/Auto-Profile-Switch-On-App-Launch.md)
  (Bristopher: "for later").
- Mouse support on the mixer (click row / drag bar) — not requested; revisit
  only if keyboard-first ever feels limiting.

## Testing

- **pytest (pure):** `mixer_key_action` across both modes × all keys;
  viewport math (offsets, dots flags, digit→row mapping while scrolled);
  rolodex tier ordering/dedup in `build_mixer_rows`.
- **Live harness:** mixer open → arrows mode select/scroll/nudge/mute round
  trip; meter pump start/stop ×3 with no COM crash on exit; sabotage test.
- **Backlog §11:** real-game feel checks (arrow mode in a borderless game,
  mute during a Discord call, pulse readability, dots discoverability),
  added in the same change.

## Risks / notes

- RegisterHotKey on ←/→/M while the popup is open swallows those keys
  globally for up to 6 s — same acknowledged behavior as today's digit grab
  (gkey rule: registration failures are skipped silently, popup still works
  minus that key).
- Session meters: some apps expose no meter interface — pump degrades to 0
  for that row, never raises.
- The System endpoint mute toggle interacts with nothing in enforcement
  (only capture mute is guarded).
