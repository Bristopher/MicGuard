# Settings Dirty-Tracking, Help Tooltips & OSD Toggle

**Status:** ✅ Production
**Author:** Bristopher
**Date:** 2026-07-24
**Version:** 2.0.0

---

## Overview

Three coordinated additions to the settings window (`SETTINGS_HTML` in
`micguard.py`):

1. **Dirty-state tracking** — edits are now visibly tracked. Changed controls
   and device/hotkey rows highlight **green**, a footer bar reads
   **"● Unsaved changes"**, a **Cancel** button reverts to the last-saved
   state, and trying to **Close** with unsaved edits raises a 3-way guard
   (Save & Close / Discard & Close / Keep Editing) instead of silently
   discarding.
2. **Help tooltips** — a small `?` glyph beside each setting shows a custom,
   theme-styled tooltip describing what that setting does.
3. **"Show volume popup (OSD)" toggle** — a new setting (`osd_enabled`,
   default on) that lets the user suppress the on-screen volume/profile popup
   that appears when a hotkey fires. Hotkeys still work; they just act
   silently.

The settings window was already a working-copy form — it loads saved config
into a JS object `S`, edits it in memory, and persists only on **Save**
(Close previously discarded silently, with a known footgun of losing edits on
an accidental tray re-open). This feature layers change-awareness on top of
that existing model; it does not change the Python save path or the config
mechanism.

## Architecture

**Baseline snapshot.** `collectState()` builds the exact object `save()`
persists. At the end of `refresh()` (after the DOM is populated),
`setBaseline()` stores `baseline` (its JSON), `baseObj` (parsed, for per-row
diffing), and `baselineProfile` (so Cancel reloads the same profile). The
baseline is cleared at the *start* of `refresh()` so mid-load renders don't
paint stale green.

**Diffing.** Any edit inside the scrolling `.content` fires a delegated
`input`/`change` listener → `updateDirty()`; list mutations that happen via
`renderList`/`renderHk` call it at the end of the render; the volume slider
and hotkey-capture (keydown, which fires neither input nor change) call it
explicitly. `updateDirty()` toggles the footer bar + Save/Cancel enabled
state, then `paintChanges()` re-applies highlighting:

- **Simple controls** (`CHANGED_SIMPLE`: state-key → element id) — the
  housing `.switchrow` gets `.changed` when that field differs from baseline.
- **Profile select, hotkeys-enabled `.sec`, EQ card** — marked individually.
- **Device rows** — keyed by device `id`: `.added` if the id isn't in the
  baseline, `.changed` if volume/hold differ.
- **Hotkey rows** — positional: `.added` beyond the baseline length,
  `.changed` if keys/target/step differ.

**Revert / close.** `cancelChanges()` calls `refresh(baselineProfile)`, which
reloads the still-unsaved config (= the baseline) and re-snapshots. The ✕ and
Close route through `tryClose()`: clean → `pywebview.api.cancel()` (hide);
dirty → show `#closemodal`. Save & Close does `await save(); cancel()`. A
successful `save()` re-baselines, so the window immediately reads clean.

**Runtime-only exclusions.** `collectState()` deliberately omits fields that
aren't persisted: the transient `connected` device flag, EQ
`available`/`processed`/`error`, and hear-yourself. They never register as
unsaved changes.

**Tooltips.** Each setting carries `<span class="help" data-tip="…">?</span>`.
A single fixed `#tip` element positions on `mouseover` of any `.help`,
clamped to and flipped inside the frameless window, and hides on `mouseout`.

**OSD toggle.** `osd_enabled` is a root flag in `DEFAULT_CONFIG` (default
`True`). `App.show_osd` early-returns when it's false — its first statement,
before any window/COM work — so both volume-hotkey and profile-switch OSDs
(which both call `show_osd`) are suppressed together. Read into the settings
window as `osdEnabled` via `get_state`, written back in `save()`, and part of
`collectState()`/`CHANGED_SIMPLE` like any other toggle.

## Features

### Implemented
- ✅ Green highlighting of changed simple controls, plus per-row `changed`/
  `added` highlighting of device and hotkey lists
- ✅ Footer "● Unsaved changes" indicator; Save/Cancel enabled only when dirty
- ✅ Cancel = revert to last-saved (reloads the baseline profile)
- ✅ 3-way close guard (Save & Close / Discard & Close / Keep Editing) when
  closing with unsaved edits
- ✅ Custom `?` help tooltips on every setting
- ✅ `osd_enabled` toggle to silence the volume/profile OSD

### Planned / Known limitations
- 🔜 Switching profiles via the dropdown while dirty still discards edits and
  re-snapshots (the pre-existing behavior). The close guard covers Close, not
  the profile switch. If this bites, extend `refresh()`'s profile-change path
  to route through the same 3-way modal.
- 🔜 Reordering a device/hotkey list marks the moved rows as `changed` (the
  honest positional/id rule) rather than recognizing a pure reorder.

## Design Philosophy / Ideology

- **One source of the saved shape.** `collectState()` is used by both `save()`
  and the dirty diff, so the "what gets saved" and "what counts as changed"
  can never drift apart.
- **Layout-stable highlighting.** Green is applied via color + border-color +
  a subtle knob ring + row `outline` — never anything that reflows the form.
- **Consistent with the window's existing model.** The window was already
  working-copy-then-Save; this makes that model *visible* instead of changing
  it. Persistence, the config merge, and live-apply on Save are untouched.
- **Never native tooltips.** `title=` clips at the frameless window edge and
  ignores the shadcn/zinc theme; a single positioned `#tip` element matches
  the rest of the UI and stays on-screen.

## API / Interface Reference

JS (`SETTINGS_HTML`):
- `collectState() -> object` — the persisted state shape (also the diff unit).
- `setBaseline()` / `isDirty()` / `updateDirty()` / `paintChanges()`.
- `CHANGED_SIMPLE` — `{stateKey: elementId}` for `.switchrow` controls.
- `cancelChanges()` — revert to the saved baseline.
- `tryClose()` / `closeSave()` / `closeDiscard()` / `closeKeep()`.

Python (`micguard.py`):
- `App.show_osd(...)` — early-returns when `cfg["osd_enabled"]` is false.
- `get_state` adds `osdEnabled`; `save` writes `cfg["osd_enabled"]`.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `osd_enabled` | `true` | Show the on-screen volume/profile popup on hotkey fire |

Dirty-tracking and tooltips have no config surface — they're always on. See
[Dynamic-Settings.md](../Dynamic-Settings.md) for the add-a-setting steps
(a new setting must join `collectState()`/`CHANGED_SIMPLE` to be tracked).

## Testing

- **pytest** (`tests/test_micguard.py`, `OsdEnabledSettingTests`): the
  `osd_enabled` default, the old-config merge picking it up, and the
  `show_osd` guard returning early when disabled / proceeding when enabled.
- **JS syntax:** the settings `<script>` is `node --check`-clean.
- **Behavioral (harness):** the settings HTML was driven with a stubbed
  `pywebview` API to confirm the dirty bar toggles, Save/Cancel enable/disable,
  per-control and per-row (changed + added) green appear, Cancel reverts,
  tooltips show the right copy, and the 3-way close modal appears only when
  dirty. See the Verification Backlog for the human click-paths that only a
  real install can confirm.

## Troubleshooting

- **A new setting doesn't persist or doesn't turn the bar on** — it's missing
  from `collectState()`. Add it there (and read it in Python `save()`).
- **A new setting saves but never shows green** — add it to `CHANGED_SIMPLE`
  (simple control) or extend `paintChanges()` (list row).
- **Tooltip clipped at the window edge** — `positionTip()` clamps/flips; if a
  new glyph sits in an unusual container, confirm `#tip` is a direct child of
  `body` (fixed positioning).
- **OSD still shows after disabling** — confirm the toggle saved
  (`osd_enabled: false` in `config.json`); the guard is read live on every
  `show_osd` call, no restart needed.

## References
- [System-Conventions.md](../System-Conventions.md) — "Settings dirty-tracking,
  help tooltips & close-guard" and "Window styling system" rows
- [Dynamic-Settings.md](../Dynamic-Settings.md) — config mechanism + add-a-setting
- [Verify/2026_07-12_Verification-Backlog.md](../Verify/2026_07-12_Verification-Backlog.md) — §21
